"""도매매 장바구니 담기 dry_run 테스트

사용법:
  python test_cart.py                     # settings.yaml 쿠키 사용
  python test_cart.py --product 64926509  # 상품번호 지정
  python test_cart.py --qty 2             # 수량 지정

실제 결제·주문은 발생하지 않습니다 (dry_run=True).
"""
import sys
import io
import argparse
import json

import yaml

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CONFIG_PATH = "config/settings.yaml"


def load_cookies() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("domaemae", {}).get("cookies") or {}


def main():
    parser = argparse.ArgumentParser(description="도매매 장바구니 dry_run 테스트")
    parser.add_argument("--product", default="64926509", help="상품번호 (기본: 64926509)")
    parser.add_argument("--qty",     type=int, default=1, help="수량 (기본: 1)")
    args = parser.parse_args()

    # ── 클라이언트 초기화 ────────────────────────────────────
    # 경로 문제 방지를 위해 sys.path에 프로젝트 루트 추가
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from src.api.domaemae import DomaemaeClient, DomaemaeCookieExpiredError

    cookies = load_cookies()
    print(f"로드된 쿠키: {len(cookies)}개")
    if not cookies:
        print("[경고] 쿠키가 비어 있습니다. python update_cookie.py 를 먼저 실행하세요.")

    client = DomaemaeClient(cookies=cookies)

    # ── 1단계: 상품 정보 확인 ────────────────────────────────
    print(f"\n[1] 상품 정보 조회: {args.product}")
    try:
        product = client.get_product(args.product)
        print(f"  price : {product['price']:,}원" if product['price'] else "  price : None")
        print(f"  stock : {product['stock']:,}개")
    except DomaemaeCookieExpiredError as e:
        print(f"  [쿠키 만료] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"  [오류] {e}")

    # ── 2단계: sellerId 추출 확인 ────────────────────────────
    print(f"\n[2] sellerId 추출")
    seller_id = client._get_seller_id(args.product)
    print(f"  sellerId : {seller_id!r}")

    # ── 3단계: 장바구니 담기 (dry_run) ──────────────────────
    print(f"\n[3] 장바구니 담기 dry_run (product={args.product}, qty={args.qty})")
    dummy_shipping = {
        "name": "테스트",
        "phone": "010-0000-0000",
        "address": "서울시 테스트구 테스트동",
        "zipcode": "00000",
        "memo": "dry_run 테스트",
    }
    try:
        result = client.place_order(
            args.product,
            args.qty,
            dummy_shipping,
            dry_run=True,
        )
        print(f"  응답: {result}")

        # JSON 파싱해서 성공 여부 확인
        if "[DRY_RUN] cart response:" in result:
            raw = result.replace("[DRY_RUN] cart response: ", "")
            try:
                data = json.loads(raw.replace("'", '"'))
            except Exception:
                import ast
                data = ast.literal_eval(raw)
            ok   = data.get("result") == "ok" or data.get("success") or data.get("code") == "200"
            msg  = data.get("msg") or data.get("message") or ""
            if ok:
                print(f"\n  [성공] 장바구니 담기 성공 — {msg}")
            else:
                print(f"\n  [실패] 장바구니 응답: {data}")
                if "로그인" in str(data) or "login" in str(data).lower():
                    print("  → 쿠키 만료 가능성. python update_cookie.py 실행 권장")

    except DomaemaeCookieExpiredError as e:
        print(f"  [쿠키 만료] {e}")
    except Exception as e:
        print(f"  [오류] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
