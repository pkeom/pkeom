"""네이버 스마트스토어 커머스 API 클라이언트"""
import base64
import difflib
import logging
import time
from datetime import datetime, timedelta, timezone
import bcrypt
import requests

logger = logging.getLogger(__name__)


class SmartstoreAPI:
    BASE_URL = "https://api.commerce.naver.com/external"

    def __init__(self, client_id: str, client_secret: str, account_type: str = "SELF"):
        # YAML이 int/bool로 파싱하는 경우 대비 str() 변환 후
        # split()+join()으로 앞·뒤·중간에 끼어있는 공백·줄바꿈·제어문자 모두 제거
        self.client_id     = str(client_id).strip()
        self.client_secret = "".join(str(client_secret).split())
        self.account_type  = str(account_type).strip()
        self._token = None
        self._token_expires_at = 0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        # ── 네이버 커머스 API 공식 서명 생성 ──────────────────────────────
        # signature = Base64( bcrypt( "{client_id}_{timestamp}", client_secret ) )
        # 참고: https://apicenter.commerce.naver.com (인증 토큰 발급 가이드)
        #
        # client_secret 은 네이버가 PHP bcrypt 로 생성한 $2y$ 형식 해시값이며,
        # bcrypt.hashpw() 의 두 번째 인자(salt)로 직접 전달한다.
        # bcrypt 4.0+ (Rust 백엔드) 는 $2y$/$2a$/$2b$ 모두 허용하므로
        # prefix 변환 없이 원본 그대로 사용하는 것이 공식 예제와 일치한다.
        timestamp = str(int(time.time() * 1000))
        password  = f"{self.client_id}_{timestamp}"

        salt = self.client_secret.encode("utf-8")

        # 유효성 사전 검사 — 비어있거나 bcrypt 형식이 아닌 경우 명확한 에러 제공
        if not salt.startswith(b"$2"):
            raise ValueError(
                f"client_secret 이 유효한 bcrypt 형식이 아닙니다. "
                f"네이버 커머스 API 센터에서 발급된 '$2y$...' 형식 시크릿을 "
                f"config/settings.yaml 의 smartstore.client_secret 에 입력하세요. "
                f"(현재 앞 10자: {self.client_secret[:10]!r})"
            )

        hashed    = bcrypt.hashpw(password.encode("utf-8"), salt)
        signature = base64.b64encode(hashed).decode("utf-8")

        resp = requests.post(
            f"{self.BASE_URL}/v1/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id":          self.client_id,
                "timestamp":          timestamp,
                "client_secret_sign": signature,
                "grant_type":         "client_credentials",
                "type":               self.account_type,
            },
            timeout=10,
        )

        if not resp.ok:
            # Naver 에러 응답 본문을 그대로 로그에 남겨 원인 파악 가능하게 한다
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            logger.error(
                "스마트스토어 토큰 발급 실패 [%s] — Naver 응답: %s",
                resp.status_code, body,
            )
            raise requests.HTTPError(
                f"토큰 발급 실패 {resp.status_code}: {body}",
                response=resp,
            )

        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + data["expires_in"]
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def find_leaf_category(self, keyword: str, whole_cat: str = "") -> tuple[str, str]:
        """키워드로 Naver /v1/categories?keyword= API를 호출하고, 카테고리명에 키워드가
        포함된 리프 카테고리들을 후보로 추린 뒤, 여러 개면 whole_cat(도매 카테고리 경로)과
        wholeCategoryName의 문자열 유사도로 가장 적합한 카테고리를 선택한다.

        Returns: (category_id, wholeCategoryName) — 매칭 없으면 ("", "")
        """
        if not keyword or not keyword.strip():
            return "", ""
        keyword  = keyword.strip()
        kw_lower = keyword.lower()
        try:
            resp = requests.get(
                f"{self.BASE_URL}/v1/categories",
                headers=self._headers(),
                params={"keyword": keyword},
                timeout=10,
            )
            if resp.ok:
                data  = resp.json()
                items = data if isinstance(data, list) else []

                candidates: list[tuple[str, str]] = []
                for cat in items:
                    if not isinstance(cat, dict) or not cat.get("last"):
                        continue
                    name  = cat.get("name", "")
                    whole = cat.get("wholeCategoryName", "") or name
                    if kw_lower in name.lower() or kw_lower in whole.lower():
                        candidates.append((str(cat["id"]), whole or name))

                if not candidates:
                    return "", ""
                if len(candidates) == 1:
                    return candidates[0]

                # 결과가 여러 개면 도매 카테고리 경로와 유사도 비교 후 최선 선택
                ref = (whole_cat or keyword).lower()
                best = max(
                    candidates,
                    key=lambda p: difflib.SequenceMatcher(None, ref, p[1].lower()).ratio(),
                )
                return best
        except Exception as e:
            logger.warning("카테고리 조회 실패 (%s): %s", keyword, e)
        return "", ""

    def get_kc_cert_status(self, category_id: str, cert_type_hint: str = "") -> tuple[bool, int]:
        """KC인증 필수 여부 + 최적 certificationInfoId 반환.

        cert_type_hint: safetykorea.kr 인증구분 텍스트 (예: "안전확인대상 전기용품")
        Returns: (is_kc_required, certificationInfoId)  — ID 0은 미확인
        """
        if not category_id:
            return False, 0
        try:
            resp = requests.get(
                f"{self.BASE_URL}/v1/categories/{category_id}",
                headers=self._headers(),
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                is_required = "KC_CERTIFICATION" in data.get("exceptionalCategories", [])

                # KC_CERTIFICATION kindType을 지원하는 항목만 추림
                kc_infos = [
                    ci for ci in data.get("certificationInfos", [])
                    if ci.get("certificationMarkType") == "KC"
                       and "KC_CERTIFICATION" in ci.get("kindTypes", [])
                ]

                cert_info_id = 0
                if kc_infos:
                    if cert_type_hint:
                        _KW = ["전기용품", "생활용품", "어린이제품", "방송통신기자재",
                               "안전인증", "안전확인", "공급자적합성확인"]
                        def _score(ci):
                            n = ci.get("name", "")
                            return sum(1 for kw in _KW
                                       if kw in cert_type_hint and kw in n)
                        cert_info_id = max(kc_infos, key=_score)["id"]
                    else:
                        cert_info_id = kc_infos[0]["id"]

                logger.info("KC인증 정보 조회: required=%s, certInfoId=%s (hint=%r)",
                            is_required, cert_info_id, cert_type_hint[:30] if cert_type_hint else "")
                return is_required, cert_info_id
        except Exception as e:
            logger.warning("카테고리 KC인증 정보 조회 실패 (%s): %s", category_id, e)
        return False, 0

    def is_kc_cert_required(self, category_id: str) -> bool:
        required, _ = self.get_kc_cert_status(category_id)
        return required

    # 도메인별 Referer 매핑 (hotlink 방지 우회)
    _REFERER_MAP = {
        "domeggook.com":   "https://www.domeggook.com/",
        "domaemae.co.kr":  "https://www.domaemae.co.kr/",
        "domeme.domeggook.com": "https://domeme.domeggook.com/",
        "gi.esmplus.com":  "https://www.domeggook.com/",
        "dnvefa72aowie.cloudfront.net": "https://domeme.domeggook.com/",
    }
    _DOWNLOAD_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def _download_headers(self, image_url: str) -> dict:
        """이미지 URL 도메인에 맞는 다운로드 헤더 반환."""
        from urllib.parse import urlparse
        host = urlparse(image_url).netloc.lower()
        referer = next(
            (v for k, v in self._REFERER_MAP.items() if host.endswith(k)),
            f"https://{host}/",
        )
        return {
            "User-Agent":      self._DOWNLOAD_UA,
            "Referer":         referer,
            "Accept":          "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
        }

    def upload_image(self, image_url: str) -> str:
        """외부 이미지 URL을 Naver 상품 이미지 CDN에 업로드하고 반환된 URL을 돌려준다."""
        import io
        dl = requests.get(image_url, timeout=20,
                          headers=self._download_headers(image_url))
        if not dl.ok:
            raise requests.HTTPError(
                f"이미지 다운로드 실패 {dl.status_code}: {image_url}", response=dl
            )
        content_type = dl.headers.get("Content-Type", "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            # application/octet-stream 등 비이미지 Content-Type → magic bytes로 실제 타입 감지
            raw = dl.content
            if raw and raw[0] == 0xFF and raw[1] == 0xD8:
                content_type = "image/jpeg"
            elif raw.startswith(b'\x89PNG\r\n\x1a\n'):
                content_type = "image/png"
            elif raw.startswith(b'GIF87a') or raw.startswith(b'GIF89a'):
                content_type = "image/gif"
            elif raw.startswith(b'RIFF') and len(raw) >= 12 and raw[8:12] == b'WEBP':
                content_type = "image/webp"
            else:
                raise ValueError(
                    f"이미지 URL이 유효한 이미지를 반환하지 않음 (Content-Type: {content_type}): {image_url}"
                )
        ext_map = {"image/jpeg": ".jpg", "image/png": ".png",
                   "image/gif": ".gif", "image/bmp": ".bmp", "image/webp": ".webp"}
        ext = ext_map.get(content_type, ".jpg")
        filename = image_url.split("/")[-1].split("?")[0] or f"image{ext}"
        if not any(filename.lower().endswith(e) for e in ext_map.values()):
            filename += ext
        resp = requests.post(
            f"{self.BASE_URL}/v1/product-images/upload",
            headers={"Authorization": f"Bearer {self._get_token()}"},
            files={"imageFiles": (filename, io.BytesIO(dl.content), content_type)},
            timeout=30,
        )
        if not resp.ok:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise requests.HTTPError(
                f"이미지 업로드 실패 {resp.status_code}: {body}", response=resp
            )
        return resp.json()["images"][0]["url"]

    def upload_image_data(self, data: bytes, content_type: str, filename: str) -> str:
        """이미 다운로드된 이미지 바이트를 Naver 상품 이미지 CDN에 업로드하고 URL을 반환한다.

        429 Rate Limit 시 최대 3회 backoff 재시도.
        """
        import io
        if not content_type.startswith("image/"):
            raise ValueError(f"유효한 이미지 Content-Type이 아닙니다: {content_type}")
        for attempt in range(3):
            resp = requests.post(
                f"{self.BASE_URL}/v1/product-images/upload",
                headers={"Authorization": f"Bearer {self._get_token()}"},
                files={"imageFiles": (filename, io.BytesIO(data), content_type)},
                timeout=30,
            )
            if resp.status_code == 429:
                wait = (attempt + 1) * 3  # 3s → 6s → 9s
                logger.warning("이미지 업로드 Rate Limit (429), %d초 후 재시도 (%d/3)", wait, attempt + 1)
                time.sleep(wait)
                continue
            if not resp.ok:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                raise requests.HTTPError(
                    f"이미지 업로드 실패 {resp.status_code}: {body}", response=resp
                )
            return resp.json()["images"][0]["url"]
        raise requests.HTTPError(f"이미지 업로드 Rate Limit — 3회 재시도 후 실패: {filename}")

    def get_orders(self, status: str = "PAYED", days: int = 3) -> list:
        """주문 목록 직접 조회 — GET /v1/pay-order/seller/product-orders

        파라미터 스펙:
          rangeType=PAYED_DATETIME, from/to KST(+09:00), page=1부터, pageSize=300
          from/to 최대 24시간 제한 → 24h 단위로 분할 조회
        """
        from datetime import datetime, timedelta, timezone, timezone as tz
        KST      = timezone(timedelta(hours=9))
        now_kst  = datetime.now(KST)
        page_size = 300

        logger.info("주문 직접 조회 시작 — status=%s 최근 %d일 (24h 단위 분할, KST 기준)",
                    status, days)

        all_orders: list = []

        for day in range(days):
            window_end   = now_kst - timedelta(days=day)
            window_start = now_kst - timedelta(days=day + 1)
            from_str = window_start.strftime("%Y-%m-%dT%H:%M:%S.000+09:00")
            to_str   = window_end.strftime("%Y-%m-%dT%H:%M:%S.000+09:00")

            page = 1
            while True:
                resp = requests.get(
                    f"{self.BASE_URL}/v1/pay-order/seller/product-orders",
                    headers=self._headers(),
                    params={
                        "rangeType":            "PAYED_DATETIME",
                        "productOrderStatuses": status,
                        "from":                 from_str,
                        "to":                   to_str,
                        "page":                 page,
                        "pageSize":             page_size,
                    },
                    timeout=10,
                )
                if not resp.ok:
                    try:
                        err_body = resp.json()
                    except Exception:
                        err_body = resp.text
                    logger.error(
                        "product-orders 조회 실패 [HTTP %s] from=%s to=%s page=%d | 응답: %s",
                        resp.status_code, from_str[:10], to_str[:10], page, err_body,
                    )
                    resp.raise_for_status()

                body = resp.json()
                # 실제 응답: {"data": [...]} — data가 배열 직접
                data = body.get("data", [])
                if isinstance(data, list):
                    contents = data
                    has_next = False  # 페이지네이션 없음
                else:
                    contents = data.get("contents", [])
                    has_next = data.get("pagination", {}).get("hasNext", False)

                logger.info("product-orders [%s~%s] page=%d — %d건 hasNext=%s",
                            from_str[:10], to_str[:10], page, len(contents), has_next)

                all_orders.extend(contents)

                if not has_next:
                    break
                page += 1
                time.sleep(0.2)

            if day < days - 1:
                time.sleep(0.3)

        logger.info("주문 직접 조회 완료: 총 %d건", len(all_orders))
        return all_orders

    def dispatch_order(self, product_order_id: str, delivery_company_code: str, tracking_number: str) -> dict:
        """송장 등록 (발송 처리)
        product_order_id: productOrderId (스마트스토어 상품주문번호)
        delivery_company_code: 네이버 택배사 코드 (예: CJGLS, LOTTE, HANJIN)
        """
        resp = requests.post(
            f"{self.BASE_URL}/v1/pay-order/seller/product-orders/dispatch",
            headers=self._headers(),
            json={
                "dispatchProductOrders": [
                    {
                        "productOrderId":    product_order_id,
                        "deliveryMethod":    "DELIVERY",
                        "deliveryCompanyCode": delivery_company_code,
                        "trackingNumber":    tracking_number,
                        "dispatchDate":      datetime.now(
                            timezone(timedelta(hours=9))
                        ).isoformat(timespec="milliseconds"),
                    }
                ]
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def set_product_sale_status(self, origin_product_no: str, on_sale: bool):
        """상품 판매 상태 변경 — GET 후 statusType만 수정해 PUT.

        PATCH /origin-products 미지원 → GET 전체 바디 획득 후 statusType 변경 PUT.
        """
        status_type = "SALE" if on_sale else "SUSPENSION"
        url = f"{self.BASE_URL}/v2/products/origin-products/{origin_product_no}"

        # 1. 현재 상품 전체 데이터 GET
        get_resp = requests.get(url, headers=self._headers(), timeout=10)
        logger.info(
            "[set_product_sale_status] GET %s → status=%s",
            url, get_resp.status_code,
        )
        get_resp.raise_for_status()
        current = get_resp.json()

        # 2. statusType만 수정
        put_body = dict(current)
        put_body["originProduct"] = dict(current.get("originProduct", {}))
        put_body["originProduct"]["statusType"] = status_type

        logger.info(
            "[set_product_sale_status] PUT %s statusType=%s",
            url, status_type,
        )

        # 3. 전체 바디 PUT
        put_resp = requests.put(
            url,
            headers=self._headers(),
            json=put_body,
            timeout=15,
        )

        logger.info(
            "[set_product_sale_status] PUT 응답 status=%s body=%s",
            put_resp.status_code, put_resp.text[:500],
        )

        try:
            data = put_resp.json()
        except ValueError:
            data = {"raw": put_resp.text}

        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            logger.error(
                "[set_product_sale_status] errors 응답: origin_product_no=%s errors=%s",
                origin_product_no, errors,
            )
            raise RuntimeError(
                f"판매상태 변경 실패 (errors): {errors} (origin_product_no={origin_product_no})"
            )

        put_resp.raise_for_status()
        return data

    def get_product(self, product_id: str) -> dict:
        """상품 정보 조회 — GET /v2/products/origin-products/{originProductNo}"""
        resp = requests.get(
            f"{self.BASE_URL}/v2/products/origin-products/{product_id}",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_products(self, size: int = 100) -> list:
        """판매자 상품 목록 조회"""
        resp = requests.get(
            f"{self.BASE_URL}/v2/products",
            headers=self._headers(),
            params={"size": size},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("simpleProducts", data.get("contents", data.get("products", [])))

    def confirm_orders(self, product_order_ids: list[str]) -> dict:
        """발주 확인 처리 — 30건씩 배치 호출.

        POST /v1/pay-order/seller/product-orders/confirm
        구매자에게 판매자가 주문을 접수했음을 알림. 최대 30건 제한이 있어
        30건씩 나눠서 연속 호출한다.

        반환: {"confirmed": [성공 IDs], "failed": [실패 IDs]}
        """
        if not product_order_ids:
            return {"confirmed": [], "failed": []}

        BATCH = 30
        confirmed: list[str] = []
        failed: list[str] = []

        for i in range(0, len(product_order_ids), BATCH):
            batch = product_order_ids[i : i + BATCH]
            try:
                resp = requests.post(
                    f"{self.BASE_URL}/v1/pay-order/seller/product-orders/confirm",
                    headers=self._headers(),
                    json={"productOrderIds": batch},
                    timeout=10,
                )
                resp.raise_for_status()
                confirmed.extend(batch)
                logger.info(
                    "발주확인 배치 성공: %d~%d건 / 총 %d건",
                    i + 1, i + len(batch), len(product_order_ids),
                )
            except Exception as e:
                logger.error(
                    "발주확인 배치 실패 (%d~%d건): %s",
                    i + 1, i + len(batch), e,
                )
                failed.extend(batch)

        return {"confirmed": confirmed, "failed": failed}

    def get_cancellations(self, hours: int = 1) -> list:
        """취소 요청 목록 조회 (최근 hours시간 이내 CANCEL_REQUEST 상태)"""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=hours)

        resp = requests.get(
            f"{self.BASE_URL}/v1/pay-order/seller/product-orders/last-changed-statuses",
            headers=self._headers(),
            params={
                "lastChangedFrom": window_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "lastChangedTo":   now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "productOrderStatuses": "CANCEL_REQUEST",
                "limitCount": 100,
            },
            timeout=10,
        )
        resp.raise_for_status()
        changed = resp.json().get("lastChangeStatuses", [])
        product_order_ids = [item["productOrderId"] for item in changed]

        if not product_order_ids:
            return []

        resp = requests.post(
            f"{self.BASE_URL}/v1/pay-order/seller/product-orders/query",
            headers=self._headers(),
            json={"productOrderIds": product_order_ids},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def get_product_order(self, product_order_id: str) -> dict:
        """단건 상품주문 상세 조회 — productOrderStatus 등 현재 상태 확인용."""
        resp = requests.post(
            f"{self.BASE_URL}/v1/pay-order/seller/product-orders/query",
            headers=self._headers(),
            json={"productOrderIds": [product_order_id]},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return data[0] if data else {}

    def approve_cancel(self, product_order_id: str) -> dict:
        """취소 요청 승인 — 환불 처리.
        발송 전 주문에만 유효. 발송 후에는 반품 절차로 처리해야 함.
        """
        resp = requests.post(
            f"{self.BASE_URL}/v1/pay-order/seller/product-orders"
            f"/{product_order_id}/claim/cancel/approve",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_returns(self, hours: int = 1) -> list:
        """반품 신청 목록 조회 (최근 hours시간 이내 RETURN_REQUEST 상태)"""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=hours)

        resp = requests.get(
            f"{self.BASE_URL}/v1/pay-order/seller/product-orders/last-changed-statuses",
            headers=self._headers(),
            params={
                "lastChangedFrom": window_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "lastChangedTo":   now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "productOrderStatuses": "RETURN_REQUEST",
                "limitCount": 100,
            },
            timeout=10,
        )
        resp.raise_for_status()
        changed = resp.json().get("lastChangeStatuses", [])
        product_order_ids = [item["productOrderId"] for item in changed]

        if not product_order_ids:
            return []

        resp = requests.post(
            f"{self.BASE_URL}/v1/pay-order/seller/product-orders/query",
            headers=self._headers(),
            json={"productOrderIds": product_order_ids},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
