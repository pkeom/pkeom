"""도매매(도매꾹) 웹 스크래핑 클라이언트 — 브라우저 쿠키 인증"""
import re
import requests
from bs4 import BeautifulSoup


class DomaemaeCookieExpiredError(Exception):
    """도매매 로그인 쿠키 만료 — update_cookie.py 실행 필요"""


class DomaemaeClient:
    API_URL  = "https://domeme.domeggook.com"
    CART_URL = "https://domeggook.com"

    def __init__(self, cookies: dict | None = None, shop: str = "", **_):
        self.shop = shop
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

    def _get_product_meta(self, product_id: str) -> tuple[str, int]:
        """상품 페이지에서 sellerId와 가격을 한 번의 요청으로 추출 — (seller_id, price)"""
        resp = self.session.get(f"{self.API_URL}/s/{product_id}")
        m = re.search(r'sellerId\s*:\s*["\']([^"\']+)["\']', resp.text)
        seller_id = m.group(1) if m else ""
        m = re.search(r'ENP_VAR\.collect\.price\s*=\s*["\'](\d+)["\']', resp.text)
        if not m:
            m = re.search(r'baseAmtDome\s*:\s*(\d+)', resp.text)
        price = int(m.group(1)) if m else 0
        return seller_id, price

    def _get_seller_id(self, product_id: str) -> str:
        """상품 페이지 JS에서 sellerId 추출"""
        return self._get_product_meta(product_id)[0]

    @staticmethod
    def _split_phone(phone: str) -> tuple[str, str, str]:
        """'010-1234-5678' 또는 '01012345678' → ('010','1234','5678')"""
        parts = re.split(r"[-\s]", phone.strip())
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        digits = re.sub(r"\D", "", phone)
        if len(digits) == 11:
            return digits[:3], digits[3:7], digits[7:]
        if len(digits) == 10:
            return digits[:3], digits[3:6], digits[6:]
        return digits, "", ""

    def place_order(self, product_id: str, quantity: int, shipping_info: dict,
                   *, dry_run: bool = False) -> str:
        """장바구니 담기 후 주문 처리 — 주문번호 반환

        shipping_info 필수 키: name, phone, zipcode, address, shop
        shipping_info 선택 키: address2, memo

        shop: 쇼핑몰 상호 — market=supply(도매매) 주문 시 서버 필수값.
              비워두면 '소비자 정보에 입력되지 않은 항목이 있습니다' 오류 발생.

        dry_run=True: 장바구니 담기까지만 실행, 실제 결제·주문 없음.
                      장바구니 API 응답(JSON)을 문자열로 반환.
        """
        shop = (shipping_info.get("shop") or self.shop or "").strip()
        if not shop:
            raise ValueError(
                "쇼핑몰 상호(shop)가 설정되지 않았습니다. "
                "shipping_info['shop'] 또는 settings.yaml domaemae.shop 을 채워주세요."
            )
        seller_id, price = self._get_product_meta(product_id)
        m1, m2, m3 = self._split_phone(shipping_info["phone"])

        # ── 장바구니 담기 ───────────────────────────────────────
        # market=supply(도매매)는 소비자 배송지(cons[...])를 함께 전송해야 함.
        # JS 확인: lAddCart() 내 param['cons[...]'] 블록 참조.
        cart_resp = self.session.post(
            f"{self.CART_URL}/main/myBuy/order/my_cartIng.php",
            data={
                "format":   "json",
                "mode":     "add",
                "market":   "supply",
                "no":       product_id,
                "sellerId": seller_id,
                "qty":      quantity,
                "memo":     shipping_info.get("memo", ""),
                "smp":      "",
                "amt":      price,
                "advcnt":   "",
                "isCoupon": "0",
                "dw":       "P",               # 선불배송(Prepaid)
                "cons[shop]":    shop,
                "cons[name]":    shipping_info["name"],
                "cons[post]":    shipping_info["zipcode"],
                "cons[addr1]":   shipping_info["address"],
                "cons[addr2]":   shipping_info.get("address2", ""),
                "cons[mobile1]": m1,
                "cons[mobile2]": m2,
                "cons[mobile3]": m3,
                "cons[phone1]":  "",
                "cons[phone2]":  "",
                "cons[phone3]":  "",
                "cons[deliReq]": shipping_info.get("memo", ""),
                "consSetAddrBook": "0",
            },
            headers={"Referer": f"{self.API_URL}/s/{product_id}"},
        )
        cart_resp.raise_for_status()
        self._check_session(cart_resp.url)

        if dry_run:
            try:
                return f"[DRY_RUN] cart response: {cart_resp.json()}"
            except Exception:
                return f"[DRY_RUN] HTTP {cart_resp.status_code} — {cart_resp.text[:200]}"

        # ── 실제 주문 처리 ──────────────────────────────────────
        resp = self.session.post(
            f"{self.CART_URL}/main/myBuy/order/my_orderInfoForm.php",
            data={
                "receiver_name":    shipping_info["name"],
                "receiver_phone":   shipping_info["phone"],
                "receiver_addr":    shipping_info["address"],
                "receiver_zipcode": shipping_info["zipcode"],
                "memo":             shipping_info.get("memo", ""),
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
