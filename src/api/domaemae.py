"""도매매(도매꾹) 웹 스크래핑 클라이언트"""
import re
import requests
from bs4 import BeautifulSoup


class DomaemaeClient:
    AUTH_URL = "https://www.domeggook.com"
    API_URL = "https://domeme.domeggook.com"

    def __init__(self, user_id: str, password: str):
        self.user_id = user_id
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        self._logged_in = False

    def login(self):
        resp = self.session.post(
            f"{self.AUTH_URL}/ssl/member/mem_loginOk.php",
            data={"user_id": self.user_id, "user_pw": self.password},
        )
        resp.raise_for_status()
        self._logged_in = True

    def _ensure_login(self):
        if not self._logged_in:
            self.login()

    def get_stock(self, product_id: str) -> int:
        """재고 수량 조회"""
        return self.get_product(product_id).get("stock", 0)

    def get_product(self, product_id: str) -> dict:
        """상품 페이지에서 가격·재고 파싱"""
        self._ensure_login()
        resp = self.session.get(f"{self.API_URL}/s/{product_id}")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 가격: 로그인 후 #lNotDiscountAmtBox 안의 .lPrice에 표시
        price_tag = soup.select_one("#lNotDiscountAmtBox .lPrice")
        price = None
        if price_tag:
            m = re.search(r"[\d,]+", price_tag.get_text())
            if m:
                price = int(m.group().replace(",", ""))

        # 재고: tr.lInfoQty > td.lInfoItemContent 텍스트에서 숫자 추출 ("549,945개" 형식)
        stock_tag = soup.select_one("tr.lInfoQty td.lInfoItemContent")
        stock = 0
        if stock_tag:
            m = re.search(r"[\d,]+", stock_tag.get_text())
            if m:
                stock = int(m.group().replace(",", ""))

        return {
            "product_id": product_id,
            "price": price,
            "stock": stock,
        }

    def place_order(self, product_id: str, quantity: int, shipping_info: dict) -> str:
        """장바구니 담기 후 주문 처리 — 주문번호 반환"""
        self._ensure_login()
        self.session.post(
            f"{self.API_URL}/cart/add.php",
            data={"product_id": product_id, "count": quantity},
        )
        resp = self.session.post(
            f"{self.API_URL}/order/process.php",
            data={
                "receiver_name": shipping_info["name"],
                "receiver_phone": shipping_info["phone"],
                "receiver_addr": shipping_info["address"],
                "receiver_zipcode": shipping_info["zipcode"],
                "memo": shipping_info.get("memo", ""),
            },
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        order_no_tag = soup.select_one(".order_no")
        return order_no_tag.text.strip() if order_no_tag else ""

    def get_order_tracking(self, order_no: str) -> dict:
        """주문 상세에서 송장 정보 파싱"""
        self._ensure_login()
        resp = self.session.get(f"{self.API_URL}/order/detail.php?no={order_no}")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        company_tag = soup.select_one(".delivery_company")
        tracking_tag = soup.select_one(".tracking_number")
        return {
            "order_no": order_no,
            "delivery_company": company_tag.text.strip() if company_tag else "",
            "tracking_number": tracking_tag.text.strip() if tracking_tag else "",
        }
