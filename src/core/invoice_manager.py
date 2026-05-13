"""도매처 송장 수집 → 스마트스토어 자동 등록

흐름:
  orders.json [ORDERED] → 도매처 송장 조회
    미발송  → 스킵 (다음 주기에 재시도)
    발송됨  → 스마트스토어 dispatch_order → orders.json [INVOICED]
    실패    → orders.json [ERROR] → 이메일 알림
"""
import logging
from src.api.smartstore import SmartstoreAPI
from src.api.domaekkuk import DomaekkukAPI
from src.api.domaemae import DomaemaeClient
from src.core.order_repository import OrderRepository
from src.db.database import get_session
from src.db.models import SupplierOrder

logger = logging.getLogger(__name__)

DELIVERY_COMPANY_MAP = {
    "CJ대한통운":         "CJGLS",
    "CJ로지스틱스":       "CJGLS",
    "롯데택배":           "LOTTE",
    "롯데글로벌로지스틱스": "LOTTE",
    "한진택배":           "HANJIN",
    "우체국택배":         "EPOST",
    "로젠택배":           "LOGEN",
    "GS편의점택배":       "GS25",
    "쿠팡로지스틱스":     "COUPANG",
    "홈픽":               "HOMEPICK",
    "경동택배":           "KDEXP",
    "대신택배":           "DAESIN",
    "일양로지스":         "ILYANG",
}


def _invoice_summary(order: dict) -> str:
    """이메일 본문용 주문 요약"""
    return "\n".join([
        f"주문ID:       {order['order_id']}",
        f"상품명:       {order.get('product_name', '')}",
        f"수령인:       {order.get('receiver_name', '')} / {order.get('receiver_phone', '')}",
        f"도매처:       {order.get('supplier', '')}",
        f"도매발주번호: {order.get('supplier_order_no', '')}",
    ])


class InvoiceManager:
    def __init__(
        self,
        ss_api: SmartstoreAPI,
        domaekkuk: DomaekkukAPI,
        domaemae: DomaemaeClient,
        order_repo: OrderRepository | None = None,
        notifier=None,
        dry_run: bool = False,
    ):
        self.ss_api    = ss_api
        self.clients   = {"domaekkuk": domaekkuk, "domaemae": domaemae}
        self._orders   = order_repo or OrderRepository()
        self._notifier = notifier
        self._dry_run  = dry_run

    # ── 진입점 ───────────────────────────────────────────────

    def run(self) -> dict:
        """ORDERED 상태 주문 송장 동기화.
        반환값: {"total": n, "invoiced": n, "pending": n, "error": n}
        """
        ordered = self._orders.find_by_status("ORDERED")
        stats   = {"total": len(ordered), "invoiced": 0, "pending": 0, "error": 0}

        if not ordered:
            logger.info("송장 동기화 대상 없음")
            return stats

        logger.info("송장 동기화 시작: %d건", stats["total"])
        for order in ordered:
            result = self._sync_one(order)
            stats[result] += 1

        logger.info(
            "송장 동기화 완료: 전체 %d건 / 등록 %d건 / 대기 %d건 / 실패 %d건",
            stats["total"], stats["invoiced"], stats["pending"], stats["error"],
        )
        return stats

    # ── 단건 처리 ────────────────────────────────────────────

    def _sync_one(self, order: dict) -> str:
        """단건 송장 처리. 반환값: 'invoiced' | 'pending' | 'error'"""
        order_id          = order["order_id"]
        supplier          = order.get("supplier", "")
        supplier_order_no = order.get("supplier_order_no", "")

        if not supplier or not supplier_order_no:
            logger.warning("도매처 정보 누락: order_id=%s", order_id)
            self._handle_error(order, "도매처 정보 누락",
                               "발주 완료된 주문에 supplier/supplier_order_no 값이 없습니다.")
            return "error"

        # 1. 도매처에서 송장 조회
        try:
            client   = self.clients[supplier]
            tracking = client.get_order_tracking(supplier_order_no)
        except Exception as e:
            self._handle_error(order, "송장 조회 오류", str(e))
            return "error"

        tracking_number  = str(tracking.get("tracking_number", "")).strip()
        delivery_company = str(tracking.get("delivery_company", "")).strip()

        if not tracking_number:
            logger.debug("미발송: order_id=%s (도매발주번호=%s)", order_id, supplier_order_no)
            return "pending"

        # 2. 스마트스토어 송장 등록 (dry_run: 실제 전송 없이 로그만)
        company_code = DELIVERY_COMPANY_MAP.get(delivery_company, delivery_company)
        if self._dry_run:
            logger.info(
                "[DRY_RUN] 송장 등록 스킵: order_id=%s, 택배사=%s(%s), 송장=%s",
                order_id, delivery_company, company_code, tracking_number,
            )
            self._orders.update_status(order_id, "INVOICED")
            return "invoiced"
        try:
            self.ss_api.dispatch_order(order_id, company_code, tracking_number)
        except Exception as e:
            self._handle_error(
                order,
                "스마트스토어 송장 등록 오류",
                f"택배사={delivery_company}({company_code}), 송장번호={tracking_number}\n{e}",
            )
            return "error"

        # 3. SupplierOrder DB 업데이트
        try:
            with get_session() as session:
                sup = (
                    session.query(SupplierOrder)
                    .filter_by(ss_order_id=order_id, supplier=supplier)
                    .first()
                )
                if sup:
                    sup.tracking_number  = tracking_number
                    sup.delivery_company = delivery_company
                    sup.status           = "SHIPPED"
        except Exception as e:
            logger.error("SupplierOrder DB 업데이트 실패: order_id=%s, %s", order_id, e)

        # 4. orders.json INVOICED
        self._orders.update_status(order_id, "INVOICED")
        logger.info(
            "송장 등록 완료: order_id=%s, 택배사=%s(%s), 송장번호=%s",
            order_id, delivery_company, company_code, tracking_number,
        )
        return "invoiced"

    # ── 오류 처리 ────────────────────────────────────────────

    def _handle_error(self, order: dict, reason: str, detail: str):
        order_id = order["order_id"]
        logger.error("송장 처리 실패 [%s]: order_id=%s — %s", reason, order_id, detail)
        self._orders.update_status(order_id, "ERROR")
        self._notify_error(order, reason, detail)

    def _notify_error(self, order: dict, reason: str, detail: str):
        if not self._notifier:
            return
        subject = f"[위탁판매] 송장 등록 실패 알림 — {reason}"
        body = "\n".join([
            f"■ 실패 사유: {reason}",
            "",
            "■ 주문 정보",
            _invoice_summary(order),
            "",
            "■ 오류 상세",
            detail,
        ])
        try:
            self._notifier.send(subject=subject, body=body)
        except Exception as e:
            logger.error("송장실패 이메일 전송 오류: %s", e)
