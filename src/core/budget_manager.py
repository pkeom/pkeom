"""예산 관리 — data/budget.json 기반

파일 구조:
{
  "current_balance": 482000,
  "total_spent":      18000,
  "total_charged":   500000,
  "history": [
    {"type": "charge",  "amount":  500000, "reason": "초기 예산",  "balance_after": 500000, "at": "..."},
    {"type": "deduct",  "amount":  -18000, "reason": "발주 완료", "order_id": "...", "balance_after": 482000, "at": "..."}
  ]
}
"""
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("data/budget.json")
_lock = threading.Lock()


class BudgetManager:
    def __init__(self, initial_balance: int = 0, path: str | Path = _DEFAULT_PATH):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initial_balance = initial_balance
        if not self._path.exists():
            self._write(self._make_default())

    def _make_default(self) -> dict:
        return {
            "current_balance": self._initial_balance,
            "total_spent":     0,
            "total_charged":   self._initial_balance,
            "history": [
                {
                    "type":          "charge",
                    "amount":        self._initial_balance,
                    "reason":        "초기 예산 설정",
                    "balance_after": self._initial_balance,
                    "at":            datetime.now(timezone.utc).isoformat(),
                }
            ] if self._initial_balance > 0 else [],
        }

    def _read(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return self._make_default()

    def _write(self, data: dict):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_balance(self) -> int:
        with _lock:
            return int(self._read().get("current_balance", 0))

    def get_data(self) -> dict:
        with _lock:
            return self._read()

    def deduct(self, amount: int, reason: str, order_id: str = "") -> int:
        """잔액 차감. 차감 후 잔액 반환."""
        with _lock:
            data = self._read()
            data["current_balance"] = int(data.get("current_balance", 0)) - amount
            data["total_spent"]     = int(data.get("total_spent", 0)) + amount
            data["history"].append({
                "type":          "deduct",
                "amount":        -amount,
                "reason":        reason,
                "order_id":      order_id,
                "balance_after": data["current_balance"],
                "at":            datetime.now(timezone.utc).isoformat(),
            })
            self._write(data)
            logger.debug("예산 차감 %s원 (%s) → 잔액 %s원",
                         f"{amount:,}", reason, f"{data['current_balance']:,}")
            return data["current_balance"]

    def charge(self, amount: int, reason: str = "예산 충전") -> int:
        """잔액 충전. 충전 후 잔액 반환."""
        with _lock:
            data = self._read()
            data["current_balance"] = int(data.get("current_balance", 0)) + amount
            data["total_charged"]   = int(data.get("total_charged", 0)) + amount
            data["history"].append({
                "type":          "charge",
                "amount":        amount,
                "reason":        reason,
                "balance_after": data["current_balance"],
                "at":            datetime.now(timezone.utc).isoformat(),
            })
            self._write(data)
            logger.info("예산 충전 %s원 (%s) → 잔액 %s원",
                        f"{amount:,}", reason, f"{data['current_balance']:,}")
            return data["current_balance"]
