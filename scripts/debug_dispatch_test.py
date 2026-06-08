"""SS dispatch_order 단독 테스트 (읽기/쓰기 부수효과 없음)

도매처 조회 없이 지정한 송장으로 dispatch API만 호출해 응답을 확인한다.
orders.json / DB는 건드리지 않는다.

실행:
    python scripts/debug_dispatch_test.py <order_id> <tracking_number> <courier_name>

예:
    python scripts/debug_dispatch_test.py 2026060895134531 411005300790 롯데택배
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
from src.core.invoice_manager import DELIVERY_COMPANY_MAP

SEP = "=" * 70

# ── requests.post 로깅 패치 (/dispatch 요청만) ───────────────────────────
_orig_post = requests.post

def _logged_post(url, **kwargs):
    if "/dispatch" in url:
        headers = kwargs.get("headers", {})
        masked  = {
            k: ("[MASKED]" if k.lower() == "authorization" else v)
            for k, v in headers.items()
        }
        print(f"[Request] POST {url}", flush=True)
        print(f"[Request] Headers: {masked}", flush=True)
        print(
            f"[Request] Body:\n{json.dumps(kwargs.get('json', {}), ensure_ascii=False, indent=2)}",
            flush=True,
        )
    resp = _orig_post(url, **kwargs)
    if "/dispatch" in url:
        print(f"[Response] HTTP {resp.status_code}", flush=True)
        print(f"[Response] Body(raw):\n{resp.text}", flush=True)
    return resp

requests.post = _logged_post


def main():
    if len(sys.argv) < 4:
        print("사용법: python scripts/debug_dispatch_test.py <order_id> <tracking_number> <courier_name>")
        print("예:     python scripts/debug_dispatch_test.py 2026060895134531 411005300790 롯데택배")
        sys.exit(1)

    order_id        = sys.argv[1]
    tracking_number = sys.argv[2]
    courier_name    = sys.argv[3]
    company_code    = DELIVERY_COMPANY_MAP.get(courier_name, courier_name)

    print(SEP)
    print(f"order_id       : {order_id}")
    print(f"tracking_number: {tracking_number}")
    print(f"courier_name   : {courier_name}  →  code={company_code}")
    print(SEP)

    cfg    = load_config()
    ss_cfg = {k: v for k, v in cfg["smartstore"].items()
              if k in ("client_id", "client_secret", "account_type")}
    api    = SmartstoreAPI(**ss_cfg)

    try:
        result = api.dispatch_order(order_id, company_code, tracking_number)
        print(f"\n[성공] dispatch_order 반환값:\n{json.dumps(result, ensure_ascii=False, indent=2)}")
    except Exception as e:
        print(f"\n[실패] {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    main()
