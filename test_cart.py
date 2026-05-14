"""도매매 Private API dry_run 테스트

사용법:
  python test_cart.py                     # settings.yaml 설정 사용
  python test_cart.py --product 64926509  # 상품번호 지정
  python test_cart.py --qty 2             # 수량 지정

실제 결제·주문은 발생하지 않습니다 (dry_run=True).
"""
import sys
import io
import argparse

import yaml

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CONFIG_PATH = "config/settings.yaml"


def load_domaemae_cfg() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("domaemae", {})


def main():
    parser = argparse.ArgumentParser(description="도매매 Private API dry_run 테스트")
    parser.add_argument("--product", default="64926509", help="상품번호 (기본: 64926509)")
    parser.add_argument("--qty",     type=int, default=1, help="수량 (기본: 1)")
    args = parser.parse_args()

    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from src.api.domaemae import DomaemaeClient

    dm_cfg = load_domaemae_cfg()
    print(f"api_key: {dm_cfg.get('api_key', '')[:8]}...")
    print(f"user_id: {dm_cfg.get('user_id', '')}")

    client = DomaemaeClient(**dm_cfg)

    # ── 1단계: 상품 정보 확인 ────────────────────────────────
    print(f"\n[1] 상품 정보 조회: {args.product}")
    try:
        product = client.get_product(args.product)
        print(f"  title : {product.get('title', '')}")
        print(f"  price : {product['price']:,}원" if product['price'] else "  price : None")
        print(f"  stock : {product['stock']:,}개")
    except Exception as e:
        print(f"  [오류] {type(e).__name__}: {e}")

    # ── 2단계: 옵션 목록 확인 ────────────────────────────────
    print(f"\n[2] 옵션 목록 조회: {args.product}")
    try:
        options = client.get_options(args.product)
        if options:
            for opt in options:
                print(f"  [{opt['id']}] {opt['name']}")
        else:
            print("  옵션 없음 (단일 상품)")
    except Exception as e:
        print(f"  [오류] {type(e).__name__}: {e}")

    # ── 3단계: 발주 dry_run ──────────────────────────────────
    print(f"\n[3] 발주 dry_run (product={args.product}, qty={args.qty})")
    dummy_shipping = {
        "name":    "홍길동",
        "phone":   "010-1234-5678",
        "zipcode": "06236",
        "address": "서울특별시 강남구 테헤란로 152",
        "memo":    "dry_run 테스트",
    }
    try:
        result = client.place_order(
            args.product,
            args.qty,
            dummy_shipping,
            dry_run=True,
        )
        print(f"  응답: {result}")
    except Exception as e:
        print(f"  [오류] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
