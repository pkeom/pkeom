"""도매매(도매꾹) 웹 스크래핑 클라이언트 — 브라우저 쿠키 인증"""
import logging
import re
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


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

    # ── 정적 파싱 헬퍼 ──────────────────────────────────────────

    @staticmethod
    def _parse_seller_price(html: str) -> tuple[str, int]:
        """HTML에서 sellerId와 기본 가격 추출"""
        m = re.search(r'sellerId\s*:\s*["\']([^"\']+)["\']', html)
        seller_id = m.group(1) if m else ""
        # 가격은 JS 변수에 삽입됨
        m = re.search(r'ENP_VAR\.collect\.price\s*=\s*["\'](\d+)["\']', html)
        if not m:
            m = re.search(r'baseAmtDome\s*:\s*(\d+)', html)
        price = int(m.group(1)) if m else 0
        return seller_id, price

    @staticmethod
    def _parse_options(html: str) -> list[dict]:
        """HTML/JS에서 옵션 목록 추출. 반환값: [{"id": str, "name": str}]

        파싱 시도 순서:
          1. JS 객체 패턴 {"no":"...","nm":"..."}  (도매매 표준)
          2. JS 객체 패턴 {"optNo":"...","optNm":"..."}  (alternative)
          3. HTML <select> 태그 (fallback)
        """
        seen: set[str] = set()
        options: list[dict] = []

        def _add(oid: str, name: str):
            if oid and oid not in seen:
                options.append({"id": oid, "name": name})
                seen.add(oid)

        # 패턴 1: {"no":"12345","nm":"아이폰11",...}
        for m in re.finditer(
            r'"no"\s*:\s*"(\d+)"[^}]*?"nm"\s*:\s*"([^"]*?)"', html, re.DOTALL
        ):
            _add(m.group(1), m.group(2))

        # 패턴 2: {"optNo":"12345","optNm":"아이폰11",...}
        if not options:
            for m in re.finditer(
                r'"optNo"\s*:\s*"(\d+)"[^}]*?"optNm"\s*:\s*"([^"]*?)"', html, re.DOTALL
            ):
                _add(m.group(1), m.group(2))

        # 패턴 3: HTML <select> fallback
        if not options:
            soup = BeautifulSoup(html, "html.parser")
            for sel in soup.select("select"):
                attr = (
                    sel.get("name", "")
                    + sel.get("id", "")
                    + " ".join(sel.get("class", []))
                )
                if not re.search(r"opt", attr, re.IGNORECASE):
                    continue
                for tag in sel.find_all("option"):
                    val = tag.get("value", "").strip()
                    text = tag.get_text(strip=True)
                    if val and val not in ("0", ""):
                        _add(val, text)
                if options:
                    break

        return options

    @staticmethod
    def _match_option(options: list[dict], option_name: str) -> str | None:
        """옵션명으로 옵션 ID 검색.

        우선순위: 정확 일치 → 포함 관계(양방향).
        대소문자·양끝 공백 무시.
        """
        if not option_name or not options:
            return None
        normalized = option_name.strip().lower()
        for opt in options:
            if opt["name"].strip().lower() == normalized:
                return opt["id"]
        for opt in options:
            lower = opt["name"].strip().lower()
            if normalized in lower or lower in normalized:
                return opt["id"]
        return None

    # ── 공개 API ────────────────────────────────────────────────

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
        _, price = self._parse_seller_price(resp.text)

        stock_tag = soup.select_one("tr.lInfoQty td.lInfoItemContent")
        stock = 0
        if stock_tag:
            m = re.search(r"[\d,]+", stock_tag.get_text())
            if m:
                stock = int(m.group().replace(",", ""))

        return {
            "product_id": product_id,
            "price": price or None,
            "stock": stock,
        }

    def get_options(self, product_id: str) -> list[dict]:
        """상품 옵션 목록 반환. [{"id": str, "name": str}]

        옵션이 없는 상품이면 빈 리스트 반환.
        """
        resp = self.session.get(f"{self.API_URL}/s/{product_id}")
        resp.raise_for_status()
        return self._parse_options(resp.text)

    def _get_product_meta(self, product_id: str) -> tuple[str, int]:
        """상품 페이지에서 sellerId와 가격을 한 번의 요청으로 추출 — (seller_id, price)"""
        resp = self.session.get(f"{self.API_URL}/s/{product_id}")
        resp.raise_for_status()
        return self._parse_seller_price(resp.text)

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
                    *, option_name: str = "", dry_run: bool = False) -> str:
        """장바구니 담기 후 주문 처리 — 주문번호 반환

        shipping_info 필수 키: name, phone, zipcode, address, shop
        shipping_info 선택 키: address2, memo

        shop: 쇼핑몰 상호 — market=supply(도매매) 주문 시 서버 필수값.
              비워두면 '소비자 정보에 입력되지 않은 항목이 있습니다' 오류 발생.

        option_name: 스마트스토어 주문의 옵션명(예: '아이폰11', '라운드').
                     값이 있으면 도매매 상품 페이지에서 일치하는 옵션 ID를 찾아
                     장바구니 요청에 포함한다. 일치하는 옵션이 없으면 경고 후 옵션 없이 발주.

        dry_run=True: 장바구니 담기까지만 실행, 실제 결제·주문 없음.
                      장바구니 API 응답(JSON)을 문자열로 반환.
        """
        shop = (shipping_info.get("shop") or self.shop or "").strip()
        if not shop:
            raise ValueError(
                "쇼핑몰 상호(shop)가 설정되지 않았습니다. "
                "shipping_info['shop'] 또는 settings.yaml domaemae.shop 을 채워주세요."
            )

        # 상품 페이지를 1회 요청해 sellerId·가격·옵션 목록을 모두 추출
        page_resp = self.session.get(f"{self.API_URL}/s/{product_id}")
        page_resp.raise_for_status()
        html = page_resp.text

        seller_id, price = self._parse_seller_price(html)

        opt_id: str | None = None
        if option_name:
            options = self._parse_options(html)
            opt_id = self._match_option(options, option_name)
            if opt_id:
                logger.info("옵션 매칭 성공: '%s' → id=%s", option_name, opt_id)
            else:
                logger.warning(
                    "옵션 '%s'을 찾지 못함 (product=%s, 후보=%d건) — 옵션 없이 발주",
                    option_name, product_id, len(options),
                )

        m1, m2, m3 = self._split_phone(shipping_info["phone"])

        # ── 장바구니 담기 ───────────────────────────────────────
        # market=supply(도매매)는 소비자 배송지(cons[...])를 함께 전송해야 함.
        # JS 확인: lAddCart() 내 param['cons[...]'] 블록 참조.
        cart_data: dict = {
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
        }

        # 옵션이 확인된 경우 PHP 배열 형식으로 전송
        # 실제 파라미터명은 브라우저 네트워크 탭에서 확인 후 필요 시 수정
        if opt_id:
            cart_data["opt[0][no]"]  = opt_id
            cart_data["opt[0][qty]"] = quantity

        cart_resp = self.session.post(
            f"{self.CART_URL}/main/myBuy/order/my_cartIng.php",
            data=cart_data,
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
