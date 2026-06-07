"""읽기 전용 — 최근 3일 PAYED/DELIVERING 주문의 claimStatus 확인.

사용법:
    python scripts/debug_claim.py
"""
import io
import json
import sys
from pathlib import Path

# Windows 터미널 인코딩 문제 방지
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.config_loader import load_config
from src.api.smartstore import SmartstoreAPI


def main():
    cfg = load_config()
    ss_cfg = cfg.get("smartstore", cfg.get("naver", {}))
    api = SmartstoreAPI(
        client_id=ss_cfg.get("client_id") or ss_cfg.get("api_key", ""),
        client_secret=ss_cfg.get("client_secret") or ss_cfg.get("secret_key", ""),
    )

    all_orders: list = []
    for status in ("PAYED", "DELIVERING"):
        try:
            orders = api.get_orders(status=status, days=3)
            print(f"[{status}] {len(orders)}건 조회됨")
            all_orders.extend(orders)
        except Exception as e:
            print(f"[{status}] 조회 실패: {e}")

    print(f"\n총 {len(all_orders)}건 (전체 출력):\n")
    print(f"{'productOrderId':<22} {'poStatus':<18} {'claimStatus':<22} {'claimType':<16}")
    print("-" * 82)

    for raw in all_orders:
        inner = raw.get("content", raw)
        po    = inner.get("productOrder", {})

        order_id     = po.get("productOrderId", "")
        po_status    = po.get("productOrderStatus", "")
        claim_status = po.get("claimStatus", "")
        claim_type   = po.get("claimType", "")

        print(f"{order_id:<22} {po_status:<18} {claim_status:<22} {claim_type:<16}")

        # claim 관련 객체 raw 출력
        for key in ("cancel", "return", "exchange", "claim"):
            obj = po.get(key) or inner.get(key)
            if obj:
                print(f"  [{key}] {json.dumps(obj, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
