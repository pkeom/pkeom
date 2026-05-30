"""도매꾹 API 클라이언트 (공식 OpenAPI v4.1)

인증:       https://www.domeggook.com
상품/발주/송장: https://domemedb.domeggook.com/ssl/api/
응답 루트:  body["domeggook"]
"""
import requests


class DomaekkukAPI:
    AUTH_URL = "https://www.domeggook.com"
    API_URL = "https://domemedb.domeggook.com/ssl/api/"

    def __init__(self, api_key: str, user_id: str = "", password: str = ""):
        self.api_key = api_key
        self.user_id = user_id      # 발주 API에서 사용
        self.password = password    # 발주 API에서 사용

    def _params(self, extra: dict | None = None) -> dict:
        base = {
            "ver": "4.1",
            "aid": self.api_key,
            "om":  "json",
        }
        if extra:
            base.update(extra)
        return base

    def _root(self, resp: requests.Response) -> dict:
        """응답 JSON의 domeggook 루트 노드 반환"""
        resp.raise_for_status()
        return resp.json().get("domeggook", {})

    # ── 상품 조회 ────────────────────────────────────────────

    def get_product(self, product_no: str) -> dict:
        """상품 상세 정보 조회. 반환값: {title, price, stock, seller_id}"""
        resp = requests.get(
            self.API_URL,
            params=self._params({"mode": "getItemView", "no": product_no}),
        )
        root   = self._root(resp)
        basis  = root.get("basis", {})
        price  = root.get("price", {})
        qty    = root.get("qty", {})
        seller = root.get("seller", {})
        return {
            "title":     basis.get("title", ""),
            "price":     int(price.get("dome") or price.get("supply") or 0),
            "stock":     int(qty.get("inventory", 0)),
            "seller_id": seller.get("id", ""),
        }

    def get_stock(self, product_no: str) -> int:
        """재고 수량 조회"""
        return self.get_product(product_no)["stock"]

    def search_products(self, keyword: str, market: str = "dome",
                        page: int = 1, size: int = 20) -> dict:
        """상품 목록 검색. 반환값: {total, items: [{no, title, price, seller_id}]}"""
        resp = requests.get(
            self.API_URL,
            params=self._params({
                "mode":   "getItemList",
                "market": market,
                "kw":     keyword,
                "pg":     page,
                "sz":     size,
            }),
        )
        root   = self._root(resp)
        header = root.get("header", {})
        items  = root.get("list", {}).get("item", [])
        if isinstance(items, dict):
            items = [items]
        return {
            "total": header.get("numberOfItems", 0),
            "items": [
                {
                    "no":        item.get("no", ""),
                    "title":     item.get("title", ""),
                    "price":     int(item.get("price") or 0),
                    "seller_id": item.get("id", ""),
                }
                for item in items
            ],
        }

    # ── 발주 ─────────────────────────────────────────────────

    def place_order(self, product_no: str, quantity: int, shipping_info: dict,
                    *, dry_run: bool = False) -> dict:
        """발주 요청. 반환값: {"order_no": "도매처발주번호", ...}

        dry_run=True: 실제 API 호출 없이 즉시 가짜 발주번호 반환 (테스트/시뮬레이션용).
        ※ 도매꾹 addOrder API는 Private API(승인 필요)입니다.
          실제 필드명은 발급받은 API 문서에서 확인 후 아래 data 딕셔너리를 조정하세요.
        """
        if dry_run:
            return {"order_no": f"[DRY_RUN] prod={product_no} qty={quantity}"}
        resp = requests.post(
            self.API_URL,
            data={                          # form-encoded (not JSON)
                "ver":      "4.1",
                "mode":     "addOrder",
                "aid":      self.api_key,
                "uid":      self.user_id,
                "pwd":      self.password,
                "om":       "json",
                "no":       product_no,     # 상품번호
                "cnt":      quantity,       # 수량
                "rtNm":     shipping_info["name"],      # 수령인명
                "rtPh":     shipping_info["phone"],     # 수령인 연락처
                "rtZip":    shipping_info["zipcode"],   # 우편번호
                "rtAddr":   shipping_info["address"],   # 주소
                "rtMsg":    shipping_info.get("memo", ""),  # 배송 메모
            },
        )
        return self._root(resp)

    def cancel_order(self, order_no: str) -> dict:
        """발주 취소.

        ※ 도매꾹 주문 API는 Private API(별도 승인 필요)이므로
          실제 mode명·파라미터명은 승인 후 발급된 문서와 대조하여 수정하세요.
          배송 시작 전에만 취소 가능. 이미 발송된 경우 API가 오류를 반환합니다.
        """
        resp = requests.post(
            self.API_URL,
            data={
                "ver":      "4.1",
                "mode":     "cancelOrder",   # Private API 문서 확인 후 수정
                "aid":      self.api_key,
                "uid":      self.user_id,
                "pwd":      self.password,
                "om":       "json",
                "order_no": order_no,        # 파라미터명 확인 필요
            },
        )
        root = self._root(resp)
        err = root.get("error") or root.get("errCode") or root.get("errMsg")
        if err:
            raise RuntimeError(f"도매꾹 발주 취소 실패: {err}")
        return root

    def get_order_tracking(self, order_no: str) -> dict:
        """발주 건 송장 정보 조회. 반환값: {order_no, delivery_company, tracking_number}
        ※ 실제 필드명은 API 승인 후 문서에서 확인 필요. 아래는 일반적 후보를 모두 시도.
        """
        resp = requests.get(
            self.API_URL,
            params=self._params({"mode": "getOrderInfo", "order_no": order_no}),
        )
        root  = self._root(resp)
        order = root.get("order", root)
        return {
            "order_no": order_no,
            "delivery_company": (
                order.get("dlvCom") or order.get("delivery_company")
                or order.get("deliveryCom") or order.get("courierName") or ""
            ),
            "tracking_number": (
                order.get("invoice") or order.get("tracking_number")
                or order.get("invoiceNo") or order.get("trackingNo") or ""
            ),
        }
