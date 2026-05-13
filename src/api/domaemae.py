"""도매매(도매꾹) 웹 스크래핑 클라이언트 — 브라우저 쿠키 인증"""
import logging
import re
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

RETAIL_URL = "https://domeggook.com"   # 소매(dome) 도메인 — 옵션 fallback 용도


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

    @staticmethod
    def _decode(resp: requests.Response) -> str:
        """EUC-KR 페이지를 Unicode 문자열로 디코딩"""
        return resp.content.decode("euc-kr", errors="replace")

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
        """HTML에서 옵션 목록 추출. 반환값: [{"id": str, "name": str}]

        도매매 상품 페이지는 ItemOptionController 초기화 블록 안에
        옵션 데이터를 아래 형식으로 embed한다 (EUC-KR 디코딩 후 Unicode):

          "CODE":{"name":"옵션명","dom":1,"sup":1,"hid":0,...}

        - CODE  : selectOpt[CODE] 형식으로 카트 POST에 전달하는 옵션 코드
                  단일 옵션 → "0", "1", ...
                  조합 옵션 → "6_00", "10_01", ...
        - sup=1 : B2B(도매/supply) 마켓에서 이용 가능
        - hid=2 : 완전 숨김 처리 → 제외
        - hid=1 : 품절·비노출 → 목록에는 포함(주문 실패는 서버가 처리)
        """
        seen: set[str] = set()
        options: list[dict] = []

        # 옵션 객체는 중첩이 없는 flat JSON → [^{}]+ 로 전체 추출 가능
        # 패턴: "CODE":{"name":"NAME","dom":N,...,"sup":N,...,"hid":N,...}
        for m in re.finditer(r'"(\d[\d_]*)"\s*:\s*(\{[^{}]+\})', html):
            code    = m.group(1)
            obj_str = m.group(2)

            name_m = re.search(r'"name"\s*:\s*"([^"]*)"', obj_str)
            sup_m  = re.search(r'"sup"\s*:\s*(\d+)',      obj_str)
            hid_m  = re.search(r'"hid"\s*:\s*(\d+)',      obj_str)

            if not name_m:
                continue

            name = name_m.group(1)
            sup  = int(sup_m.group(1)) if sup_m else 0
            hid  = int(hid_m.group(1)) if hid_m else 0

            if sup == 1 and hid != 2 and code not in seen:
                options.append({"id": code, "name": name})
                seen.add(code)

        return options

    @staticmethod
    def _match_option(options: list[dict], option_name: str) -> str | None:
        """옵션명으로 옵션 ID 검색.

        우선순위:
          1. 정확 일치 (대소문자·공백 무시)
          2. 포함 관계 (양방향) — "아이폰11"이 "아이폰11/라운드"에 포함되는 경우 등
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

    # ── 공개 API ────────────────────────────────────────────────

    def get_stock(self, product_id: str) -> int:
        """재고 수량 조회"""
        return self.get_product(product_id).get("stock", 0)

    def get_product(self, product_id: str) -> dict:
        """상품 페이지에서 가격·재고 파싱"""
        resp = self.session.get(f"{self.API_URL}/s/{product_id}")
        resp.raise_for_status()
        html = self._decode(resp)
        soup = BeautifulSoup(html, "html.parser")

        _, price = self._parse_seller_price(html)

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

        B2B 페이지(domeme.domeggook.com)를 먼저 확인한다.
        로그인 쿠키가 없거나 B2B 옵션이 비어 있으면
        소매 페이지(domeggook.com)로 재시도 — 동일 상품 번호로 접근 가능.

        옵션 없는 상품(단일 상품)은 빈 리스트를 반환한다.
        """
        resp = self.session.get(f"{self.API_URL}/s/{product_id}")
        resp.raise_for_status()
        options = self._parse_options(self._decode(resp))

        if not options:
            options = self._fetch_retail_options(product_id)

        logger.debug("get_options(%s): %d건", product_id, len(options))
        return options

    def _fetch_retail_options(self, product_id: str) -> list[dict]:
        """소매 페이지(domeggook.com)에서 옵션 목록 조회 — B2B fallback"""
        try:
            resp = self.session.get(f"{RETAIL_URL}/{product_id}", timeout=10)
            resp.raise_for_status()
            return self._parse_options(self._decode(resp))
        except Exception as e:
            logger.debug("소매 페이지 옵션 조회 실패 (product=%s): %s", product_id, e)
            return []

    def _get_product_meta(self, product_id: str) -> tuple[str, int]:
        """상품 페이지에서 sellerId와 가격을 한 번의 요청으로 추출 — (seller_id, price)"""
        resp = self.session.get(f"{self.API_URL}/s/{product_id}")
        resp.raise_for_status()
        return self._parse_seller_price(self._decode(resp))

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

        option_name: 스마트스토어 주문의 옵션명(예: '아이폰11', '아이폰11/라운드').
                     값이 있으면 상품 페이지에서 일치하는 옵션 코드를 찾아
                     selectOpt[{code}] 파라미터로 카트 POST에 포함한다.
                     일치 옵션이 없으면 경고 로그 후 옵션 없이 발주.

        dry_run=True: 장바구니 담기까지만 실행, 실제 결제·주문 없음.
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
        html = self._decode(page_resp)

        seller_id, price = self._parse_seller_price(html)

        opt_id: str | None = None
        if option_name:
            options = self._parse_options(html)
            if not options:
                # 쿠키 미설정·B2B 옵션 비공개 시 소매 페이지로 재시도
                options = self._fetch_retail_options(product_id)

            opt_id = self._match_option(options, option_name)
            if opt_id:
                logger.info("옵션 매칭 성공: '%s' → code=%s", option_name, opt_id)
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

        # 옵션 코드 확인 시 selectOpt[{code}] 파라미터 추가
        # JS lAddCart(): param['selectOpt[' + resultItem.code + ']'] = resultItem.qty
        if opt_id:
            cart_data[f"selectOpt[{opt_id}]"] = quantity

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
        soup = BeautifulSoup(self._decode(resp), "html.parser")
        self._check_session(resp.url)
        order_no_tag = soup.select_one(".order_no")
        return order_no_tag.text.strip() if order_no_tag else ""

    def get_order_tracking(self, order_no: str) -> dict:
        """주문 상세에서 송장 정보 파싱"""
        resp = self.session.get(f"{self.API_URL}/order/detail.php?no={order_no}")
        resp.raise_for_status()
        soup = BeautifulSoup(self._decode(resp), "html.parser")
        self._check_session(resp.url)

        company_tag = soup.select_one(".delivery_company")
        tracking_tag = soup.select_one(".tracking_number")
        return {
            "order_no": order_no,
            "delivery_company": company_tag.text.strip() if company_tag else "",
            "tracking_number": tracking_tag.text.strip() if tracking_tag else "",
        }
