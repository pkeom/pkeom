"""네이버 커머스 API 택배사 코드 목록 조회 (읽기 전용 디버그)

실행:
    python scripts/debug_delivery_companies.py
"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from src.utils.config_loader import load_config
from src.api.smartstore import SmartstoreAPI

SEP = "=" * 70


def main():
    cfg = load_config()
    ss_cfg = {k: v for k, v in cfg["smartstore"].items()
              if k in ("client_id", "client_secret", "account_type")}
    api = SmartstoreAPI(**ss_cfg)

    url = f"{api.BASE_URL}/v1/pay-order/seller/delivery-companies"
    headers = api._headers()

    print(f"GET {url}")
    resp = requests.get(url, headers=headers, timeout=10)
    print(f"HTTP {resp.status_code}")
    print()

    if not resp.ok:
        print(f"[오류] {resp.text}")
        return

    try:
        data = resp.json()
    except Exception:
        print(f"[파싱 실패] raw:\n{resp.text}")
        return

    # 응답 구조에 따라 목록 추출
    companies = data if isinstance(data, list) else data.get("deliveryCompanies", data.get("contents", []))

    if not isinstance(companies, list):
        print(f"[예상 외 응답 구조] raw:\n{json.dumps(data, ensure_ascii=False, indent=2)}")
        return

    print(f"택배사 총 {len(companies)}개\n")
    print(f"{'코드':<20} {'택배사명'}")
    print("-" * 50)
    for c in sorted(companies, key=lambda x: str(x.get("code", x.get("deliveryCompanyCode", "")))):
        code = c.get("code") or c.get("deliveryCompanyCode") or c.get("id", "")
        name = c.get("name") or c.get("deliveryCompanyName") or c.get("label", "")
        print(f"{code:<20} {name}")

    # 롯데 관련 항목만 따로 강조
    print()
    print(SEP)
    lotte = [c for c in companies
             if "롯데" in str(c.get("name", "") or c.get("deliveryCompanyName", ""))]
    if lotte:
        print(f"[롯데 관련 항목 {len(lotte)}개]")
        for c in lotte:
            print(f"  {json.dumps(c, ensure_ascii=False)}")
    else:
        print("[롯데 관련 항목 없음]")


if __name__ == "__main__":
    main()
