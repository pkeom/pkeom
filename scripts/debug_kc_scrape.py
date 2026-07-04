"""KC 인증 스크래핑 단독 확인 (읽기 전용)

실행:
    python scripts/debug_kc_scrape.py 58091305
"""
import io, sys, json
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from bs4 import BeautifulSoup

from src.utils.config_loader import load_config
from src.api.smartstore import SmartstoreAPI
from src.core.product_register import _scrape_domaemae, _fetch_kc_cert_detail

SEP = "=" * 60

def main():
    product_id = sys.argv[1] if len(sys.argv) > 1 else "58091305"
    page_url = f"https://domeme.domeggook.com/s/{product_id}"

    # ── 1. 페이지 직접 fetch → lCert 블록 개수 확인 ──────────────
    print(f"[fetch] {page_url}")
    resp = requests.get(page_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://domeme.domeggook.com/",
    }, timeout=15)
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "lxml")

    cert_blocks = soup.select("div.lCert.lHasImg")
    print(f"\n[div.lCert.lHasImg 블록 수] {len(cert_blocks)}개")
    for i, blk in enumerate(cert_blocks):
        title_el = blk.select_one("div.lCertTitle")
        num_el   = blk.select_one("div.lCertNum")
        link_el  = blk.select_one("div.lCertNum a[href]")
        print(f"  [{i}] title={title_el.get_text(strip=True) if title_el else ''!r}"
              f"  num={num_el.get_text(strip=True)[:60] if num_el else ''!r}"
              f"  link={link_el['href'] if link_el else ''}")

    # ── 2. _scrape_domaemae 호출 → KC 필드값 ─────────────────────
    print(f"\n{SEP}")
    print("[_scrape_domaemae 실행]")
    scraped = _scrape_domaemae(product_id)

    kc_no     = scraped.get("kc_cert_no", "")
    kc_type   = scraped.get("kc_cert_type", "")
    kc_agency = scraped.get("kc_cert_agency", "")
    category  = scraped.get("category_id", "")

    print(f"  kc_cert_no     = {kc_no!r}     {'← 비어있음' if not kc_no else ''}")
    print(f"  kc_cert_type   = {kc_type!r}   {'← 비어있음' if not kc_type else ''}")
    print(f"  kc_cert_agency = {kc_agency!r} {'← 비어있음' if not kc_agency else ''}")
    print(f"  category_id    = {category!r}")

    # ── 3. get_kc_cert_status → kc_cert_info_id ──────────────────
    print(f"\n{SEP}")
    print("[get_kc_cert_status 호출]")
    if not category:
        print("  category_id 없음 → 조회 불가")
        return

    cfg    = load_config()
    ss_cfg = {k: v for k, v in cfg["smartstore"].items()
              if k in ("client_id", "client_secret", "account_type")}
    api = SmartstoreAPI(**ss_cfg)

    kc_required, kc_info_id = api.get_kc_cert_status(category, kc_type)
    print(f"  category_id    = {category!r}")
    print(f"  cert_type_hint = {kc_type!r}")
    print(f"  kc_required    = {kc_required}")
    print(f"  kc_cert_info_id= {kc_info_id}  {'← 0=미확인/실패' if not kc_info_id else '← 성공'}")

    print(f"\n{SEP}")
    print("[최종 판정]")
    print(f"  kc_cert_no     = {kc_no!r}")
    print(f"  kc_cert_agency = {kc_agency!r}")
    print(f"  kc_cert_info_id= {kc_info_id}")
    all_ok = bool(kc_no and kc_agency and kc_info_id)
    print(f"  → 등록 가능: {'YES' if all_ok else 'NO (셋 중 하나 이상 비어있음)'}")

if __name__ == "__main__":
    main()
