"""스마트스토어 취소 요청 자동 처리 (매 10분)

취소 요청(CANCEL_REQUEST) 처리 흐름:
  a) DB 발주 없음          → SS_AUTO        SS가 자동 처리 (알림 없음)
  b) DB ORDERED (출고 전)  → DENY_SENT      도매처에 setOrdDeny 전송
       도매처 취소 승인    → APPROVED            SS approve_cancel 호출
       도매처 취소 거부    → REJECTED_WAIT_SHIP  invoice_manager 위임 (도매꾹 출고 후 취소 불가)
       3영업일 미처리      → URGENT_3DAY     긴급 알림 (계속 폴링)
       4영업일 초과        → MANUAL_4DAY     수동 처리 알림
  c) DB SHIPPED (출고됨)   → SHIPPED_REJECT  SS dispatch 호출 → CANCEL_REJECT

예외) 발주확인 후 취소 요청 → RACE_CONDITION  수동 처리 알림
"""
import json
import logging
import os
import time as _time
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
        order_repo=None,  # OrderRepository — 취소 철회 시 주문 상태 복원용
    ):
        self._ss       = smartstore
        self._notifier = notifier
        self._file     = file or CANCELLATIONS_FILE
        self._suppliers: dict = {}
        if dk_api is not None:
            self._suppliers["domaekkuk"] = dk_api
        if dm_cli is not None:
            self._suppliers["domaemae"]  = dm_cli
        self._order_repo = order_repo

    # ── 진입점 ───────────────────────────────────────────────────

    def run(self) -> dict:
        logger.info("취소 요청 감지 시작")
        data     = self._load()
        seen_ids = {r["product_order_id"] for r in data["cancellations"]}
        new_count = 0

        # 1. 신규 취소 요청 감지 — PAYED/DELIVERING 주문 조회 후 claimStatus 필터
        # (CANCEL_REQUEST는 productOrderStatuses 유효값이 아닌 claim 상태값 → 400 오류)
        raw_cancels: list = []
        for _status in ("PAYED", "DELIVERING"):
            try:
                raw_cancels += self._ss.get_orders(status=_status, days=3)
            except Exception as e:
                logger.warning("취소 요청 조회 실패 (status=%s): %s", _status, e)

        for item in raw_cancels:
            inner    = item.get("content", item)
            po       = inner.get("productOrder", {})
            # claimStatus == CANCEL_REQUEST 인 건만 처리
            if po.get("claimStatus") != "CANCEL_REQUEST":
                continue
            # 반품·교환 등 제외 — CANCEL 타입만 처리
            if po.get("claimType", "CANCEL") not in ("CANCEL", ""):
                continue
            order_id = po.get("productOrderId", "")
            if not order_id or order_id in seen_ids:
                continue

            entry          = self._make_entry(item)
            supplier_order = self._get_supplier_order(order_id)

            if not supplier_order or supplier_order["status"] in ("CANCELLED", "ERROR", "", "NEW"):
                self._route_ss_auto(entry)
            elif (supplier_order["tracking_number"]
                  or supplier_order["status"] in ("SHIPPED", "INVOICED", "DONE")):
                self._route_shipped(entry, supplier_order)
            elif supplier_order["status"] == "ORDERED":
                was_confirmed = self._was_ss_confirmed(order_id)
                self._route_ordered(entry, supplier_order, was_confirmed)
            else:
                # PENDING, STOCK_PENDING 등 → SS 취소 승인
                self._route_ss_auto(entry)

            data["cancellations"].append(entry)
            seen_ids.add(order_id)
            new_count += 1
            logger.info(
                "신규 취소 요청 처리: %s / %s → %s",
                order_id, entry.get("product_name"), entry.get("cancel_state"),
            )

        # 2. 진행 중인 취소 요청 폴링
        _active = [e for e in data["cancellations"]
                   if e.get("cancel_state", "") in (
                       "DENY_SENT", "URGENT_3DAY", "BUYER_WITHDREW_DENY_PENDING",
                       "REJECTED_WAIT_SHIP", "CANCEL_RECHECK_PENDING",
                   )]
        logger.info("진행 중인 취소 폴링 대상: %d건 %s",
                    len(_active),
                    [(e["product_order_id"], e.get("cancel_state")) for e in _active])
        for entry in data["cancellations"]:
            state = entry.get("cancel_state", "")
            if state in ("DENY_SENT", "URGENT_3DAY", "BUYER_WITHDREW_DENY_PENDING"):
                self._poll_deny_result(entry)
            elif state == "REJECTED_WAIT_SHIP":
                self._poll_tracking_for_rejected(entry)
            elif state == "CANCEL_RECHECK_PENDING":
                self._recheck_cancel_pre_deny(entry)

        # 3. 레이스 컨디션 감지 (발주확인 후 취소 요청)
        self._detect_race_conditions(data, seen_ids)

        self._save(data)
        total = len(data["cancellations"])
        logger.info("취소 요청 처리 완료: 신규 %d건 / 누적 %d건", new_count, total)
        return {"new": new_count, "total": total}

    # ── 라우팅 ───────────────────────────────────────────────────

    def _route_ss_auto(self, entry: dict):
        """case a: DB 발주 없음 또는 이미 취소/오류 → SS 취소 승인 직접 호출."""
        order_id = entry["product_order_id"]
        try:
            self._ss.approve_cancel(order_id)
            entry["cancel_state"] = "SS_AUTO"
            entry["result_label"] = "도매처 발주 없음 → SS 취소 승인 완료"
            logger.info("SS 취소 승인(발주없음): order_id=%s", order_id)
        except Exception as e:
            entry["cancel_state"] = "SS_AUTO_FAILED"
            entry["result_label"] = f"SS 취소 승인 실패: {e}"
            logger.error("SS 취소 승인 실패 (order_id=%s): %s", order_id, e)
        # orders.json을 CANCELLED로 → OrderPlacer가 재발주하지 않도록
        self._update_supplier_status(order_id, "CANCELLED")

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
        order_id = entry["product_order_id"]
        entry["supplier"]          = supplier
        entry["supplier_order_no"] = order_no

        extra = ""
        if was_confirmed:
            extra = (
                "⚠️ SS 발주확인 완료 후 취소 요청이 접수되었습니다.\n"
                "   도매처 취소 요청을 자동으로 전송합니다."
            )

        # ── Case 1: setOrdDeny 호출 전 구매자 취소 철회 감지 ────────────
        claim_check = self._get_ss_claim_status(order_id)
        if claim_check is None:
            # 429/오류 → 취소 철회로 단정 금지, 다음 회차에 재확인
            entry["cancel_state"] = "CANCEL_RECHECK_PENDING"
            entry["result_label"] = "SS claimStatus 조회 실패(429) — 다음 회차 재확인"
            logger.warning("setOrdDeny 보류 (SS 조회 실패): order_id=%s", order_id)
            return
        if claim_check == "WITHDRAWN":
            entry["cancel_state"] = "BUYER_WITHDREW_PRE_DENY"
            entry["result_label"] = "구매자 취소 철회 감지 — setOrdDeny 미전송, 발주 정상 진행"
            self._restore_order_status(order_id, "ORDERED")
            self._notify(
                entry,
                f"[취소철회] 구매자 취소 철회 감지 (setOrdDeny 전) — {entry['product_name']}",
                f"→ 구매자가 취소 요청을 철회했습니다.\n"
                f"  도매처 취소 요청(setOrdDeny)을 전송하지 않았습니다.\n"
                f"  도매처 발주({supplier} / 주문번호: {order_no})가 정상 진행됩니다.\n"
                f"  도매처 출고 시 송장이 자동으로 등록됩니다.",
            )
            return
        # claim_check == "CANCEL_REQUEST" → setOrdDeny 진행

        if not order_no:
            entry["cancel_state"] = "DENY_FAILED"
            entry["result_label"] = f"{supplier} 발주번호 없음 — 수동 처리 필요"
            self._notify(
                entry, f"[긴급] 도매처 취소 요청 실패 — 수동 처리 필요 — {entry['product_name']}",
                f"오류: {supplier} 발주번호 없음\n"
                f"→ 도매꾹/도매매 사이트에서 직접 취소 처리 후 SS 취소 승인하세요.\n{extra}",
            )
            return

        ok, msg = self._call_deny(supplier, order_no, entry.get("cancel_reason", ""))
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

    def _recheck_cancel_pre_deny(self, entry: dict):
        """CANCEL_RECHECK_PENDING: SS claimStatus 재확인 후 setOrdDeny 또는 철회 처리."""
        order_id    = entry["product_order_id"]
        supplier    = entry.get("supplier", "")
        order_no    = entry.get("supplier_order_no", "")

        claim_check = self._get_ss_claim_status(order_id)
        if claim_check is None:
            logger.warning("recheck 보류 (SS 조회 실패): order_id=%s", order_id)
            return  # keep CANCEL_RECHECK_PENDING

        if claim_check == "WITHDRAWN":
            entry["cancel_state"] = "BUYER_WITHDREW_PRE_DENY"
            entry["result_label"] = "구매자 취소 철회 확인 (재확인)"
            self._restore_order_status(order_id, "ORDERED")
            self._notify(
                entry,
                f"[취소철회] 구매자 취소 철회 확인 (재확인) — {entry['product_name']}",
                f"→ 재확인 결과 취소 요청이 철회됐습니다.\n"
                f"  도매처 발주({supplier} / {order_no})가 정상 진행됩니다.",
            )
            return

        # CANCEL_REQUEST 유지 → setOrdDeny 재시도
        if not order_no:
            entry["cancel_state"] = "DENY_FAILED"
            entry["result_label"] = f"{supplier} 발주번호 없음 — 수동 처리 필요 (재확인 후)"
            self._notify(
                entry, f"[긴급] 도매처 취소 요청 실패 (재확인 후) — {entry['product_name']}",
                f"오류: {supplier} 발주번호 없음\n"
                f"→ 도매꾹/도매매 사이트에서 직접 취소 처리 후 SS 취소 승인하세요.",
            )
            return

        ok, msg = self._call_deny(supplier, order_no, entry.get("cancel_reason", ""))
        if ok:
            entry["cancel_state"] = "DENY_SENT"
            entry["deny_sent_at"] = datetime.now(timezone.utc).isoformat()
            entry["result_label"] = f"{supplier} 취소 요청 전송 완료 (재확인 후) — 도매처 응답 대기 중"
            self._notify(
                entry, f"[취소처리] 도매처 취소 요청 전송 (재확인 후) — {entry['product_name']}",
                f"→ {supplier} 취소 요청(setOrdDeny)을 전송했습니다.\n"
                f"  도매처 응답을 10분마다 확인합니다.",
            )
        else:
            entry["cancel_state"] = "DENY_FAILED"
            entry["result_label"] = f"{supplier} 취소 요청 전송 실패 (재확인 후): {msg}"
            self._notify(
                entry, f"[긴급] 도매처 취소 요청 실패 (재확인 후) — {entry['product_name']}",
                f"오류: {msg}\n"
                f"→ 도매꾹/도매매 사이트에서 직접 주문({order_no})을 취소하세요.",
            )

    # ── 폴링 ─────────────────────────────────────────────────────

    def _poll_deny_result(self, entry: dict):
        """DENY_SENT / URGENT_3DAY / BUYER_WITHDREW_DENY_PENDING: 도매처 취소 처리 결과 폴링."""
        supplier = entry.get("supplier", "")
        order_no = entry.get("supplier_order_no", "")
        order_id = entry["product_order_id"]
        state    = entry.get("cancel_state", "DENY_SENT")

        bdays = _business_days_since(entry.get("deny_sent_at", entry["detected_at"]))
        entry["business_days_elapsed"] = bdays
        entry["last_checked_at"]       = datetime.now(timezone.utc).isoformat()

        result = self._get_cancel_result(supplier, order_no)
        logger.info("취소 결과 폴링: %s / %s → %s (영업일 %d일)", order_id, order_no, result, bdays)

        # ── Case 2: setOrdDeny 후 구매자 취소 철회 감지 ─────────────────
        claim_check    = self._get_ss_claim_status(order_id)
        # None(429/오류)은 철회로 단정 금지 → buyer_withdrew = False
        buyer_withdrew = (claim_check == "WITHDRAWN")

        if buyer_withdrew and result == "APPROVED":
            self._handle_buyer_withdrawal_after_approved(entry)
            return

        if buyer_withdrew and result == "REJECTED":
            self._handle_buyer_withdrawal_after_rejected(entry)
            return

        if buyer_withdrew and result == "PENDING":
            # 구매자 철회 + 도매처 미처리 → 상태 갱신 후 계속 폴링
            if state != "BUYER_WITHDREW_DENY_PENDING":
                entry["cancel_state"] = "BUYER_WITHDREW_DENY_PENDING"
                entry["result_label"] = "구매자 취소 철회, 도매처 응답 대기 중"
                self._notify(
                    entry,
                    f"[취소철회] 구매자 취소 철회 — 도매처 응답 대기 중 — {entry['product_name']}",
                    f"→ 구매자가 취소 요청을 철회했습니다.\n"
                    f"  도매처({supplier})의 취소 처리 결과를 계속 확인합니다.\n"
                    f"  도매처 거부 시 발주 정상 진행 / 승인 시 자동 재발주됩니다.",
                )
            if bdays >= 2 and state != "BUYER_WITHDREW_DENY_MANUAL":
                entry["cancel_state"] = "BUYER_WITHDREW_DENY_MANUAL"
                entry["result_label"] = f"{bdays}영업일 경과 — 수동 확인 필요"
                self._notify(
                    entry,
                    f"[수동처리] 구매자 취소 철회 후 도매처 {bdays}영업일 미처리 — {entry['product_name']}",
                    f"→ 구매자가 취소를 철회했으나 도매처({supplier}) 취소 요청이 "
                    f"{bdays}영업일 미처리입니다.\n"
                    f"  도매처 주문({order_no}) 상태를 직접 확인하세요.\n"
                    f"  취소 승인 시 재발주가 필요합니다.",
                )
            return

        # ── 구매자 철회 없음 — 기존 폴링 로직 ───────────────────────────
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
            # 도매처 취소 거부 → 출고 후 invoice_manager가 SS 송장 등록 자동 처리
            # (도매꾹 정책상 출고 후 취소 요청 불가 → 거부 응답 시점에는 항상 출고 전)
            entry["cancel_state"] = "REJECTED_WAIT_SHIP"
            entry["result_label"] = f"{supplier} 취소 거부 — invoice_manager 자동 처리 대기 중"
            self._notify(
                entry, f"[취소거부] 도매처 취소 거부 — 출고 대기 중 — {entry['product_name']}",
                f"→ {supplier}이(가) 취소를 거부했습니다.\n"
                f"  도매처가 출고하면 invoice_manager가 자동으로 SS 송장 등록을 처리합니다.\n"
                f"  추가 수동 조치 불필요 — 송장 등록 완료 시 자동으로 알림이 발송됩니다.",
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
        """REJECTED_WAIT_SHIP: 취소 거부 후 출고 대기 → 송장 확인.

        invoice_manager가 SS 송장 등록을 자동 처리하므로 dispatch는 호출하지 않는다.
        DB에 tracking_number가 등록되면 상태를 완료로 업데이트하고 알림만 전송한다.
        """
        order_id = entry["product_order_id"]
        entry["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        sup = self._get_supplier_order(order_id)
        if not sup or not sup["tracking_number"]:
            return
        tracking = sup["tracking_number"]
        entry["cancel_state"] = "INVOICE_REGISTERED"
        entry["invoice_number"] = tracking
        entry["result_label"] = (
            f"송장 등록 확인 — invoice_manager 자동 처리 완료 (송장: {tracking})"
        )
        self._notify(
            entry, f"[취소거부] 송장 등록 확인 — 자동 처리 완료 — {entry['product_name']}",
            f"→ {entry['supplier']} 취소 거부 후 출고가 확인되었습니다.\n"
            f"  송장번호: {tracking}\n"
            f"  invoice_manager가 SS 송장 등록을 자동으로 처리했습니다.\n"
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

            # 취소 요청 목록 (최근 1일) — PAYED/DELIVERING 조회 후 claimStatus 필터
            try:
                _raw_24h: list = []
                for _s in ("PAYED", "DELIVERING"):
                    try:
                        _raw_24h += self._ss.get_orders(status=_s, days=1)
                    except Exception:
                        pass
                cancel_24h = set()
                for _c in _raw_24h:
                    _inner = _c.get("content", _c)
                    _po    = _inner.get("productOrder", {})
                    if _po.get("claimStatus") == "CANCEL_REQUEST":
                        _pid = _po.get("productOrderId", "")
                        if _pid:
                            cancel_24h.add(_pid)
            except Exception:
                cancel_24h = set()

            for order_id in (recent_confirmed & cancel_24h) - seen_ids:
                if not order_id:
                    continue
                logger.warning("레이스 컨디션 감지: order_id=%s", order_id)
                entry = {
                    "product_order_id":      order_id,
                    "ss_order_id":           "",
                    "product_name":          "(재확인 필요)",
                    "quantity":              0,
                    "cancel_reason":         "발주확인 후 취소 요청 감지",
                    "buyer_name":            "",
                    "buyer_phone":           "",
                    "receiver_name":         "",
                    "receiver_phone":        "",
                    "receiver_address":      "",
                    "receiver_zipcode":      "",
                    "invoice_number":        "",
                    "supplier":              "",
                    "supplier_order_no":     "",
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

    # ── 구매자 취소 철회 처리 ────────────────────────────────────

    _SS_RETRY_DELAYS = (1.0, 2.0)

    def _get_ss_claim_status(self, product_order_id: str) -> str | None:
        """SS 주문의 claimStatus 반환.

        Returns:
            "CANCEL_REQUEST" — 취소 요청 유지 중
            "WITHDRAWN"      — 클레임 없음 (취소 철회)
            None             — 429/오류 → 철회로 단정 금지, 다음 회차 보류
        """
        for attempt, delay in enumerate((*self._SS_RETRY_DELAYS, None)):
            try:
                order_data = self._ss.get_product_order(product_order_id)
                if not order_data:
                    logger.warning("SS 주문 조회 빈 응답 — 불확실: %s", product_order_id)
                    return None
                po = order_data.get("productOrder", {})
                claim_status = po.get("claimStatus", "")
                po_status    = po.get("productOrderStatus", "")
                if claim_status == "CANCEL_REQUEST" or po_status == "CANCEL_REQUEST":
                    return "CANCEL_REQUEST"
                return "WITHDRAWN"
            except Exception as e:
                _code = getattr(getattr(e, "response", None), "status_code", None)
                if _code == 429 and delay is not None:
                    logger.warning(
                        "SS 주문 조회 429 — %.0f초 후 재시도 (attempt %d): %s",
                        delay, attempt + 1, product_order_id,
                    )
                    _time.sleep(delay)
                else:
                    logger.warning("SS claimStatus 조회 실패 — 불확실: %s %s", product_order_id, e)
                    return None
        logger.warning("SS 주문 조회 429 반복 실패 — 불확실: %s", product_order_id)
        return None

    def _restore_order_status(self, order_id: str, status: str) -> bool:
        """orders.json의 주문 상태를 변경해 OrderPlacer/InvoiceManager가 재처리하도록 한다."""
        if self._order_repo is None:
            logger.warning("order_repo 미설정 — 주문 상태 변경 불가: %s", order_id)
            return False
        try:
            order = self._order_repo.find(order_id)
            if order is None:
                logger.warning("주문 상태 변경 실패 — orders.json에 없음: %s", order_id)
                return False
            self._order_repo.update_status(order_id, status)
            logger.info("주문 상태 → %s (재처리 예정): %s", status, order_id)
            return True
        except Exception as e:
            logger.error("주문 상태 변경 실패 (%s → %s): %s", order_id, status, e)
            return False

    def _handle_buyer_withdrawal_after_approved(self, entry: dict):
        """Case 2-A: 도매처 취소 승인 + 구매자 SS 취소 철회 → 재발주."""
        order_id = entry["product_order_id"]
        supplier = entry.get("supplier", "")
        self._update_supplier_status(order_id, "CANCELLED")
        restored = self._restore_order_status(order_id, "NEW")
        if restored:
            entry["cancel_state"] = "BUYER_WITHDREW_REORDER"
            entry["result_label"] = f"{supplier} 취소 승인 + 구매자 철회 → 자동 재발주 예정"
            self._notify(
                entry,
                f"[취소철회] 구매자 취소 철회 + 도매처 취소 승인 → 재발주 예정 — {entry['product_name']}",
                f"→ 구매자가 취소 요청을 철회했으나 도매처({supplier})는 이미 취소를 승인했습니다.\n"
                f"  도매처 발주 상태를 CANCELLED로 변경했습니다.\n"
                f"  주문을 NEW로 복원 — 다음 발주 주기에 자동 재발주됩니다.",
            )
        else:
            entry["cancel_state"] = "BUYER_WITHDREW_REORDER_FAILED"
            entry["result_label"] = f"{supplier} 취소 승인 + 구매자 철회 → 재발주 실패 (수동 처리)"
            self._notify(
                entry,
                f"[긴급] 구매자 취소 철회 + 도매처 취소 승인 → 재발주 실패 — {entry['product_name']}",
                f"→ 구매자가 취소를 철회했고 도매처({supplier})는 이미 취소를 승인했습니다.\n"
                f"  orders.json에서 주문을 찾을 수 없어 자동 재발주에 실패했습니다.\n"
                f"  필요 조치:\n"
                f"  1. 도매꾹/도매매 사이트에서 해당 상품을 새로 주문\n"
                f"  2. SS 스마트스토어센터에서 발주확인 처리",
            )

    def _handle_buyer_withdrawal_after_rejected(self, entry: dict):
        """Case 2-B: 도매처 취소 거부 + 구매자 SS 취소 철회 → 발주 정상 진행."""
        supplier = entry.get("supplier", "")
        entry["cancel_state"] = "BUYER_WITHDREW_REJECTED"
        entry["result_label"] = f"{supplier} 취소 거부 + 구매자 철회 → 발주 정상 진행"
        self._notify(
            entry,
            f"[취소철회] 구매자 취소 철회 + 도매처 취소 거부 → 발주 정상 진행 — {entry['product_name']}",
            f"→ 구매자가 취소 요청을 철회했으며, 도매처({supplier})도 취소를 거부했습니다.\n"
            f"  도매처 발주는 이미 진행 중입니다.\n"
            f"  도매처 출고 시 송장이 자동으로 등록됩니다. 추가 조치 불필요.",
        )

    # ── 도매처 API ───────────────────────────────────────────────

    def _call_deny(self, supplier: str, order_no: str,
                   cancel_reason: str = "") -> tuple[bool, str]:
        client = self._suppliers.get(supplier)
        if not client:
            logger.error("setOrdDeny 클라이언트 없음: supplier=%s order_no=%s", supplier, order_no)
            return False, f"도매처 클라이언트 없음: {supplier}"
        memo = cancel_reason or "고객 취소요청"
        try:
            resp = client.cancel_order(order_no, memo=memo)
            logger.info("setOrdDeny 성공: supplier=%s order_no=%s resp=%s", supplier, order_no, resp)
            return True, "성공"
        except Exception as e:
            logger.error("setOrdDeny 실패: supplier=%s order_no=%s error=%s", supplier, order_no, e)
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
        # orders.json 우선 조회 (실제 운영 데이터)
        if self._order_repo is not None:
            try:
                order = self._order_repo.find(ss_order_id)
                if order:
                    return {
                        "supplier":          order.get("supplier", ""),
                        "supplier_order_no": order.get("supplier_order_no", ""),
                        "status":            order.get("status", ""),
                        "tracking_number":   order.get("tracking_number", ""),
                        "delivery_company":  order.get("delivery_company", ""),
                    }
            except Exception as e:
                logger.warning("orders.json 발주 조회 실패 (%s): %s", ss_order_id, e)
        # SQLAlchemy 폴백 (레거시)
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
        # orders.json 우선
        if self._order_repo is not None:
            try:
                self._order_repo.update_status(ss_order_id, status)
                return
            except Exception as e:
                logger.warning("orders.json 상태 업데이트 실패 (%s → %s): %s", ss_order_id, status, e)
        # SQLAlchemy 폴백
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

    def _make_entry(self, raw: dict) -> dict:
        """GET /v1/pay-order/seller/product-orders 응답 1건 → 취소 엔트리 생성.

        raw: {"content": {"productOrder": {...}, "order": {...}, "buyer": {...}}}
        """
        inner = raw.get("content", raw)
        po    = inner.get("productOrder", {})
        ord_  = inner.get("order", {})
        buyer = inner.get("buyer", {})
        addr  = po.get("shippingAddress") or {}

        # 취소 사유: productOrder.cancel.cancelReason 또는 claimReason
        cancel_obj    = po.get("cancel", {})
        cancel_reason = (
            cancel_obj.get("cancelReason")
            or cancel_obj.get("claimReason")
            or po.get("cancelReason")
            or po.get("claimReason")
            or "사유 미상"
        )

        base   = str(addr.get("baseAddress", "") or "")
        detail = str(addr.get("detailedAddress", "") or addr.get("detailAddress", "") or "")
        address_str = " ".join(filter(None, [base, detail]))

        return {
            # ── 주문 식별 ────────────────────────────────────────
            "product_order_id": po.get("productOrderId", ""),
            "ss_order_id":      ord_.get("orderId", "") or po.get("orderId", ""),
            # ── 상품 정보 ────────────────────────────────────────
            "product_name":     po.get("productName", "알 수 없음"),
            "quantity":         int(po.get("quantity") or 0),
            "cancel_reason":    cancel_reason,
            # ── 구매자 / 수령인 정보 ─────────────────────────────
            "buyer_name":       (buyer.get("buyerName") or ord_.get("ordererName", "")),
            "buyer_phone":      (buyer.get("buyerTel1") or ord_.get("ordererTel", "")),
            "receiver_name":    addr.get("name", ""),
            "receiver_phone":   (addr.get("tel1", "") or addr.get("tel2", "")),
            "receiver_address": address_str,
            "receiver_zipcode": str(addr.get("zipCode", "") or ""),
            # ── 기존 발송 정보 ───────────────────────────────────
            "invoice_number":   str(po.get("invoiceNumber") or ""),
            # ── 도매처 처리 상태 (발주 후 채워짐) ────────────────
            "supplier":              "",
            "supplier_order_no":     "",
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
                for entry in data.get("cancellations", []):
                    # 구버전 마이그레이션 (cancel_state 없는 경우)
                    if "cancel_state" not in entry:
                        entry.setdefault("cancel_state",          entry.get("result", "LEGACY"))
                        entry.setdefault("deny_sent_at",          "")
                        entry.setdefault("last_checked_at",       "")
                        entry.setdefault("supplier",              "")
                        entry.setdefault("supplier_order_no",     "")
                        entry.setdefault("business_days_elapsed", 0)
                    # 신규 필드 기본값 (구버전 호환)
                    entry.setdefault("ss_order_id",      "")
                    entry.setdefault("buyer_name",       "")
                    entry.setdefault("buyer_phone",      "")
                    entry.setdefault("receiver_name",    "")
                    entry.setdefault("receiver_phone",   "")
                    entry.setdefault("receiver_address", "")
                    entry.setdefault("receiver_zipcode", "")
                    entry.setdefault("invoice_number",   "")
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
            f"■ 알림 종류     : {subject}",
            f"■ 현재 상태     : {entry.get('result_label', '-')}",
            "",
            "■ 상품 정보",
            f"  상품명        : {entry.get('product_name', '-')}",
            f"  수량          : {entry.get('quantity', '-')}개",
            "",
            "■ 주문 정보",
            f"  SS 주문번호   : {entry.get('product_order_id', '-')}",
            f"  구매자        : {entry.get('buyer_name', '-')} / {entry.get('buyer_phone', '-')}",
            f"  수령인        : {entry.get('receiver_name', '-')} / {entry.get('receiver_phone', '-')}",
            f"  배송주소      : {entry.get('receiver_address', '-')}",
            "",
            "■ 도매처 정보",
            f"  도매처        : {entry.get('supplier', '-')}",
            f"  도매처 발주번호: {entry.get('supplier_order_no', '-')}",
            f"  기존 송장번호 : {entry.get('invoice_number', '-')}",
            "",
            f"■ 취소 사유     : {entry.get('cancel_reason', '-')}",
            f"■ 감지 시각     : {entry.get('detected_at', '-')}",
        ]
        if detail:
            lines += ["", "■ 필요한 조치", detail]
        try:
            self._notifier.send(subject=subject, body="\n".join(lines))
        except Exception as e:
            logger.error("이메일 전송 실패: %s", e)
