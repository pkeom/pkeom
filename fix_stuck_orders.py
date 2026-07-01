#!/usr/bin/env python3
"""일회용 정리 스크립트 — ERROR로 멈춘 특정 주문 3건을 완료(INVOICED) 처리.

배경:
    아래 3건이 orders.json에서 status="ERROR"로 고착되어 무한 재시도 중이다.
    - order_collector: ERROR → NEW 로 되돌려 재발주 유발 (find_by_status("ERROR"))
    - invoice_manager: ORDERED + ERROR 를 매번 송장 재조회 (find_by_status)
    두 경로 모두 ERROR를 재시도하므로 오류/알림이 계속 발생한다.

완료 상태값 선택 = "INVOICED":
    find_by_status로 조회/재시도되는 상태는 NEW / ORDERED / ERROR 뿐이다.
    INVOICED는 어디에서도 재시도되지 않으며(invoice_manager가 송장 성공 시 만드는
    정상 종착 상태), order_placer의 멱등성 가드 ("ORDERED","INVOICED","DONE")와
    cancel_monitor의 발송완료 판정에도 '완료'로 취급된다.
    → ERROR → INVOICED 로 바꾸면 재발주·송장 재조회 루프가 모두 멈춘다.

안전장치:
    - 대상 3건만, 그리고 현재 status가 정확히 "ERROR"인 경우에만 변경한다.
    - 원자적 저장: 같은 디렉터리에 임시파일로 쓰고 fsync 후 os.replace 로 교체.
    - orders.json.bak 백업은 이미 존재한다고 가정(별도 백업 생성 안 함).

일회성 실행:  python fix_stuck_orders.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ORDERS_PATH = Path(__file__).parent / "data" / "orders.json"

TARGET_IDS = [
    "2026060776240541",
    "2026060777267571",
    "2026060780333561",
]

FROM_STATUS = "ERROR"
TO_STATUS   = "INVOICED"


def _atomic_write(path: Path, data: dict) -> None:
    """같은 디렉터리 임시파일 → fsync → os.replace 로 원자적 교체."""
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)   # 원자적 교체 (동일 파일시스템)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main() -> int:
    if not ORDERS_PATH.exists():
        print(f"[오류] 파일 없음: {ORDERS_PATH}")
        return 1

    with open(ORDERS_PATH, encoding="utf-8") as f:
        doc = json.load(f)

    orders = doc.get("orders", [])
    by_id = {o.get("order_id"): o for o in orders}
    now = datetime.now().isoformat(timespec="seconds")

    changed = 0
    for oid in TARGET_IDS:
        o = by_id.get(oid)
        if o is None:
            print(f"[건너뜀] {oid} — orders.json에 없음")
            continue
        cur = o.get("status")
        if cur != FROM_STATUS:
            print(f"[건너뜀] {oid} — status={cur!r} (ERROR 아님, 변경 안 함)")
            continue
        o["status"] = TO_STATUS
        o["updated_at"] = now
        changed += 1
        print(f"[변경]   {oid} — {FROM_STATUS} → {TO_STATUS}")

    if changed == 0:
        print("\n변경 대상 없음 — 파일을 건드리지 않았습니다.")
        return 0

    _atomic_write(ORDERS_PATH, doc)
    print(f"\n완료: {changed}건 {FROM_STATUS} → {TO_STATUS} (원자적 저장: {ORDERS_PATH})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
