"""예산 초과 대기 주문 저장소 — data/pending_orders.json

파일 구조:
{
  "pending": [
    {
      "order":           {...},   // orders.json 의 주문 dict 전체
      "estimated_cost":  15000,  // 예상 발주 금액 (상품가 × 수량 + 배송비)
      "pending_since":   "..."   // 대기 전환 시각 (ISO8601)
    }
  ]
}
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_PATH = Path("data/pending_orders.json")
_lock = threading.Lock()


class PendingOrderRepository:
    def __init__(self, path: str | Path = _DEFAULT_PATH):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write([])

    def _read(self) -> list[dict]:
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f).get("pending", [])
        except Exception:
            return []

    def _write(self, items: list[dict]):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"pending": items}, f, ensure_ascii=False, indent=2)

    def all(self) -> list[dict]:
        """전체 대기 목록 반환 (복사본)"""
        with _lock:
            return list(self._read())

    def count(self) -> int:
        with _lock:
            return len(self._read())

    def total_amount(self) -> int:
        """전체 대기 주문 예상 총액"""
        with _lock:
            return sum(i.get("estimated_cost", 0) for i in self._read())

    def add_many(self, new_items: list[dict]):
        """[{"order": {...}, "estimated_cost": N}, ...] 형태로 추가"""
        if not new_items:
            return
        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            items = self._read()
            existing_ids = {i["order"]["order_id"] for i in items}
            for item in new_items:
                oid = item["order"]["order_id"]
                if oid not in existing_ids:
                    items.append({
                        "order":          item["order"],
                        "estimated_cost": item.get("estimated_cost", 0),
                        "pending_since":  item.get("pending_since", now),
                    })
                    existing_ids.add(oid)
            self._write(items)

    def remove_many(self, order_ids: set[str]):
        """처리 완료된 주문 ID 목록을 대기 목록에서 제거"""
        if not order_ids:
            return
        with _lock:
            items = self._read()
            items = [i for i in items if i["order"]["order_id"] not in order_ids]
            self._write(items)
