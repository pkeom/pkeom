"""네이버 스마트스토어 커머스 API 클라이언트"""
import base64
import logging
import time
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

    def get_orders(self, status: str = "PAYED", days: int = 1) -> list:
        """주문 목록 조회 (days: 최근 몇 일치)
        last-changed-statuses API 최대 조회 범위: 24시간
        days > 1 이면 24시간 단위로 분할 조회 후 합산
        """
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        window = timedelta(hours=24)

        product_order_ids = []
        window_end = now
        remaining = timedelta(days=days)

        first = True
        while remaining.total_seconds() > 0:
            if not first:
                time.sleep(0.3)
            first = False
            window_start = window_end - min(window, remaining)
            resp = requests.get(
                f"{self.BASE_URL}/v1/pay-order/seller/product-orders/last-changed-statuses",
                headers=self._headers(),
                params={
                    "lastChangedFrom": window_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "lastChangedTo":   window_end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "productOrderStatuses": status,
                    "limitCount": 100,
                },
            )
            resp.raise_for_status()
            changed = resp.json().get("lastChangeStatuses", [])
            product_order_ids.extend(item["productOrderId"] for item in changed)
            window_end = window_start
            remaining -= window

        if not product_order_ids:
            return []

        resp = requests.post(
            f"{self.BASE_URL}/v1/pay-order/seller/product-orders/query",
            headers=self._headers(),
            json={"productOrderIds": product_order_ids},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

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
                    }
                ]
            },
        )
        resp.raise_for_status()
        return resp.json()

    def set_product_sale_status(self, product_id: str, on_sale: bool):
        """상품 판매 상태 변경 (품절 시 판매중지)"""
        resp = requests.put(
            f"{self.BASE_URL}/v2/products/{product_id}",
            headers=self._headers(),
            json={"saleStatus": "ON_SALE" if on_sale else "SUSPENSION"},
        )
        resp.raise_for_status()
        return resp.json()

    def get_product(self, product_id: str) -> dict:
        """상품 정보 조회"""
        resp = requests.get(
            f"{self.BASE_URL}/v2/products/{product_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def get_products(self, size: int = 100) -> list:
        """판매자 상품 목록 조회"""
        resp = requests.get(
            f"{self.BASE_URL}/v2/products",
            headers=self._headers(),
            params={"size": size},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("simpleProducts", data.get("contents", data.get("products", [])))

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
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
