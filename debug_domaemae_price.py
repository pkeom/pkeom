"""도매매 로그인 성공 여부 확인 + 가격 관련 HTML 구조 출력"""
import os
import sys
import io
import re
import requests
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PRODUCT_ID = "64926509"
AUTH_URL   = "https://domeggook.com"
API_URL    = "https://domeme.domeggook.com"

USER_ID  = os.environ.get("DM_USER", "")
PASSWORD = os.environ.get("DM_PASS", "")

if not USER_ID or not PASSWORD:
    print("사용법: DM_USER=아이디 DM_PASS=비밀번호 python debug_domaemae_price.py")
    sys.exit(1)


def separator(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": AUTH_URL,
})

# ── 1. 로그인 ────────────────────────────────────────────────
separator("1. 로그인 시도")
login_url = f"{AUTH_URL}/main/member/mem_ing.php"
print(f"POST {login_url}")

resp = session.post(
    login_url,
    data={
        "mode": "mongoLogin",
        "encording": "utf8",
        "back": "",
        "extCookie": "",
        "id": USER_ID,
        "pass": PASSWORD,
    },
    allow_redirects=True,
)
print(f"HTTP 상태: {resp.status_code}")
print(f"최종 URL:  {resp.url}")

# 로그인 성공 여부 판별
login_html = resp.text
login_soup = BeautifulSoup(login_html, "html.parser")

# 실패 메시지 패턴 탐지
fail_keywords = ["비밀번호", "아이디", "로그인 실패", "잘못", "틀린", "error", "fail"]
page_text = login_soup.get_text()
failed = any(kw in page_text for kw in fail_keywords)

# 로그인 성공 시 세션 쿠키가 설정됨
cookies = {c.name: c.value for c in session.cookies}
print(f"세션 쿠키: {cookies}")

if failed:
    print("\n[FAIL] 로그인 실패 — 아이디/비밀번호 확인 필요")
    print("페이지 텍스트 앞부분:", page_text[:300])
    sys.exit(1)
else:
    print("\n[OK] 로그인 성공 (쿠키 설정됨)")

# ── 2. 로그인 후 상품 페이지 요청 ────────────────────────────
separator(f"2. 상품 페이지 요청: {API_URL}/s/{PRODUCT_ID}")
prod_resp = session.get(f"{API_URL}/s/{PRODUCT_ID}", allow_redirects=True)
print(f"HTTP 상태: {prod_resp.status_code}")
print(f"최종 URL:  {prod_resp.url}")

prod_soup = BeautifulSoup(prod_resp.text, "html.parser")

# ── 3. 로그인 상태 확인 ──────────────────────────────────────
separator("3. 로그인 상태 확인")
only_com_div = prod_soup.select_one("#lMsgOnlyCom")
if only_com_div:
    print(f"[주의] #lMsgOnlyCom 존재 → 가격 미표시: '{only_com_div.get_text(strip=True)}'")
else:
    print("[OK] #lMsgOnlyCom 없음 → 가격 표시 중")

# ── 4. 가격 관련 요소 전부 출력 ──────────────────────────────
separator("4. 가격 관련 HTML 요소")

targets = [
    "#lNotDiscountAmtBox",
    "#lNotDiscountAmtBox .lPrice",
    ".lInfoAmt",
    ".lInfoAmtWrap",
    ".lDiscountOrgAmt",
    ".lInfoPriceQty",
]
for sel in targets:
    tag = prod_soup.select_one(sel)
    if tag:
        print(f"\n[{sel}]")
        print(str(tag)[:600])
    else:
        print(f"\n[{sel}] → 없음")

# ── 5. 숫자+원 패턴 요소 모두 출력 ──────────────────────────
separator("5. 숫자+원 패턴 요소 (len<40)")
for tag in prod_soup.find_all(True):
    text = tag.get_text(strip=True)
    if re.search(r"\d[\d,]+원", text) and len(text) < 40:
        print(f"  {tag.name:6s} class={tag.get('class')} id={tag.get('id')} → {text}")

# ── 6. 재고 요소 확인 ────────────────────────────────────────
separator("6. 재고 요소 (tr.lInfoQty)")
qty_tag = prod_soup.select_one("tr.lInfoQty td.lInfoItemContent")
if qty_tag:
    m = re.search(r"[\d,]+", qty_tag.get_text())
    stock = int(m.group().replace(",", "")) if m else 0
    print(f"[OK] 재고: {stock}개")
    print(str(qty_tag)[:400])
else:
    print("[없음] tr.lInfoQty td.lInfoItemContent 요소 미발견")

# ── 7. 최종 파싱 결과 요약 ───────────────────────────────────
separator("7. 파싱 결과 요약")
price_tag = prod_soup.select_one("#lNotDiscountAmtBox .lPrice")
price = None
if price_tag:
    m = re.search(r"[\d,]+", price_tag.get_text())
    if m:
        price = int(m.group().replace(",", ""))

print(f"price : {price}")
print(f"stock : {stock if qty_tag else 0}")
