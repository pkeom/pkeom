"""data/price_alerts.json 기반 가격 변동 알림 저장소

파일 구조:
{
  "alerts": [
    {
      "id":                   "uuid4",
      "ss_product_id":        "P100",
      "supplier":             "domaekkuk",
      "supplier_product_id":  "12345",
      "product_name":         "티셔츠",
      "old_price":            10000,
      "new_price":            12000,
      "change_rate":          0.2,    // 양수=인상, 음수=인하
      "detected_at":          "2026-05-07T10:00:00",
      "is_read":              false
    }
  ]
}
"""
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

_DEFAULT_PATH = Path("data/price_alerts.json")
_lock = threading.Lock()


class PriceAlertRepository:
    def __init__(self, path: str | Path = _DEFAULT_PATH):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ── 내부 I/O ────────────────────────────────────────────

    def _read(self) -> list[dict]:
        if not self._path.exists():
            return []
        with open(self._path, encoding="utf-8") as f:
            return json.load(f).get("alerts", [])

    def _write(self, alerts: list[dict]):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"alerts": alerts}, f, ensure_ascii=False, indent=2)

    # ── 공개 API ─────────────────────────────────────────────

    def all(self) -> list[dict]:
        """전체 알림 목록 (최신순)"""
        with _lock:
            alerts = self._read()
        return sorted(alerts, key=lambda a: a.get("detected_at", ""), reverse=True)

    def unread(self) -> list[dict]:
        """읽지 않은 알림 목록"""
        return [a for a in self.all() if not a.get("is_read", False)]

    def count_unread(self) -> int:
        with _lock:
            return sum(1 for a in self._read() if not a.get("is_read", False))

    def add(
        self,
        ss_product_id: str,
        supplier: str,
        supplier_product_id: str,
        product_name: str,
        old_price: int | None,
        new_price: int,
    ) -> dict:
        """가격 변동 알림 추가. 반환값: 생성된 alert dict"""
        if old_price:
            change_rate = round((new_price - old_price) / old_price, 4)
        else:
            change_rate = 0.0

        alert = {
            "id":                  str(uuid.uuid4()),
            "ss_product_id":       ss_product_id,
            "supplier":            supplier,
            "supplier_product_id": supplier_product_id,
            "product_name":        product_name,
            "old_price":           old_price,
            "new_price":           new_price,
            "change_rate":         change_rate,
            "detected_at":         datetime.now().isoformat(timespec="seconds"),
            "is_read":             False,
        }
        with _lock:
            alerts = self._read()
            alerts.append(alert)
            self._write(alerts)
        return alert

    def mark_read(self, alert_id: str) -> bool:
        """단건 읽음 처리. 성공 True / 미존재 False"""
        with _lock:
            alerts = self._read()
            for a in alerts:
                if a["id"] == alert_id:
                    a["is_read"] = True
                    self._write(alerts)
                    return True
        return False

    def mark_all_read(self):
        """전체 읽음 처리"""
        with _lock:
            alerts = self._read()
            for a in alerts:
                a["is_read"] = True
            self._write(alerts)

    def by_product(self, ss_product_id: str) -> list[dict]:
        """특정 스마트스토어 상품의 알림만 반환"""
        return [a for a in self.all() if a.get("ss_product_id") == ss_product_id]
