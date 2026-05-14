"""재고부족 대기 주문 저장소 — data/stock_pending_orders.json

파일 구조:
{
  "pending": [
    {
      "order_id":            "...",
      "supplier":            "domaekkuk" | "domaemae",
      "supplier_product_id": "...",
      "ss_product_id":       "...",
      "added_at":            "...",  // ISO8601
      "order":               {...}   // orders.json 주문 dict 전체
    }
  ]
}
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_PATH = Path("data/stock_pending_orders.json")
_lock = threading.Lock()


class StockPendingRepository:
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
        with _lock:
            return list(self._read())

    def count(self) -> int:
        with _lock:
            return len(self._read())

    def add(self, entry: dict):
        """entry: {"order_id", "supplier", "supplier_product_id", "ss_product_id", "order"}"""
        now = datetime.now(timezone.utc).isoformat()
        with _lock:
            items = self._read()
            existing_ids = {i["order_id"] for i in items}
            if entry["order_id"] not in existing_ids:
                items.append({**entry, "added_at": entry.get("added_at", now)})
                self._write(items)

    def find_by_supplier(self, supplier: str, supplier_product_id: str) -> list[dict]:
        """특정 도매처 상품의 대기 주문 전체 반환"""
        with _lock:
            return [
                i for i in self._read()
                if i["supplier"] == supplier
                and i["supplier_product_id"] == supplier_product_id
            ]

    def remove_many(self, order_ids: set[str]):
        if not order_ids:
            return
        with _lock:
            items = self._read()
            items = [i for i in items if i["order_id"] not in order_ids]
            self._write(items)
