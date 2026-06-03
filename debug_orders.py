"""주문 수집 불가 원인 진단 스크립트

실행:
    python debug_orders.py
"""
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

from src.utils.config_loader import load_config
from src.api.smartstore import SmartstoreAPI

SEP = "-" * 60


def main():
    cfg    = load_config()
    ss_cfg = {k: v for k, v in cfg["smartstore"].items()
              if k in ("client_id", "client_secret", "account_type")}
    api = SmartstoreAPI(**ss_cfg)

    # ── 1. 토큰 확인 ────────────────────────────────────────────
    print(SEP)
    print("[1] 인증 토큰 발급")
    try:
        token = api._get_token()
        print(f"    OK — 토큰 앞 20자: {token[:20]}...")
    except Exception as e:
        print(f"    FAIL — {e}")
        sys.exit(1)

    headers = api._headers()

    # ── 2. last-changed-statuses 원본 응답 확인 ─────────────────
    print(SEP)
    print("[2] last-changed-statuses 원본 응답 (PAYED, 최근 7일)")
    now      = datetime.now(timezone.utc)
    from_dt  = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to_dt    = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    resp = requests.get(
        f"{api.BASE_URL}/v1/pay-order/seller/product-orders/last-changed-statuses",
        headers=headers,
        params={
            "lastChangedFrom":       from_dt,
            "lastChangedTo":         to_dt,
            "productOrderStatuses":  "PAYED",
            "limitCount":            100,
        },
        timeout=10,
    )
    print(f"    HTTP {resp.status_code}")
    raw = resp.json()
    print(f"    응답 최상위 키: {list(raw.keys())}")

    # 어떤 키에 주문 목록이 있는지 확인
    for key in raw:
        val = raw[key]
        if isinstance(val, list):
            print(f"    '{key}' 배열 길이: {len(val)}")
            if val:
                print(f"    '{key}' 첫 항목 키: {list(val[0].keys()) if isinstance(val[0], dict) else val[0]}")
        else:
            print(f"    '{key}': {val}")

    # ── 3. 코드에서 사용하는 키(lastChangeStatuses)로 추출 ──────
    print(SEP)
    print("[3] 코드 방식(lastChangeStatuses 키)으로 ID 추출")
    ids_from_code_key = [item["productOrderId"] for item in raw.get("lastChangeStatuses", [])]
    print(f"    'lastChangeStatuses' 키 결과: {len(ids_from_code_key)}건 → {ids_from_code_key}")

    # 실제 응답의 모든 list 키에서 productOrderId 시도
    print(SEP)
    print("[4] 응답 내 모든 list 키에서 productOrderId 탐색")
    for key, val in raw.items():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            ids = [item.get("productOrderId") for item in val if "productOrderId" in item]
            if ids:
                print(f"    '{key}' → productOrderId {len(ids)}건: {ids}")

    # ── 5. 상태 코드별 + 24시간 구간별 탐색 ───────────────────
    print(SEP)
    print("[5] PAYED 상태 — 24시간 구간별 탐색 (최근 7일)")
    for day_ago in range(7):
        w_end   = now - timedelta(days=day_ago)
        w_start = w_end - timedelta(hours=24)
        r = requests.get(
            f"{api.BASE_URL}/v1/pay-order/seller/product-orders/last-changed-statuses",
            headers=headers,
            params={
                "lastChangedFrom":      w_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "lastChangedTo":        w_end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "productOrderStatuses": "PAYED",
                "limitCount":           100,
            },
            timeout=10,
        )
        body  = r.json()
        items = body.get("data") if isinstance(body.get("data"), list) else []
        label = f"-{day_ago}일~-{day_ago+1}일 전"
        print(f"    {label:20} HTTP {r.status_code}  data={len(items)}건  raw_data={repr(items[:2])}")

    print(SEP)
    print("[5b] 상태 코드별 (최근 24시간, data 내용 포함)")
    from_1d = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    for status_code in ("PAYED", "PAY_DONE", "DELIVERING", "DELIVERED", "CANCELED"):
        r = requests.get(
            f"{api.BASE_URL}/v1/pay-order/seller/product-orders/last-changed-statuses",
            headers=headers,
            params={
                "lastChangedFrom":      from_1d,
                "lastChangedTo":        to_dt,
                "productOrderStatuses": status_code,
                "limitCount":           100,
            },
            timeout=10,
        )
        body  = r.json()
        items = body.get("data") if isinstance(body.get("data"), list) else []
        print(f"    {status_code:15} HTTP {r.status_code}  data={len(items)}건")

    # ── 6. orders.json 현재 상태 ───────────────────────────────
    print(SEP)
    print("[6] data/orders.json 현재 상태")
    orders_path = Path("data/orders.json")
    if orders_path.exists():
        orders = json.loads(orders_path.read_text(encoding="utf-8")).get("orders", [])
        print(f"    저장된 주문 수: {len(orders)}건")
        for o in orders[-3:]:
            print(f"    order_id={o['order_id']}  product_id={o.get('product_id','')}  status={o.get('status','')}  collected={o.get('collected_at','')}")
    else:
        print("    data/orders.json 없음")

    print(SEP)
    print("진단 완료.")


if __name__ == "__main__":
    main()
