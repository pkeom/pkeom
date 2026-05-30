"""스마트스토어 취소 요청 자동 처리

흐름:
  1. CANCEL_REQUEST 감지
  2. DB에서 도매처 발주번호 조회 (SupplierOrder)
  3. 도매처 주문 취소 API 호출 (배송 전인 경우만)
     - 도매꾹: cancelOrder (Private API)
     - 도매매: setCancelOrder (Private API)
  4. 취소 성공 → 스마트스토어 취소 승인 자동 처리
  5. 취소 실패 / 배송 시작 → 이메일 알림 + 수동 처리 플래그
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
        dk_api=None,   # DomaekkukAPI | None
        dm_cli=None,   # DomaemaeClient | None
    ):
        self._ss = smartstore
        self._notifier = notifier
        self._file = file or CANCELLATIONS_FILE
        self._suppliers: dict = {}
        if dk_api is not None:
            self._suppliers["domaekkuk"] = dk_api
        if dm_cli is not None:
            self._suppliers["domaemae"] = dm_cli

    # ── 진입점 ───────────────────────────────────────────────────

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

            product_name  = po.get("productName", "알 수 없음")
            quantity      = po.get("quantity", 0)
            cancel_reason = (
                claim.get("cancelReason")
                or claim.get("claimReason")
                or "사유 미상"
            )
            invoice_number = po.get("invoiceNumber", "") or ""
            detected_at    = datetime.now(timezone.utc).isoformat()

            # ── 도매처 발주 취소 ──────────────────────────────────
            supplier_order = self._get_supplier_order(order_id)
            supplier_cancelled, supplier_cancel_msg, supplier_info = \
                self._process_supplier_cancel(order_id, supplier_order)

            # ── 스마트스토어 취소 처리 ────────────────────────────
            if invoice_number:
                # 이미 발송 → API로 취소 불가, 반품 안내
                result       = "RETURN_GUIDE"
                result_label = f"반품 안내 필요 ({supplier_cancel_msg})"

            elif supplier_cancelled:
                # 도매처 취소 완료(or 발주 없음) → 스마트스토어 취소 승인
                try:
                    self._ss.approve_cancel(order_id)
                    result       = "CANCEL_APPROVED"
                    result_label = f"취소 자동 승인 ({supplier_cancel_msg})"
                except Exception as e:
                    result       = "APPROVE_FAILED"
                    result_label = f"승인 실패: {e}"

            else:
                # 도매처 취소 실패 → 수동 처리 필요
                result       = "SUPPLIER_CANCEL_FAILED"
                result_label = f"도매처 취소 실패 — 수동 처리 필요 ({supplier_cancel_msg})"

            entry = {
                "product_order_id": order_id,
                "product_name":     product_name,
                "quantity":         quantity,
                "cancel_reason":    cancel_reason,
                "invoice_number":   invoice_number,
                "supplier_info":    supplier_info,
                "result":           result,
                "result_label":     result_label,
                "detected_at":      detected_at,
            }
            data["cancellations"].append(entry)
            seen_ids.add(order_id)
            new_count += 1

            self._notify(entry)
            logger.info(
                "취소 요청 처리 완료: %s / %s → %s",
                order_id, product_name, result_label,
            )

        if new_count:
            self._save(data)

        logger.info(
            "취소 요청 감지 완료: 신규 %d건 / 누적 %d건",
            new_count, len(data["cancellations"]),
        )
        return {"new": new_count, "total": len(data["cancellations"])}

    # ── 도매처 취소 ──────────────────────────────────────────────

    def _process_supplier_cancel(
        self,
        ss_order_id: str,
        supplier_order: dict | None,
    ) -> tuple[bool, str, str]:
        """도매처 발주 취소 처리.

        반환: (취소_성공, 상태_메시지, 도매처_정보_문자열)
        """
        if not supplier_order:
            return True, "도매처 발주 없음", ""

        supplier = supplier_order["supplier"]
        order_no = supplier_order["supplier_order_no"]
        supplier_info = f"{supplier} / {order_no}" if order_no else supplier

        if supplier_order["tracking_number"] or supplier_order["status"] == "SHIPPED":
            return False, f"배송 시작됨({supplier}) — 취소 불가", supplier_info

        if not order_no:
            return False, f"{supplier} 발주번호 없음", supplier_info

        ok, msg = self._call_supplier_cancel(supplier, order_no)
        if ok:
            self._update_supplier_status(ss_order_id, "CANCELLED")
        return ok, msg, supplier_info

    def _call_supplier_cancel(self, supplier: str, order_no: str) -> tuple[bool, str]:
        """도매처 취소 API 실제 호출."""
        client = self._suppliers.get(supplier)
        if not client:
            return False, f"도매처 클라이언트 미등록: {supplier}"
        try:
            client.cancel_order(order_no)
            logger.info("도매처 발주 취소 완료: %s / %s", supplier, order_no)
            return True, f"{supplier} 취소 완료"
        except Exception as e:
            logger.error("도매처 발주 취소 실패 (%s/%s): %s", supplier, order_no, e)
            return False, str(e)

    # ── DB 연동 ──────────────────────────────────────────────────

    def _get_supplier_order(self, ss_order_id: str) -> dict | None:
        """DB에서 도매처 발주 정보 조회."""
        try:
            from src.db.database import get_session
            from src.db.models import SupplierOrder
            with get_session() as session:
                row = (
                    session.query(SupplierOrder)
                    .filter(SupplierOrder.ss_order_id == ss_order_id)
                    .order_by(SupplierOrder.ordered_at.desc())
                    .first()
                )
                if row:
                    return {
                        "supplier":          row.supplier,
                        "supplier_order_no": row.supplier_order_no or "",
                        "status":            row.status or "",
                        "tracking_number":   row.tracking_number or "",
                    }
        except Exception as e:
            logger.error("도매처 발주 조회 실패 (%s): %s", ss_order_id, e)
        return None

    def _update_supplier_status(self, ss_order_id: str, status: str):
        """SupplierOrder 상태를 CANCELLED로 업데이트."""
        try:
            from src.db.database import get_session
            from src.db.models import SupplierOrder
            with get_session() as session:
                row = (
                    session.query(SupplierOrder)
                    .filter(SupplierOrder.ss_order_id == ss_order_id)
                    .order_by(SupplierOrder.ordered_at.desc())
                    .first()
                )
                if row:
                    row.status = status
        except Exception as e:
            logger.error("도매처 발주 상태 업데이트 실패 (%s → %s): %s", ss_order_id, status, e)

    # ── 파일 I/O ─────────────────────────────────────────────────

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

    # ── 이메일 알림 ──────────────────────────────────────────────

    def _notify(self, entry: dict):
        if not self._notifier:
            return

        result = entry["result"]
        name   = entry["product_name"]

        if result == "CANCEL_APPROVED":
            subject = f"[스마트스토어] 취소 자동 승인 — {name}"
            body = "\n".join([
                "■ 취소 요청이 자동으로 처리되었습니다.",
                "",
                f"상품명   : {name}",
                f"수량     : {entry['quantity']}개",
                f"취소 사유: {entry['cancel_reason']}",
                f"도매처   : {entry['supplier_info'] or '발주 없음'}",
                f"주문번호 : {entry['product_order_id']}",
                f"처리 결과: {entry['result_label']}",
                f"감지 시각: {entry['detected_at']}",
            ])

        elif result == "RETURN_GUIDE":
            subject = f"[스마트스토어] 취소→반품 안내 필요 — {name}"
            body = "\n".join([
                "■ 이미 발송된 주문에 취소 요청이 접수되었습니다.",
                "  → 구매자에게 반품 절차를 안내하세요.",
                "",
                f"상품명   : {name}",
                f"수량     : {entry['quantity']}개",
                f"취소 사유: {entry['cancel_reason']}",
                f"송장번호 : {entry['invoice_number']}",
                f"도매처   : {entry['supplier_info'] or '발주 없음'}",
                f"주문번호 : {entry['product_order_id']}",
                f"감지 시각: {entry['detected_at']}",
            ])

        elif result == "SUPPLIER_CANCEL_FAILED":
            subject = f"[긴급] 도매처 취소 실패 — 수동 처리 필요 — {name}"
            body = "\n".join([
                "■ 도매처 발주 취소에 실패했습니다. 직접 취소해 주세요.",
                "",
                f"상품명   : {name}",
                f"수량     : {entry['quantity']}개",
                f"취소 사유: {entry['cancel_reason']}",
                f"도매처   : {entry['supplier_info']}",
                f"주문번호 : {entry['product_order_id']}",
                f"오류 내용: {entry['result_label']}",
                f"감지 시각: {entry['detected_at']}",
                "",
                "→ 도매꾹/도매매 사이트에서 직접 주문 취소 후 스마트스토어 취소를 승인하세요.",
            ])

        else:  # APPROVE_FAILED
            subject = f"[스마트스토어] 취소 승인 실패 — {name}"
            body = "\n".join([
                "■ 취소 처리 중 오류가 발생했습니다. 수동으로 확인하세요.",
                "",
                f"상품명   : {name}",
                f"도매처   : {entry['supplier_info'] or '발주 없음'}",
                f"주문번호 : {entry['product_order_id']}",
                f"오류     : {entry['result_label']}",
                f"감지 시각: {entry['detected_at']}",
            ])

        try:
            self._notifier.send(subject=subject, body=body)
        except Exception as e:
            logger.error("취소 이메일 전송 실패: %s", e)
