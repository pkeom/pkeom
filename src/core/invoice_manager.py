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
from src.db.database import get_session, is_db_initialized
from src.db.models import SupplierOrder

logger = logging.getLogger(__name__)

DELIVERY_COMPANY_MAP = {
    "CJ대한통운":         "CJGLS",
    "CJ로지스틱스":       "CJGLS",
    "롯데택배":           "HYUNDAI",   # 레거시: 현대택배→롯데택배, 네이버 코드는 HYUNDAI
    "롯데글로벌로지스틱스": "HYUNDAI",
    "한진택배":           "HANJIN",
    "우체국택배":         "EPOST",
    "로젠택배":           "KGB",       # 레거시: 로젠=KGB (구 KGB택배)
    "경동택배":           "KDEXP",
    "대신택배":           "DAESIN",
    "일양로지스":         "ILYANG",
    "천일택배":           "CHUNIL",
    "합동택배":           "HAPDONG",
    "GTX로지스":          "GTX",
    "건영택배":           "KUNYOUNG",
    "CVSnet편의점택배":   "CVSNET",
    "CU편의점택배":       "CU",
    # 아래 3개는 네이버 공식 코드 미확인 — 104119 발생 시 알림으로 잡힘
    "GS편의점택배":       "GS25",
    "쿠팡로지스틱스":     "COUPANG",
    "홈픽":               "HOMEPICK",
}

def _is_courier_code_error(exc: Exception) -> bool:
    """104119(택배사코드 확인) 에러 여부 판정."""
    resp_text = getattr(getattr(exc, "response", None), "text", "") or ""
    return "104119" in resp_text or "104119" in str(exc)


def _invoice_summary(order: dict) -> str:
    """이메일 본문용 주문 요약"""
    return "\n".join([
        f"주문ID:       {order['order_id']}",
        f"상품명:       {order.get('product_name', '')}",
        f"상품ID:       {order.get('product_id', '')}",
        f"옵션코드:     {order.get('option_code', '')}",
        f"수량:         {order.get('quantity', '')}",
        f"구매자:       {order.get('buyer_name', '')}",
        f"수령인:       {order.get('receiver_name', '')} / {order.get('receiver_phone', '')}",
        f"주소:         {order.get('receiver_address', '')} ({order.get('receiver_zipcode', '')})",
        f"배송메모:     {order.get('delivery_memo', '')}",
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
        ordered = (
            self._orders.find_by_status("ORDERED")
            + self._orders.find_by_status("ERROR")
        )
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

        # 2. 택배사 코드 매핑 확인 — 미등록이면 알림 후 보류
        if delivery_company and delivery_company not in DELIVERY_COMPANY_MAP:
            self._notify_courier_missing(order, delivery_company, mapped_code=None)
            return "pending"

        company_code = DELIVERY_COMPANY_MAP.get(delivery_company, delivery_company)

        # 3. 스마트스토어 송장 등록 (dry_run: 실제 전송 없이 로그만)
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
            if _is_courier_code_error(e):
                self._notify_courier_missing(order, delivery_company, mapped_code=company_code)
                return "pending"
            self._handle_error(
                order,
                "스마트스토어 송장 등록 오류",
                f"택배사={delivery_company}({company_code}), 송장번호={tracking_number}\n{e}",
            )
            return "error"

        # 4. SupplierOrder DB 업데이트 (DB 초기화된 경우에만)
        if is_db_initialized():
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

        # 5. orders.json INVOICED
        self._orders.update_status(order_id, "INVOICED")
        logger.info(
            "송장 등록 완료: order_id=%s, 택배사=%s(%s), 송장번호=%s",
            order_id, delivery_company, company_code, tracking_number,
        )
        return "invoiced"

    # ── 오류 처리 ────────────────────────────────────────────

    def _notify_courier_missing(self, order: dict, delivery_company: str, mapped_code: str | None):
        order_id = order["order_id"]
        if mapped_code:
            reason  = f"택배사 코드 매핑 오류: {delivery_company} → {mapped_code} (104119)"
            detail  = (f"네이버 deliveryCompanyCode '{mapped_code}'이(가) 유효하지 않습니다.\n"
                       f"DELIVERY_COMPANY_MAP에서 '{delivery_company}' 매핑을 수정하세요.")
        else:
            reason  = f"택배사 코드 매핑 필요: {delivery_company}"
            detail  = (f"도매처에서 받은 택배사명 '{delivery_company}'이(가) DELIVERY_COMPANY_MAP에\n"
                       f"등록되어 있지 않습니다. 네이버 deliveryCompanyCode를 확인 후 추가하세요.")
        logger.warning("택배사 코드 매핑 필요: %s (mapped=%s), order_id=%s",
                       delivery_company, mapped_code, order_id)
        if self._notifier:
            subject = f"[위탁판매] 택배사 코드 매핑 필요 — {delivery_company}"
            body = "\n".join([
                f"■ 알림 종류 : 택배사 코드 매핑 필요",
                f"■ 택배사명  : {delivery_company}",
                f"■ order_id  : {order_id}",
                f"■ 현재 상태 : 송장등록 보류 (다음 회차에 자동 재시도)",
                "",
                "■ 주문 정보",
                _invoice_summary(order),
                "",
                "■ 필요한 조치",
                detail,
            ])
            try:
                self._notifier.send(subject=subject, body=body)
            except Exception as e:
                logger.error("택배사매핑 이메일 전송 오류: %s", e)

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
            f"■ 알림 종류 : 송장 등록 실패",
            f"■ 실패 사유 : {reason}",
            f"■ 현재 상태 : ERROR (송장 미등록)",
            "",
            "■ 주문 정보",
            _invoice_summary(order),
            "",
            "■ 오류 상세",
            detail,
            "",
            "■ 필요한 조치",
            "  스마트스토어 판매자 센터에서 해당 주문의 송장번호를",
            "  직접 입력하여 발송 처리해 주세요.",
        ])
        try:
            self._notifier.send(subject=subject, body=body)
        except Exception as e:
            logger.error("송장실패 이메일 전송 오류: %s", e)
