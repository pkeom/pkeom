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

    def get_options(self, product_no: str) -> list[dict]:
        """상품 옵션 목록 조회. 반환값: [{"id": str, "name": str}]

        getItemView 응답의 selectOpt 노드를 파싱한다.
        """
        import logging as _log
        resp = requests.get(
            self.API_URL,
            params=self._params({"mode": "getItemView", "no": product_no}),
        )
        root       = self._root(resp)
        select_opt = root.get("selectOpt") or {}
        if not isinstance(select_opt, dict):
            return []
        options: list[dict] = []
        seen: set[str] = set()
        for code, info in select_opt.items():
            if not isinstance(info, dict):
                continue
            if int(info.get("hid", 0)) == 2:   # hid=2: 완전 숨김 제외
                continue
            name = str(info.get("name", "")).strip()
            if name and code not in seen:
                options.append({"id": str(code), "name": name})
                seen.add(code)
        _log.getLogger(__name__).debug(
            "도매꾹 옵션 목록: product=%s, count=%d", product_no, len(options)
        )
        return options

    def place_order(self, product_no: str, quantity: int, shipping_info: dict,
                    *, supplier_option_id: str = "", dry_run: bool = False) -> dict:
        """발주 요청. 반환값: {"order_no": "도매처발주번호", ...}

        supplier_option_id: 매핑에 저장된 도매꾹 옵션 코드.
          값이 있으면 addOrder 요청의 'opt' 필드로 전달한다.
          없으면 opt 필드를 생략하고 옵션 없이 발주한다.

        dry_run=True: 실제 API 호출 없이 즉시 가짜 발주번호 반환.
        ※ 도매꾹 addOrder API는 Private API(승인 필요)입니다.
          실제 필드명은 발급받은 API 문서에서 확인 후 조정하세요.
          (현재 옵션 필드명 'opt' 는 추정값 — API 승인 후 검증 필요)
        """
        import logging as _log
        _logger = _log.getLogger(__name__)

        if dry_run:
            opt_tag = f" opt={supplier_option_id}" if supplier_option_id else ""
            return {"order_no": f"[DRY_RUN] prod={product_no} qty={quantity}{opt_tag}"}

        data: dict = {
            "ver":      "4.1",
            "mode":     "addOrder",
            "aid":      self.api_key,
            "uid":      self.user_id,
            "pwd":      self.password,
            "om":       "json",
            "no":       product_no,                       # 상품번호
            "cnt":      quantity,                         # 수량
            "rtNm":     shipping_info["name"],            # 수령인명
            "rtPh":     shipping_info["phone"],           # 수령인 연락처
            "rtZip":    shipping_info["zipcode"],         # 우편번호
            "rtAddr":   shipping_info["address"],         # 주소
            "rtMsg":    shipping_info.get("memo", ""),    # 배송 메모
        }

        if supplier_option_id:
            data["opt"] = supplier_option_id             # 옵션 코드 전달
            _logger.info(
                "도매꾹 발주 옵션 전달: product=%s, opt=%s", product_no, supplier_option_id
            )
        else:
            _logger.debug("도매꾹 발주 옵션 없음: product=%s", product_no)

        resp = requests.post(self.API_URL, data=data)
        return self._root(resp)

    def cancel_order(self, order_no: str) -> dict:
        """발주 취소 (setOrdDeny).

        배송 시작 전에만 취소 가능. 이미 발송된 경우 API가 오류를 반환합니다.
        """
        resp = requests.post(
            self.API_URL,
            data={
                "ver":      "4.1",
                "mode":     "setOrdDeny",
                "aid":      self.api_key,
                "uid":      self.user_id,
                "pwd":      self.password,
                "om":       "json",
                "order_no": order_no,
            },
        )
        root = self._root(resp)
        err = root.get("error") or root.get("errCode") or root.get("errMsg")
        if err:
            raise RuntimeError(f"도매꾹 발주 취소 실패: {err}")
        return root

    def get_cancel_result(self, order_no: str) -> str:
        """setOrdDeny 후 도매처 취소 처리 결과 확인 (getOrderInfo).

        반환: 'APPROVED' | 'REJECTED' | 'PENDING'
        ※ 실제 status 값은 API 문서 확인 후 아래 키워드 목록 조정 필요.
        """
        try:
            resp = requests.get(
                self.API_URL,
                params=self._params({"mode": "getOrderInfo", "order_no": order_no}),
            )
            root   = self._root(resp)
            order  = root.get("order", root)
            status = (
                str(order.get("status",       ""))
                or str(order.get("ordStatus", ""))
                or str(order.get("cancelStatus", ""))
            ).lower()
            if any(k in status for k in ["cancel_ok", "취소승인", "cancel_accept", "deny_ok", "refund"]):
                return "APPROVED"
            if any(k in status for k in ["cancel_deny", "취소거부", "deny_fail", "cancel_reject"]):
                return "REJECTED"
        except Exception as e:
            import logging as _log
            _log.getLogger(__name__).warning("도매꾹 취소 결과 조회 실패 (%s): %s", order_no, e)
        return "PENDING"

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
