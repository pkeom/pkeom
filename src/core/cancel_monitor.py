"""스마트스토어 취소 요청 자동 처리 (매 10분)

취소 요청(CANCEL_REQUEST) 처리 흐름:
  a) DB 발주 없음          → SS_AUTO        SS가 자동 처리 (알림 없음)
  b) DB ORDERED (출고 전)  → DENY_SENT      도매처에 setOrdDeny 전송
       도매처 취소 승인    → APPROVED        SS approve_cancel 호출
       도매처 취소 거부    → REJECTED        SS dispatch 호출 → CANCEL_REJECT
       출고 후 송장 대기   → REJECTED_WAIT_SHIP  송장 확인 후 dispatch
       3영업일 미처리      → URGENT_3DAY     긴급 알림 (계속 폴링)
       4영업일 초과        → MANUAL_4DAY     수동 처리 알림
  c) DB SHIPPED (출고됨)   → SHIPPED_REJECT  SS dispatch 호출 → CANCEL_REJECT

예외) 발주확인 후 취소 요청 → RACE_CONDITION  수동 처리 알림
"""
import json
import logging
import os
from datetime import datetime, timezone, timedelta

from src.api.smartstore import SmartstoreAPI

logger = logging.getLogger(__name__)

CANCELLATIONS_FILE = "data/cancellations.json"
CONFIRM_LOG_FILE   = "data/confirm_log.json"

# 매 주기 재폴링할 활성 상태
_POLL_STATES = frozenset({"DENY_SENT", "URGENT_3DAY", "REJECTED_WAIT_SHIP"})


def _business_days_since(dt_str: str) -> int:
    """dt_str 이후 경과한 영업일(월~금) 수."""
    try:
        start = datetime.fromisoformat(dt_str)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        count = 0
        day   = start.date() + timedelta(days=1)
        today = datetime.now(timezone.utc).date()
        while day <= today:
            if day.weekday() < 5:
                count += 1
            day += timedelta(days=1)
        return count
    except Exception:
        return 0


