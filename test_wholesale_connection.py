"""도매꾹/도매매 API 연결 테스트"""
import sys
import io
import json
import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_URL = "https://domeggook.com/ssl/api/"
API_KEY  = "749082e66071934ef74df9d6e6511cda"
SEARCH_KW = "티셔츠"


def separator(title: str):
    print(f"\n{'='*55}")
    print(f"  {title}")
    print(f"{'='*55}")


def _call(params: dict) -> tuple[int, dict | None]:
    """API 호출 공통 함수 — (status_code, body) 반환"""
    resp = requests.get(BASE_URL, params=params, timeout=10)
    try:
        body = resp.json()
    except Exception:
        body = None
    return resp.status_code, body


def _print_product(item: dict, idx: int):
    print(f"  [{idx}] {item.get('title', 'N/A')[:40]}")
    print(f"       상품번호: {item.get('no', 'N/A')}")
    print(f"       가격:     {item.get('price', 'N/A'):,}원" if isinstance(item.get('price'), int)
          else f"       가격:     {item.get('price', 'N/A')}")
    print(f"       판매자:   {item.get('nick') or item.get('id', 'N/A')}")


# ──────────────────────────────────────────────────────────
# 1. 도매꾹 상품 목록 조회
# ──────────────────────────────────────────────────────────
def test_domaekkuk_list() -> str | None:
    """도매꾹 상품 검색 — 성공 시 첫 번째 상품번호 반환"""
    separator("1. 도매꾹 상품 목록 조회 (keyword: " + SEARCH_KW + ")")
    params = {
        "ver":    "4.1",
        "mode":   "getItemList",
        "aid":    API_KEY,
        "market": "dome",
        "om":     "json",
        "kw":     SEARCH_KW,
        "sz":     5,
        "pg":     1,
    }
    print(f"  요청 URL: {BASE_URL}")
    print(f"  파라미터: {params}")

    status, body = _call(params)
    print(f"  HTTP 상태: {status}")

    if status != 200 or body is None:
        print(f"[FAIL] 응답 본문: {body}")
        return None

    root   = body.get("domeggook", body)
    header = root.get("header", {})
    items  = root.get("list", {}).get("item", [])
    if isinstance(items, dict):  # 단건일 때 dict로 오는 경우 대응
        items = [items]

    total = header.get("numberOfItems", "?")
    print(f"[OK] 전체 검색 건수: {total}건 / 이번 페이지: {len(items)}건")

    for i, item in enumerate(items, 1):
        _print_product(item, i)

    return items[0].get("no") if items else None


# ──────────────────────────────────────────────────────────
# 2. 도매꾹 상품 상세 조회
# ──────────────────────────────────────────────────────────
def test_domaekkuk_detail(product_no: str) -> bool:
    separator(f"2. 도매꾹 상품 상세 조회 (no={product_no})")
    params = {
        "ver":  "4.1",
        "mode": "getItemView",
        "aid":  API_KEY,
        "om":   "json",
        "no":   product_no,
    }
    status, body = _call(params)
    print(f"  HTTP 상태: {status}")

    if status != 200 or body is None:
        print(f"[FAIL] 응답 본문: {body}")
        return False

    root   = body.get("domeggook", body)
    basis  = root.get("basis", {})
    price  = root.get("price", {})
    qty    = root.get("qty", {})
    seller = root.get("seller", {})
    print(f"[OK] 상품명:  {basis.get('title', 'N/A')}")
    print(f"     가격:    {price.get('dome', 'N/A')}원 (도매꾹) / {price.get('supply', 'N/A')}원 (도매매)")
    print(f"     재고:    {qty.get('inventory', 'N/A')}개")
    print(f"     판매자:  {seller.get('nick') or seller.get('id', 'N/A')}")
    return True


# ──────────────────────────────────────────────────────────
# 3. 도매매 상품 목록 조회
# ──────────────────────────────────────────────────────────
def test_domaemae_list() -> str | None:
    """도매매 상품 검색 — 성공 시 첫 번째 상품번호 반환"""
    separator("3. 도매매 상품 목록 조회 (keyword: " + SEARCH_KW + ")")
    params = {
        "ver":    "4.1",
        "mode":   "getItemList",
        "aid":    API_KEY,
        "market": "supply",
        "om":     "json",
        "kw":     SEARCH_KW,
        "sz":     5,
        "pg":     1,
    }
    status, body = _call(params)
    print(f"  HTTP 상태: {status}")

    if status != 200 or body is None:
        print(f"[FAIL] 응답 본문: {body}")
        return None

    root   = body.get("domeggook", body)
    header = root.get("header", {})
    items  = root.get("list", {}).get("item", [])
    if isinstance(items, dict):
        items = [items]

    total = header.get("numberOfItems", "?")
    print(f"[OK] 전체 검색 건수: {total}건 / 이번 페이지: {len(items)}건")

    for i, item in enumerate(items, 1):
        _print_product(item, i)

    return items[0].get("no") if items else None


# ──────────────────────────────────────────────────────────
# 4. 도매매 상품 상세 조회
# ──────────────────────────────────────────────────────────
def test_domaemae_detail(product_no: str) -> bool:
    separator(f"4. 도매매 상품 상세 조회 (no={product_no})")
    params = {
        "ver":    "4.1",
        "mode":   "getItemView",
        "aid":    API_KEY,
        "market": "supply",
        "om":     "json",
        "no":     product_no,
    }
    status, body = _call(params)
    print(f"  HTTP 상태: {status}")

    if status != 200 or body is None:
        print(f"[FAIL] 응답 본문: {body}")
        return False

    root   = body.get("domeggook", body)
    basis  = root.get("basis", {})
    price  = root.get("price", {})
    qty    = root.get("qty", {})
    seller = root.get("seller", {})
    print(f"[OK] 상품명:  {basis.get('title', 'N/A')}")
    print(f"     가격:    {price.get('dome', 'N/A')}원 (도매꾹) / {price.get('supply', 'N/A')}원 (도매매)")
    print(f"     재고:    {qty.get('inventory', 'N/A')}개")
    print(f"     판매자:  {seller.get('nick') or seller.get('id', 'N/A')}")
    return True


# ──────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────
def main():
    from datetime import datetime
    print("\n[도매꾹/도매매 API 연결 테스트]")
    print(f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"API Key  : {API_KEY[:8]}{'*' * (len(API_KEY) - 8)}")

    results = []

    # 도매꾹
    first_no = test_domaekkuk_list()
    results.append(("도매꾹 목록 조회", first_no is not None))

    if first_no:
        ok = test_domaekkuk_detail(first_no)
        results.append(("도매꾹 상세 조회", ok))
    else:
        results.append(("도매꾹 상세 조회", False))

    # 도매매
    first_no_supply = test_domaemae_list()
    results.append(("도매매 목록 조회", first_no_supply is not None))

    if first_no_supply:
        ok = test_domaemae_detail(first_no_supply)
        results.append(("도매매 상세 조회", ok))
    else:
        results.append(("도매매 상세 조회", False))

    # 결과 요약
    separator("테스트 결과 요약")
    all_pass = True
    for name, ok in results:
        status = "OK  " if ok else "FAIL"
        print(f"  [{status}] {name}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print("모든 테스트 통과 - API 연결 정상입니다.")
    else:
        print("일부 테스트 실패 - 위 응답 본문을 확인해 주세요.")
    print()


if __name__ == "__main__":
    main()
