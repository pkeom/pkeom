"""스마트스토어 반품 신청 감지 → data/returns.json 저장 + 이메일 알림

흐름:
  스마트스토어 API에서 RETURN_REQUEST 상태 주문 조회
    이미 처리한 ID → 스킵
    신규 반품     → returns.json 저장 + 이메일 알림
"""
import json
import logging
import os
from datetime import datetime, timezone

from src.api.smartstore import SmartstoreAPI

logger = logging.getLogger(__name__)

RETURNS_FILE = "data/returns.json"


class ReturnMonitor:
    def __init__(self, smartstore: SmartstoreAPI, notifier=None):
        self._ss = smartstore
        self._notifier = notifier

    def run(self) -> dict:
        """반품 신청 감지 및 알림. 반환값: {"new": n, "total": n}"""
        logger.info("반품 감지 시작")
        try:
            raw = self._ss.get_returns(hours=1)
        except Exception as e:
            logger.error("반품 목록 조회 실패: %s", e)
            return {"new": 0, "total": 0}

        data = self._load()
        seen_ids = {r["product_order_id"] for r in data["returns"]}
        new_count = 0

        for item in raw:
            po = item.get("productOrder", {})
            claim = item.get("claim", {})

            order_id = po.get("productOrderId", "")
            if not order_id or order_id in seen_ids:
                continue

            product_name = po.get("productName", "알 수 없음")
            quantity = po.get("quantity", 0)
            return_reason = (
                claim.get("returnReason")
                or claim.get("claimReason")
                or "사유 미상"
            )
            return_reason_type = claim.get("returnReasonType", "")
            detected_at = datetime.now(timezone.utc).isoformat()

            entry = {
                "product_order_id": order_id,
                "product_name": product_name,
                "quantity": quantity,
                "return_reason": return_reason,
                "return_reason_type": return_reason_type,
                "detected_at": detected_at,
            }
            data["returns"].append(entry)
            seen_ids.add(order_id)
            new_count += 1

            self._notify(entry)
            logger.info(
                "신규 반품 감지: %s / %s / %d개 / %s",
                order_id, product_name, quantity, return_reason,
            )

        if new_count:
            self._save(data)

        logger.info(
            "반품 감지 완료: 신규 %d건 / 누적 %d건",
            new_count, len(data["returns"]),
        )
        return {"new": new_count, "total": len(data["returns"])}

    def _load(self) -> dict:
        if os.path.exists(RETURNS_FILE):
            try:
                with open(RETURNS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"returns": []}

    def _save(self, data: dict):
        os.makedirs(os.path.dirname(RETURNS_FILE), exist_ok=True)
        with open(RETURNS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _notify(self, entry: dict):
        if not self._notifier:
            return
        subject = f"[스마트스토어] 반품 신청 — {entry['product_name']}"
        body = "\n".join([
            "■ 새 반품 신청이 접수되었습니다.",
            "",
            f"상품명   : {entry['product_name']}",
            f"수량     : {entry['quantity']}개",
            f"반품 사유: {entry['return_reason']}",
            f"주문번호 : {entry['product_order_id']}",
            f"감지 시각: {entry['detected_at']}",
        ])
        try:
            self._notifier.send(subject=subject, body=body)
        except Exception as e:
            logger.error("반품 이메일 전송 실패: %s", e)