class CancelMonitor:
    def __init__(
        self,
        smartstore: SmartstoreAPI,
        notifier=None,
        file: str | None = None,
        dk_api=None,
        dm_cli=None,
    ):
        self._ss       = smartstore
        self._notifier = notifier
        self._file     = file or CANCELLATIONS_FILE
        self._suppliers: dict = {}
        if dk_api is not None:
            self._suppliers["domaekkuk"] = dk_api
        if dm_cli is not None:
            self._suppliers["domaemae"]  = dm_cli

    # ── 진입점 ───────────────────────────────────────────────────

    def run(self) -> dict:
        logger.info("취소 요청 감지 시작")
        data     = self._load()
        seen_ids = {r["product_order_id"] for r in data["cancellations"]}
        new_count = 0

        # 1. 신규 취소 요청 감지 (최근 1시간)
        try:
            raw_cancels = self._ss.get_cancellations(hours=1)
        except Exception as e:
            logger.error("취소 요청 목록 조회 실패: %s", e)
            raw_cancels = []

        for item in raw_cancels:
            po       = item.get("productOrder", {})
            claim    = item.get("claim", {})
            order_id = po.get("productOrderId", "")
            if not order_id or order_id in seen_ids:
                continue

            entry          = self._make_entry(po, claim)
            supplier_order = self._get_supplier_order(order_id)

            if not supplier_order or supplier_order["status"] in ("CANCELLED", "ERROR", ""):
                self._route_ss_auto(entry)
            elif supplier_order["tracking_number"] or supplier_order["status"] == "SHIPPED":
                self._route_shipped(entry, supplier_order)
            elif supplier_order["status"] == "ORDERED":
                was_confirmed = self._was_ss_confirmed(order_id)
                self._route_ordered(entry, supplier_order, was_confirmed)
            else:
                # PENDING, STOCK_PENDING 등 → SS 자동 처리
                self._route_ss_auto(entry)

            data["cancellations"].append(entry)
            seen_ids.add(order_id)
            new_count += 1
            logger.info(
                "신규 취소 요청 처리: %s / %s → %s",
                order_id, entry.get("product_name"), entry.get("cancel_state"),
            )

        # 2. 진행 중인 취소 요청 폴링
        for entry in data["cancellations"]:
            state = entry.get("cancel_state", "")
            if state in ("DENY_SENT", "URGENT_3DAY"):
                self._poll_deny_result(entry)
            elif state == "REJECTED_WAIT_SHIP":
                self._poll_tracking_for_rejected(entry)

        # 3. 레이스 컨디션 감지 (발주확인 후 취소 요청)
        self._detect_race_conditions(data, seen_ids)

        self._save(data)
        total = len(data["cancellations"])
        logger.info("취소 요청 처리 완료: 신규 %d건 / 누적 %d건", new_count, total)
        return {"new": new_count, "total": total}

    # ── 라우팅 ───────────────────────────────────────────────────

    def _route_ss_auto(self, entry: dict):
        """case a: DB 발주 없음 또는 이미 취소/오류 → SS 자동 처리."""
        entry["cancel_state"] = "SS_AUTO"
        entry["result_label"] = "SS 자동 처리 (도매처 발주 없음)"

    def _route_shipped(self, entry: dict, sup: dict):
        """case c: DB SHIPPED → SS dispatch(CANCEL_REJECT)."""
        entry["supplier"]          = sup["supplier"]
        entry["supplier_order_no"] = sup["supplier_order_no"]
        entry["invoice_number"]    = sup["tracking_number"]
        self._do_dispatch_reject(entry, sup["delivery_company"], sup["tracking_number"])
        if entry["cancel_state"] == "REJECTED":
            entry["cancel_state"] = "SHIPPED_REJECT"
            entry["result_label"] = (
                f"출고 완료 → SS 발송처리(CANCEL_REJECT) 완료 "
                f"(송장: {sup['tracking_number']})"
            )
            self._notify(
                entry, f"[취소거부] 출고 완료 건 CANCEL_REJECT — {entry['product_name']}",
                "→ 이미 출고된 주문에 취소 요청이 접수되어 SS 발송처리(CANCEL_REJECT)로 처리했습니다.\n"
                "  고객이 반품 요청 시 반품 절차를 안내하세요.",
            )

    def _route_ordered(self, entry: dict, sup: dict, was_confirmed: bool):
        """case b: DB ORDERED → setOrdDeny(도매처 취소 요청 전송)."""
        supplier = sup["supplier"]
        order_no = sup["supplier_order_no"]
        entry["supplier"]          = supplier
        entry["supplier_order_no"] = order_no

        extra = ""
        if was_confirmed:
            extra = (
                "⚠️ SS 발주확인 완료 후 취소 요청이 접수되었습니다.\n"
                "   도매처 취소 요청을 자동으로 전송합니다."
            )

        if not order_no:
            entry["cancel_state"] = "DENY_FAILED"
            entry["result_label"] = f"{supplier} 발주번호 없음 — 수동 처리 필요"
            self._notify(
                entry, f"[긴급] 도매처 취소 요청 실패 — 수동 처리 필요 — {entry['product_name']}",
                f"오류: {supplier} 발주번호 없음\n"
                f"→ 도매꾹/도매매 사이트에서 직접 취소 처리 후 SS 취소 승인하세요.\n{extra}",
            )
            return

        ok, msg = self._call_deny(supplier, order_no)
        if ok:
            entry["cancel_state"] = "DENY_SENT"
            entry["deny_sent_at"] = datetime.now(timezone.utc).isoformat()
            entry["result_label"] = f"{supplier} 취소 요청 전송 완료 — 도매처 응답 대기 중"
            self._notify(
                entry, f"[취소처리] 도매처 취소 요청 전송 — {entry['product_name']}",
                f"→ {supplier} 취소 요청(setOrdDeny)을 전송했습니다.\n"
                f"  도매처 응답을 10분마다 확인합니다.\n"
                f"  4영업일 내 응답 없으면 SS가 자동 취소 승인합니다.\n{extra}",
            )
        else:
            entry["cancel_state"] = "DENY_FAILED"
            entry["result_label"] = f"{supplier} 취소 요청 전송 실패: {msg}"
            self._notify(
                entry, f"[긴급] 도매처 취소 요청 실패 — 수동 처리 필요 — {entry['product_name']}",
                f"오류: {msg}\n"
                f"→ 도매꾹/도매매 사이트에서 직접 주문({order_no})을 취소하세요.\n"
                f"  취소 후 SS에서 취소 승인을 처리하세요.\n{extra}",
            )

    # ── 폴링 ─────────────────────────────────────────────────────

    def _poll_deny_result(self, entry: dict):
        """DENY_SENT / URGENT_3DAY: 도매처 취소 처리 결과 폴링."""
        supplier = entry.get("supplier", "")
        order_no = entry.get("supplier_order_no", "")
        order_id = entry["product_order_id"]

        bdays = _business_days_since(entry.get("deny_sent_at", entry["detected_at"]))
        entry["business_days_elapsed"] = bdays
        entry["last_checked_at"]       = datetime.now(timezone.utc).isoformat()

        result = self._get_cancel_result(supplier, order_no)
        logger.debug("취소 결과 폴링: %s → %s (영업일 %d일)", order_id, result, bdays)

        if result == "APPROVED":
            # 도매처 취소 승인 → SS 취소 승인
            try:
                self._ss.approve_cancel(order_id)
                entry["cancel_state"] = "APPROVED"
                entry["result_label"] = f"{supplier} 취소 승인 → SS 취소 승인 완료"
                self._update_supplier_status(order_id, "CANCELLED")
                self._notify(
                    entry, f"[취소완료] 도매처 취소 승인 → SS 취소 완료 — {entry['product_name']}",
                    f"→ {supplier}이(가) 취소 요청을 승인했습니다.\n"
                    f"  SS 취소 승인(approve_cancel)도 완료되었습니다. 환불이 진행됩니다.",
                )
            except Exception as e:
                entry["cancel_state"] = "MANUAL_REQUIRED"
                entry["result_label"] = f"SS 취소 승인 실패: {e}"
                self._notify(
                    entry, f"[긴급] SS 취소 승인 실패 — 수동 처리 필요 — {entry['product_name']}",
                    f"도매처({supplier})는 취소를 승인했으나 SS 취소 승인 API가 실패했습니다.\n"
                    f"오류: {e}\n"
                    f"→ 스마트스토어 센터에서 직접 취소 승인하세요.",
                )

        elif result == "REJECTED":
            # 도매처 취소 거부 → 송장 확인 후 SS dispatch
            sup = self._get_supplier_order(order_id)
            tracking = sup["tracking_number"] if sup else ""
            company  = sup["delivery_company"] if sup else ""
            if tracking:
                self._do_dispatch_reject(entry, company, tracking)
                if entry["cancel_state"] == "REJECTED":
                    self._notify(
                        entry, f"[취소거부] 도매처 취소 거부 → SS CANCEL_REJECT — {entry['product_name']}",
                        f"→ {supplier}이(가) 취소 요청을 거부하고 출고를 진행했습니다.\n"
                        f"  SS 발송처리(CANCEL_REJECT)로 취소 요청을 거부했습니다.\n"
                        f"  송장번호: {tracking}\n"
                        f"  고객이 반품 요청 시 반품 절차를 안내하세요.",
                    )
            else:
                # 아직 출고 전 → 송장 대기
                entry["cancel_state"] = "REJECTED_WAIT_SHIP"
                entry["result_label"] = f"{supplier} 취소 거부 — 출고 후 송장 확인 대기 중"
                self._notify(
                    entry, f"[취소거부] 도매처 취소 거부 — 출고 대기 중 — {entry['product_name']}",
                    f"→ {supplier}이(가) 취소를 거부했습니다. 출고 후 자동으로 SS CANCEL_REJECT 처리합니다.\n"
                    f"  송장 등록(invoice_manager)을 확인하세요.",
                )

        else:  # PENDING
            if bdays >= 4 and entry["cancel_state"] != "MANUAL_4DAY":
                entry["cancel_state"] = "MANUAL_4DAY"
                entry["result_label"] = f"{bdays}영업일 경과 — SS 자동 승인 예상, 수동 처리 필요"
                self._notify(
                    entry, f"[수동처리] 취소 {bdays}영업일 초과 — SS 자동 승인 예상 — {entry['product_name']}",
                    f"→ {bdays}영업일이 경과하여 SS가 자동으로 취소 승인했을 가능성이 높습니다.\n"
                    f"  도매처({supplier}) 주문번호 {order_no}를 사이트에서 직접 취소하세요.\n"
                    f"  필요 조치:\n"
                    f"  1. {supplier} 사이트에서 주문({order_no}) 상태 확인\n"
                    f"  2. 미취소 시 직접 취소 처리\n"
                    f"  3. SS 스마트스토어센터에서 취소 상태 확인",
                )
            elif bdays >= 3 and entry["cancel_state"] not in ("URGENT_3DAY", "MANUAL_4DAY"):
                entry["cancel_state"] = "URGENT_3DAY"
                entry["result_label"] = f"{bdays}영업일 경과 — SS 자동 승인 임박"
                self._notify(
                    entry, f"[긴급] 취소 처리 {bdays}영업일 — SS 자동 승인 임박 — {entry['product_name']}",
                    f"→ {supplier} 취소 요청 후 {bdays}영업일이 경과했습니다.\n"
                    f"  내일(4영업일)까지 미처리 시 SS가 자동으로 취소 승인 + 페널티 부과됩니다.\n"
                    f"  도매꾹/도매매 사이트에서 주문({order_no}) 상태를 직접 확인하세요.",
                )

    def _poll_tracking_for_rejected(self, entry: dict):
        """REJECTED_WAIT_SHIP: 취소 거부 후 출고 대기 → 송장 확인."""
        order_id = entry["product_order_id"]
        entry["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        sup = self._get_supplier_order(order_id)
        if not sup or not sup["tracking_number"]:
            return
        self._do_dispatch_reject(entry, sup["delivery_company"], sup["tracking_number"])
        if entry["cancel_state"] == "REJECTED":
            self._notify(
                entry, f"[취소거부] SS CANCEL_REJECT 완료 (출고 확인 후) — {entry['product_name']}",
                f"→ {entry['supplier']} 취소 거부 후 출고가 확인되어 SS 발송처리(CANCEL_REJECT)했습니다.\n"
                f"  송장번호: {sup['tracking_number']}\n"
                f"  고객이 반품 요청 시 반품 절차를 안내하세요.",
            )

    # ── SS dispatch (CANCEL_REJECT) ──────────────────────────────

    def _do_dispatch_reject(self, entry: dict, delivery_company: str, tracking_number: str):
        """SS dispatch_order 호출 → CANCEL_REJECT."""
        from src.core.invoice_manager import DELIVERY_COMPANY_MAP
        order_id = entry["product_order_id"]
        code     = DELIVERY_COMPANY_MAP.get(delivery_company, delivery_company or "CJGLS")
        try:
            self._ss.dispatch_order(order_id, code, tracking_number)
            entry["cancel_state"] = "REJECTED"
            entry["result_label"] = f"SS 발송처리(CANCEL_REJECT) 완료 (송장: {tracking_number})"
        except Exception as e:
            entry["cancel_state"] = "MANUAL_REQUIRED"
            entry["result_label"] = f"SS 발송처리 실패: {e}"
            self._notify(
                entry, f"[긴급] SS 발송처리 실패 — 수동 처리 필요 — {entry['product_name']}",
                f"SS dispatch API 실패: {e}\n"
                f"송장번호: {tracking_number}\n"
                f"→ 스마트스토어 센터에서 직접 발송처리하세요.",
            )

    # ── 레이스 컨디션 감지 ───────────────────────────────────────

    def _detect_race_conditions(self, data: dict, seen_ids: set):
        """발주확인 완료 후 취소 요청이 들어온 경우 감지.

        confirm_log.json에 기록된 최근 1시간 발주확인 주문 중,
        24시간 취소 요청 목록에 있으면서 아직 미처리인 건을 탐지한다.
        """
        try:
            if not os.path.exists(CONFIRM_LOG_FILE):
                return
            with open(CONFIRM_LOG_FILE, "r", encoding="utf-8") as f:
                confirm_data = json.load(f)

            cutoff_1h = datetime.now(timezone.utc) - timedelta(hours=1)
            recent_confirmed = {
                c["order_id"]
                for c in confirm_data.get("confirms", [])
                if datetime.fromisoformat(c["confirmed_at"]) >= cutoff_1h
            }
            if not recent_confirmed:
                return

            # 24시간 취소 요청 목록
            try:
                raw_24h = self._ss.get_cancellations(hours=24)
                cancel_24h = {
                    item.get("productOrder", {}).get("productOrderId", "")
                    for item in raw_24h
                }
            except Exception:
                cancel_24h = set()

            for order_id in (recent_confirmed & cancel_24h) - seen_ids:
                if not order_id:
                    continue
                logger.warning("레이스 컨디션 감지: order_id=%s", order_id)
                entry = {
                    "product_order_id":      order_id,
                    "product_name":          "(재확인 필요)",
                    "quantity":              0,
                    "cancel_reason":         "발주확인 후 취소 요청 감지",
                    "supplier":              "",
                    "supplier_order_no":     "",
                    "invoice_number":        "",
                    "cancel_state":          "RACE_CONDITION",
                    "deny_sent_at":          "",
                    "last_checked_at":       datetime.now(timezone.utc).isoformat(),
                    "detected_at":           datetime.now(timezone.utc).isoformat(),
                    "result_label":          "발주확인 후 취소 요청 감지 — 수동 처리 필요",
                    "business_days_elapsed": 0,
                }
                data["cancellations"].append(entry)
                seen_ids.add(order_id)
                self._notify(
                    entry, f"[예외] 발주확인 후 취소 요청 감지 — 수동 처리 필요",
                    f"SS 주문번호: {order_id}\n"
                    f"→ SS 발주확인 완료 후 고객이 취소 요청했습니다.\n"
                    f"  도매꾹/도매매 사이트에서 해당 주문을 직접 취소하고\n"
                    f"  SS에서 취소 승인을 처리하세요.\n"
                    f"  필요 조치:\n"
                    f"  1. 도매처 사이트 로그인 → 해당 주문 취소\n"
                    f"  2. SS 스마트스토어센터 → 취소 승인",
                )
        except Exception as e:
            logger.warning("레이스 컨디션 감지 실패: %s", e)

    # ── 도매처 API ───────────────────────────────────────────────

    def _call_deny(self, supplier: str, order_no: str) -> tuple[bool, str]:
        client = self._suppliers.get(supplier)
        if not client:
            return False, f"도매처 클라이언트 없음: {supplier}"
        try:
            client.cancel_order(order_no)
            return True, "성공"
        except Exception as e:
            logger.error("setOrdDeny 실패 (%s/%s): %s", supplier, order_no, e)
            return False, str(e)

    def _get_cancel_result(self, supplier: str, order_no: str) -> str:
        client = self._suppliers.get(supplier)
        if not client:
            return "PENDING"
        try:
            return client.get_cancel_result(order_no)
        except Exception as e:
            logger.warning("취소 결과 조회 실패 (%s/%s): %s", supplier, order_no, e)
            return "PENDING"

    # ── DB 연동 ──────────────────────────────────────────────────

    def _get_supplier_order(self, ss_order_id: str) -> dict | None:
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
                        "delivery_company":  row.delivery_company or "",
                    }
        except Exception as e:
            logger.error("도매처 발주 조회 실패 (%s): %s", ss_order_id, e)
        return None

    def _update_supplier_status(self, ss_order_id: str, status: str):
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
            logger.error("SupplierOrder 상태 업데이트 실패 (%s): %s", ss_order_id, e)

    def _was_ss_confirmed(self, order_id: str) -> bool:
        """confirm_log.json에 발주확인 기록이 있는지 확인."""
        try:
            if not os.path.exists(CONFIRM_LOG_FILE):
                return False
            with open(CONFIRM_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return any(c["order_id"] == order_id for c in data.get("confirms", []))
        except Exception:
            return False

    # ── 파일 I/O ─────────────────────────────────────────────────

    def _make_entry(self, po: dict, claim: dict) -> dict:
        return {
            "product_order_id":      po.get("productOrderId", ""),
            "product_name":          po.get("productName", "알 수 없음"),
            "quantity":              po.get("quantity", 0),
            "cancel_reason":         (
                claim.get("cancelReason")
                or claim.get("claimReason")
                or "사유 미상"
            ),
            "supplier":              "",
            "supplier_order_no":     "",
            "invoice_number":        po.get("invoiceNumber", "") or "",
            "cancel_state":          "",
            "deny_sent_at":          "",
            "last_checked_at":       "",
            "detected_at":           datetime.now(timezone.utc).isoformat(),
            "result_label":          "",
            "business_days_elapsed": 0,
        }

    def _load(self) -> dict:
        if os.path.exists(self._file):
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 구버전 항목 마이그레이션 (cancel_state 없는 경우)
                for entry in data.get("cancellations", []):
                    if "cancel_state" not in entry:
                        entry.setdefault("cancel_state",          entry.get("result", "LEGACY"))
                        entry.setdefault("deny_sent_at",          "")
                        entry.setdefault("last_checked_at",       "")
                        entry.setdefault("supplier",              "")
                        entry.setdefault("supplier_order_no",     "")
                        entry.setdefault("business_days_elapsed", 0)
                return data
            except Exception:
                pass
        return {"cancellations": []}

    def _save(self, data: dict):
        os.makedirs(os.path.dirname(self._file) or ".", exist_ok=True)
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 알림 공통 ────────────────────────────────────────────────

    def _notify(self, entry: dict, subject: str, detail: str = ""):
        """대시보드(로거) + 이메일 알림."""
        logger.info("[취소알림] %s | %s", entry.get("product_order_id", ""), subject)
        if not self._notifier:
            return
        lines = [
            f"■ {subject}",
            "",
            f"상품명         : {entry.get('product_name', '-')}",
            f"SS 주문번호    : {entry.get('product_order_id', '-')}",
            f"도매처         : {entry.get('supplier', '-')}",
            f"도매처 발주번호 : {entry.get('supplier_order_no', '-')}",
            f"현재 상태      : {entry.get('result_label', '-')}",
            f"취소 사유      : {entry.get('cancel_reason', '-')}",
            f"감지 시각      : {entry.get('detected_at', '-')}",
        ]
        if detail:
            lines += ["", detail]
        try:
            self._notifier.send(subject=subject, body="\n".join(lines))
        except Exception as e:
            logger.error("이메일 전송 실패: %s", e)
