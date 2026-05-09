"""도매매(도매꾹) 웹 스크래핑 클라이언트 — 브라우저 쿠키 인증"""
import re
import requests
from bs4 import BeautifulSoup


class DomaemaeCookieExpiredError(Exception):
    """도매매 로그인 쿠키 만료 — update_cookie.py 실행 필요"""


class DomaemaeClient:
    API_URL = "https://domeme.domeggook.com"

    def __init__(self, cookies: dict | None = None, **_):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
            "Referer": "https://domeggook.com/",
        })
        # cookiejar 도메인 매칭 로직을 우회하기 위해 Cookie 헤더를 직접 주입
        if cookies:
            self.session.headers["Cookie"] = "; ".join(
                f"{k}={v}" for k, v in cookies.items()
            )

    def _check_session(self, final_url: str):
        """로그인 필요 페이지에서 인증 만료 감지. 만료 시 DomaemaeCookieExpiredError 발생."""
        if "mem_login" in final_url or "mem_formLogin" in final_url:
            raise DomaemaeCookieExpiredError(
                "도매매 쿠키 만료(로그인 페이지 리다이렉트) — update_cookie.py를 실행해 쿠키를 갱신하세요."
            )

    def get_stock(self, product_id: str) -> int:
        """재고 수량 조회"""
        return self.get_product(product_id).get("stock", 0)

    def get_product(self, product_id: str) -> dict:
        """상품 페이지에서 가격·재고 파싱"""
        resp = self.session.get(f"{self.API_URL}/s/{product_id}")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 가격은 JS 변수에 삽입됨 (HTML 요소는 JS 렌더링 후 생성되므로 정적 파싱 불가)
        # ENP_VAR.collect.price = "22600"  ← 트래킹 픽셀 변수, 가장 안정적
        # baseAmtDome: 22600               ← 상태관리 store 변수 (fallback)
        price = None
        m = re.search(r'ENP_VAR\.collect\.price\s*=\s*["\'](\d+)["\']', resp.text)
        if not m:
            m = re.search(r'baseAmtDome\s*:\s*(\d+)', resp.text)
        if m:
            price = int(m.group(1))

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
        self._check_session(resp.url)
        order_no_tag = soup.select_one(".order_no")
        return order_no_tag.text.strip() if order_no_tag else ""

    def get_order_tracking(self, order_no: str) -> dict:
        """주문 상세에서 송장 정보 파싱"""
        resp = self.session.get(f"{self.API_URL}/order/detail.php?no={order_no}")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        self._check_session(resp.url)

        company_tag = soup.select_one(".delivery_company")
        tracking_tag = soup.select_one(".tracking_number")
        return {
            "order_no": order_no,
            "delivery_company": company_tag.text.strip() if company_tag else "",
            "tracking_number": tracking_tag.text.strip() if tracking_tag else "",
        }
