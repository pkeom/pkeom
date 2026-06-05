"""도매매(도매꾹) Private API 클라이언트 — 공식 Private API 인증"""
import datetime
import logging
import socket
import requests

logger = logging.getLogger(__name__)

API_URL = "https://domeggook.com/ssl/api/"
_RENEW_BUFFER = 30  # sIdRenewDate 만료 N초 전에 갱신


class DomaemaeClient:

    def __init__(self, api_key: str, user_id: str = "", password: str = "", **_):
        self.api_key  = api_key
        self.user_id  = user_id
        self.password = password

        self._sid: str = ""
        self._sid_renew_date: datetime.datetime | None = None
        self._login_time: datetime.datetime | None = None
        self._login_keep_seconds: int = 0

    # ── 세션 관리 ─────────────────────────────────────────────────

    @staticmethod
    def _local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    @staticmethod
    def _parse_dt(value) -> datetime.datetime:
        """API 날짜 값(문자열·숫자) → datetime. 파싱 실패 시 now+180s."""
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d%H%M%S"):
            try:
                return datetime.datetime.strptime(str(value), fmt)
            except ValueError:
                pass
        try:
            return datetime.datetime.fromtimestamp(float(value))
        except Exception:
            return datetime.datetime.now() + datetime.timedelta(seconds=180)

    def _login(self):
        """setLogin으로 새 세션 발급 (id/pw 필요)."""
        resp = requests.post(API_URL, data={
            "mode":       "setLogin",
            "ver":        "4.1",
            "om":         "json",
            "aid":        self.api_key,
            "id":         self.user_id,
            "pw":         self.password,
            "loginKeep":  "1",
            "ip":         self._local_ip(),
            "device":     "Third Party",
        }, timeout=10)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            raise ValueError(f"도매매 로그인 응답 JSON 파싱 실패: {resp.text[:200]}")
        root = data.get("domeggook", data)

        self._sid        = str(root.get("sId", ""))
        self._login_time = datetime.datetime.now()

        renew_raw = root.get("sIdRenewDate", "")
        self._sid_renew_date = (
            self._parse_dt(renew_raw) if renew_raw
            else datetime.datetime.now() + datetime.timedelta(seconds=180)
        )

        keep = root.get("loginKeepTime", 0)
        self._login_keep_seconds = int(keep) if keep else 30 * 24 * 3600

        logger.info("도매매 로그인 성공 (sId 앞 8자: %s)", self._sid[:8])

    def _renew(self):
        """setLoginChk로 sId 갱신 (30일 세션 유지)."""
        resp = requests.post(API_URL, data={
            "mode":          "setLoginChk",
            "ver":           "4.0",
            "om":            "json",
            "aid":           self.api_key,
            "id":            self.user_id,
            "sId":           self._sid,
            "sIdRenewDate":  self._sid_renew_date.strftime("%Y-%m-%d %H:%M:%S")
                             if self._sid_renew_date else "",
            "loginKeep":     "1",
        }, timeout=10)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            raise ValueError(f"도매매 세션 갱신 응답 JSON 파싱 실패: {resp.text[:200]}")
        root = data.get("domeggook", data)

        new_sid = str(root.get("sId", ""))
        if new_sid:
            self._sid = new_sid

        renew_raw = root.get("sIdRenewDate", "")
        self._sid_renew_date = (
            self._parse_dt(renew_raw) if renew_raw
            else datetime.datetime.now() + datetime.timedelta(seconds=180)
        )
        logger.debug("도매매 sId 갱신 완료")

    def _ensure_session(self):
        """API 호출 전 세션 유효성 보장."""
        now = datetime.datetime.now()

        if not self._sid:
            self._login()
            return

        # loginKeepTime 초과 → 재로그인
        if (self._login_time and self._login_keep_seconds > 0
                and (now - self._login_time).total_seconds() >= self._login_keep_seconds):
            logger.info("도매매 세션 만료(loginKeepTime 초과) → 재로그인")
            self._login()
            return

        # sIdRenewDate 임박 → 갱신
        if (self._sid_renew_date
                and now >= self._sid_renew_date - datetime.timedelta(seconds=_RENEW_BUFFER)):
            self._renew()

    def _get(self, mode: str, ver: str, extra: dict | None = None) -> dict:
        self._ensure_session()
        params = {"mode": mode, "ver": ver, "om": "json",
                  "aid": self.api_key, "sId": self._sid}
        if extra:
            params.update(extra)
        resp = requests.get(API_URL, params=params, timeout=10)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            raise ValueError(f"도매매 API 응답 JSON 파싱 실패 (mode={mode}): {resp.text[:200]}")
        return data.get("domeggook", data)

    def _post(self, mode: str, ver: str, data: dict | None = None) -> dict:
        self._ensure_session()
        d = {"mode": mode, "ver": ver, "om": "json",
             "aid": self.api_key, "sId": self._sid}
        if data:
            d.update(data)
        resp = requests.post(API_URL, data=d, timeout=10)
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError:
            raise ValueError(f"도매매 API 응답 JSON 파싱 실패 (mode={mode}): {resp.text[:200]}")
        return body.get("domeggook", body)

    # ── 파싱 헬퍼 ─────────────────────────────────────────────────

    @staticmethod
    def _parse_options(select_opt) -> list[dict]:
        """getItemView selectOpt 딕셔너리 → [{"id": str, "name": str}]

        selectOpt 구조: {"CODE": {"name": str, "sup": int, "hid": int}}
        hid=2 : 완전 숨김 → 제외
        """
        if not select_opt or not isinstance(select_opt, dict):
            return []
        options = []
        seen: set[str] = set()
        for code, info in select_opt.items():
            if not isinstance(info, dict):
                continue
            try:
                hid = int(info.get("hid", 0))
            except (TypeError, ValueError):
                hid = 0
            if hid == 2:
                continue
            name = str(info.get("name", "")).strip()
            if name and code not in seen:
                options.append({"id": str(code), "name": name})
                seen.add(code)
        return options

    @staticmethod
    def _match_option(options: list[dict], option_name: str) -> str | None:
        """옵션명으로 옵션 ID 검색.

        우선순위:
          1. 정확 일치 (대소문자·공백 무시)
          2. 포함 관계 (양방향)
        """
        if not option_name or not options:
            return None
        normalized = option_name.strip().lower()
        for opt in options:
            if opt["name"].strip().lower() == normalized:
                return opt["id"]
        for opt in options:
            lower = opt["name"].strip().lower()
            if normalized in lower:
                return opt["id"]
        return None

    # ── 공개 API ──────────────────────────────────────────────────

    def get_product(self, product_id: str) -> dict:
        """상품 상세 정보 조회. 반환값: {product_id, title, price, stock}"""
        root  = self._get("getItemView", "4.5", {"no": product_id})
        basis = root.get("basis", {})
        price = root.get("price", {})
        qty   = root.get("qty", {})
        try:
            price_val = int(price.get("supply") or price.get("dome") or 0) or None
        except (TypeError, ValueError):
            price_val = None
        try:
            stock_val = int(qty.get("inventory", 0))
        except (TypeError, ValueError):
            stock_val = None
        return {
            "product_id": product_id,
            "title":      basis.get("title", ""),
            "price":      price_val,
            "stock":      stock_val,
        }

    def get_stock(self, product_id: str) -> int | None:
        return self.get_product(product_id)["stock"]

    def get_options(self, product_id: str) -> list[dict]:
        """상품 옵션 목록. [{"id": str, "name": str}]"""
        root = self._get("getItemView", "4.5", {"no": product_id})
        return self._parse_options(root.get("selectOpt"))

    def place_order(self, product_id: str, quantity: int, shipping_info: dict,
                    *, option_id: str = "", option_name: str = "",
                    dry_run: bool = False) -> str:
        """setOrder로 발주. 주문번호 반환.

        옵션 처리 우선순위:
          1. option_id  : 매핑에 저장된 도매처 옵션 코드를 직접 사용 (권장)
          2. option_name: option_id 없을 때 옵션명으로 API 검색 후 매칭 (폴백)
          3. 둘 다 없음 : 옵션 없이 발주

        shipping_info 필수 키: name, phone, zipcode, address
        shipping_info 선택 키: memo
        """
        if dry_run:
            opt_tag = f" opt={option_id or option_name}" if (option_id or option_name) else ""
            return f"[DRY_RUN] prod={product_id} qty={quantity}{opt_tag}"

        option_code: str | None = None

        if option_id:
            # supplier_option_id 직접 사용 — API 검색 불필요
            option_code = option_id
            logger.info(
                "도매매 발주 옵션 직접 전달: product=%s, option_id=%s",
                product_id, option_id,
            )
        elif option_name:
            # 폴백: 옵션명으로 getItemView 호출 후 매칭
            options = self.get_options(product_id)
            option_code = self._match_option(options, option_name)
            if option_code:
                logger.info(
                    "도매매 발주 옵션 매칭 성공: '%s' → code=%s (product=%s)",
                    option_name, option_code, product_id,
                )
            else:
                logger.warning(
                    "도매매 발주 옵션 매칭 실패: '%s' (product=%s, 후보=%d건) — 옵션 없이 발주",
                    option_name, product_id, len(options),
                )
        else:
            logger.debug("도매매 발주 옵션 없음: product=%s", product_id)

        data: dict = {
            "item[0][no]":        product_id,
            "item[0][cnt]":       str(quantity),
            "deliinfo[name]":     shipping_info["name"],
            "deliinfo[mobile]":   shipping_info["phone"],
            "deliinfo[post]":     shipping_info["zipcode"],
            "deliinfo[addr1]":    shipping_info["address"],
            "deliinfo[deli_msg]": shipping_info.get("memo", ""),
            "receipt":            "0",
        }
        if option_code:
            data["item[0][option]"] = option_code

        root = self._post("setOrder", "4.3", data)

        # e머니 부족 및 일반 에러 감지
        from src.core.exceptions import EmoneyShortageError
        errors = root.get("errors", {})
        if isinstance(errors, dict):
            dcode = errors.get("dcode", "")
            if dcode == "LACK_MONEY":
                raise EmoneyShortageError(
                    f"도매매 e머니 잔액 부족 — 충전 후 재시도하세요. "
                    f"(msg: {errors.get('msg', '')})"
                )
        err = root.get("error") or root.get("errCode") or root.get("errMsg")
        if err:
            raise RuntimeError(f"도매매 발주 실패: {err}")
        return str(root.get("orderNo", ""))

    def cancel_order(self, order_no: str) -> dict:
        """발주 취소 (setOrdDeny).

        배송 시작 전에만 취소 가능. 이미 발송된 경우 API가 오류를 반환합니다.
        """
        root = self._post("setOrdDeny", "4.0", {"no": order_no})
        err = root.get("error") or root.get("errCode") or root.get("errMsg")
        if err:
            raise RuntimeError(f"도매매 발주 취소 실패: {err}")
        return root

    def get_cancel_result(self, order_no: str) -> str:
        """setOrdDeny 후 도매처 취소 처리 결과 확인 (getOrderView).

        반환: 'APPROVED' | 'REJECTED' | 'PENDING'
        ※ 실제 status 값은 API 문서 확인 후 아래 키워드 목록 조정 필요.
        """
        try:
            root  = self._get("getOrderView", "4.0", {"for": "buy", "no": order_no})
            items = root.get("items", [])
            if isinstance(items, dict):
                items = list(items.values())
            item   = items[0] if items and isinstance(items[0], dict) else {}
            status = (
                str(item.get("status",         ""))
                or str(item.get("ordStatus",   ""))
                or str(item.get("cancelStatus",""))
                or str(root.get("status",      ""))
            ).lower()
            if any(k in status for k in ["cancel_ok", "취소승인", "cancel_accept", "deny_ok", "refund"]):
                return "APPROVED"
            if any(k in status for k in ["cancel_deny", "취소거부", "deny_fail", "cancel_reject"]):
                return "REJECTED"
        except Exception as e:
            logger.warning("도매매 취소 결과 조회 실패 (%s): %s", order_no, e)
        return "PENDING"

    def get_order_tracking(self, order_no: str) -> dict:
        """getOrderView로 송장 정보 조회."""
        root  = self._get("getOrderView", "4.0", {"for": "buy", "no": order_no})
        items = root.get("items", [])
        if isinstance(items, dict):
            items = list(items.values())
        delivery = items[0].get("delivery", {}) if items and isinstance(items[0], dict) else {}
        return {
            "order_no":         order_no,
            "delivery_company": delivery.get("companyName", ""),
            "tracking_number":  delivery.get("code", ""),
        }
