"""도매매 웹 스크래핑 클라이언트 (별도 공식 API 미제공 시 사용)"""
import requests
from bs4 import BeautifulSoup


class DomaemaeClient:
    BASE_URL = "https://www.domaemae.co.kr"

    def __init__(self, user_id: str, password: str):
        self.user_id = user_id
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        self._logged_in = False

    def login(self):
        resp = self.session.post(
            f"{self.BASE_URL}/member/login_ok.php",
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
        resp = self.session.get(f"{self.BASE_URL}/product/view.php?no={product_id}")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        price_tag = soup.select_one(".price strong")
        stock_tag = soup.select_one(".stock_count")

        return {
            "product_id": product_id,
            "price": int(price_tag.text.replace(",", "").strip()) if price_tag else None,
            "stock": int(stock_tag.text.strip()) if stock_tag else 0,
        }

    def place_order(self, product_id: str, quantity: int, shipping_info: dict) -> str:
        """장바구니 담기 후 주문 처리 — 주문번호 반환"""
        self._ensure_login()
        self.session.post(
            f"{self.BASE_URL}/cart/add.php",
            data={"product_id": product_id, "count": quantity},
        )
        resp = self.session.post(
            f"{self.BASE_URL}/order/process.php",
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
        resp = self.session.get(f"{self.BASE_URL}/order/detail.php?no={order_no}")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        company_tag = soup.select_one(".delivery_company")
        tracking_tag = soup.select_one(".tracking_number")
        return {
            "order_no": order_no,
            "delivery_company": company_tag.text.strip() if company_tag else "",
            "tracking_number": tracking_tag.text.strip() if tracking_tag else "",
        }
