"""스마트스토어 취소 요청 자동 처리

흐름:
  스마트스토어 API에서 CANCEL_REQUEST 상태 주문 조회 (매 10분)
    이미 처리한 ID → 스킵
    송장 미등록  → 취소 승인 자동 처리 (POST /claim/cancel/approve)
    송장 등록됨  → 취소 승인 불가, 반품 안내 이메일 알림
"""
import json
import logging
import os
from datetime import datetime, timezone

from src.api.smartstore import SmartstoreAPI

logger = logging.getLogger(__name__)

CANCELLATIONS_FILE = "data/cancellations.json"


class CancelMonitor:
    def __init__(
        self,
        smartstore: SmartstoreAPI,
        notifier=None,
        file: str | None = None,
    ):
        self._ss = smartstore
        self._notifier = notifier
        self._file = file or CANCELLATIONS_FILE

    def run(self) -> dict:
        """취소 요청 감지 및 자동 처리. 반환값: {"new": n, "total": n}"""
        logger.info("취소 요청 감지 시작")
        try:
            raw = self._ss.get_cancellations(hours=1)
        except Exception as e:
            logger.error("취소 요청 목록 조회 실패: %s", e)
            return {"new": 0, "total": 0}

        data = self._load()
        seen_ids = {r["product_order_id"] for r in data["cancellations"]}
        new_count = 0

        for item in raw:
            po    = item.get("productOrder", {})
            claim = item.get("claim", {})

            order_id = po.get("productOrderId", "")
            if not order_id or order_id in seen_ids:
                continue

            product_name   = po.get("productName", "알 수 없음")
            quantity       = po.get("quantity", 0)
            cancel_reason  = (
                claim.get("cancelReason")
                or claim.get("claimReason")
                or "사유 미상"
            )
            invoice_number = po.get("invoiceNumber", "") or ""
            detected_at    = datetime.now(timezone.utc).isoformat()

            if invoice_number:
                # 이미 발송됨 → API로 취소 승인 불가, 반품 절차 안내
                result       = "RETURN_GUIDE"
                result_label = "반품 안내 필요"
                logger.info(
                    "취소 요청(발송 완료): %s / %s → 반품 안내 처리",
                    order_id, product_name,
                )
            else:
                # 발송 전 → 취소 승인 자동 처리
                try:
                    self._ss.approve_cancel(order_id)
                    result       = "CANCEL_APPROVED"
                    result_label = "취소 자동 승인"
                    logger.info(
                        "취소 자동 승인 완료: %s / %s",
                        order_id, product_name,
                    )
                except Exception as e:
                    result       = "APPROVE_FAILED"
                    result_label = f"승인 실패: {e}"
                    logger.error("취소 승인 실패 (%s): %s", order_id, e)

            entry = {
                "product_order_id": order_id,
                "product_name":     product_name,
                "quantity":         quantity,
                "cancel_reason":    cancel_reason,
                "invoice_number":   invoice_number,
                "result":           result,
                "result_label":     result_label,
                "detected_at":      detected_at,
            }
            data["cancellations"].append(entry)
            seen_ids.add(order_id)
            new_count += 1

            self._notify(entry)
            logger.info(
                "신규 취소 요청 처리: %s / %s / %d개 / %s → %s",
                order_id, product_name, quantity, cancel_reason, result_label,
            )

        if new_count:
            self._save(data)

        logger.info(
            "취소 요청 감지 완료: 신규 %d건 / 누적 %d건",
            new_count, len(data["cancellations"]),
        )
        return {"new": new_count, "total": len(data["cancellations"])}

    def _load(self) -> dict:
        if os.path.exists(self._file):
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"cancellations": []}

    def _save(self, data: dict):
        os.makedirs(os.path.dirname(self._file) or ".", exist_ok=True)
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _notify(self, entry: dict):
        if not self._notifier:
            return

        if entry["result"] == "CANCEL_APPROVED":
            subject = f"[스마트스토어] 취소 자동 승인 — {entry['product_name']}"
            body = "\n".join([
                "■ 취소 요청이 자동으로 승인되었습니다.",
                "",
                f"상품명   : {entry['product_name']}",
                f"수량     : {entry['quantity']}개",
                f"취소 사유: {entry['cancel_reason']}",
                f"주문번호 : {entry['product_order_id']}",
                f"처리 결과: 취소 승인 완료 (환불 처리됨)",
                f"감지 시각: {entry['detected_at']}",
            ])
        elif entry["result"] == "RETURN_GUIDE":
            subject = f"[스마트스토어] 취소 요청(발송 완료) — 반품 안내 필요 — {entry['product_name']}"
            body = "\n".join([
                "■ 이미 발송된 주문에 취소 요청이 접수되었습니다.",
                "  → 취소 승인 불가. 구매자에게 반품 절차를 안내하세요.",
                "",
                f"상품명   : {entry['product_name']}",
                f"수량     : {entry['quantity']}개",
                f"취소 사유: {entry['cancel_reason']}",
                f"송장번호 : {entry['invoice_number']}",
                f"주문번호 : {entry['product_order_id']}",
                f"감지 시각: {entry['detected_at']}",
            ])
        else:
            subject = f"[스마트스토어] 취소 승인 실패 — {entry['product_name']}"
            body = "\n".join([
                "■ 취소 요청 자동 승인에 실패했습니다.",
                "  → 스마트스토어 센터에서 수동으로 확인하세요.",
                "",
                f"상품명   : {entry['product_name']}",
                f"주문번호 : {entry['product_order_id']}",
                f"오류     : {entry['result_label']}",
                f"감지 시각: {entry['detected_at']}",
            ])

        try:
            self._notifier.send(subject=subject, body=body)
        except Exception as e:
            logger.error("취소 요청 이메일 전송 실패: %s", e)
