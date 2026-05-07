"""data/orders.json 기반 주문 저장소

파일 구조:
{
  "orders": [
    {
      "order_id":         "2026050700000001",   // productOrderId — 고유키·중복방지 기준
      "ss_order_id":      "2026050700000001",   // orderId (상위 주문번호)
      "product_id":       "12345678",
      "product_name":     "티셔츠",
      "option_code":      "",
      "quantity":         1,
      "buyer_name":       "홍길동",
      "receiver_name":    "홍길동",
      "receiver_phone":   "010-1234-5678",
      "receiver_address": "서울시 강남구...",
      "receiver_zipcode": "06234",
      "delivery_memo":    "",
      "status":           "NEW",         // NEW / ORDERED / INVOICED / DONE / ERROR
      "collected_at":     "2026-05-07T17:00:00",
      "updated_at":       "2026-05-07T17:00:00"
    }
  ]
}
"""
import json
import threading
from datetime import datetime
from pathlib import Path

_DEFAULT_PATH = Path("data/orders.json")
_lock = threading.Lock()


class OrderRepository:
    def __init__(self, path: str | Path = _DEFAULT_PATH):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ── 내부 I/O ────────────────────────────────────────────

    def _read(self) -> list[dict]:
        if not self._path.exists():
            return []
        with open(self._path, encoding="utf-8") as f:
            return json.load(f).get("orders", [])

    def _write(self, orders: list[dict]):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"orders": orders}, f, ensure_ascii=False, indent=2)

    # ── 공개 API ─────────────────────────────────────────────

    def all(self) -> list[dict]:
        """전체 주문 목록 반환 (최신순)"""
        with _lock:
            orders = self._read()
        return sorted(orders, key=lambda o: o.get("collected_at", ""), reverse=True)

    def get_known_ids(self) -> set[str]:
        """이미 수집된 order_id 집합 반환 — O(1) 중복 검사용"""
        with _lock:
            return {o["order_id"] for o in self._read()}

    def find(self, order_id: str) -> dict | None:
        """order_id(productOrderId)로 단건 조회"""
        with _lock:
            for o in self._read():
                if o["order_id"] == order_id:
                    return o
        return None

    def find_by_status(self, status: str) -> list[dict]:
        """상태별 주문 목록 반환"""
        with _lock:
            return [o for o in self._read() if o.get("status") == status]

    def add_many(self, new_orders: list[dict]) -> int:
        """신규 주문 일괄 추가. 이미 존재하는 order_id는 건너뜀. 추가된 건수 반환."""
        if not new_orders:
            return 0
        with _lock:
            orders = self._read()
            existing = {o["order_id"] for o in orders}
            added = 0
            for order in new_orders:
                if order["order_id"] not in existing:
                    orders.append(order)
                    existing.add(order["order_id"])
                    added += 1
            if added:
                self._write(orders)
        return added

    def update_status(self, order_id: str, status: str):
        """주문 상태 변경"""
        now = datetime.now().isoformat(timespec="seconds")
        with _lock:
            orders = self._read()
            for o in orders:
                if o["order_id"] == order_id:
                    o["status"]     = status
                    o["updated_at"] = now
                    break
            self._write(orders)

    def update_supplier_info(self, order_id: str, supplier: str, supplier_order_no: str):
        """발주 완료 시 도매처 정보 기록 (상태는 별도 update_status로 변경)"""
        now = datetime.now().isoformat(timespec="seconds")
        with _lock:
            orders = self._read()
            for o in orders:
                if o["order_id"] == order_id:
                    o["supplier"]          = supplier
                    o["supplier_order_no"] = supplier_order_no
                    o["updated_at"]        = now
                    break
            self._write(orders)

    def count_by_status(self) -> dict[str, int]:
        """상태별 건수 반환. 예: {"NEW": 3, "ORDERED": 1, ...}"""
        with _lock:
            orders = self._read()
        counts: dict[str, int] = {}
        for o in orders:
            s = o.get("status", "NEW")
            counts[s] = counts.get(s, 0) + 1
        return counts
