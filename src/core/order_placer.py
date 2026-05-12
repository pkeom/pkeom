"""매핑 테이블 기반 도매처 자동 발주

흐름:
  orders.json [NEW] → 매핑 조회 → 도매처 발주 API 호출
    성공 → SupplierOrder DB 저장 → orders.json [ORDERED]
    실패 → orders.json [ERROR]   → 이메일 알림
"""
import logging
from src.api.domaekkuk import DomaekkukAPI
from src.api.domaemae import DomaemaeClient
from src.core.mapping_repository import MappingRepository
from src.core.order_repository import OrderRepository
from src.db.database import get_session
from src.db.models import SupplierOrder

logger = logging.getLogger(__name__)


def _extract_supplier_order_no(result) -> str:
    """도매처 API 응답(dict 또는 str)에서 발주번호 추출"""
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        return str(
            result.get("order_no")
            or result.get("orderNo")
            or result.get("ordNo")
            or result.get("no")
            or ""
        )
    return ""


def _order_summary(order: dict, mapping: dict | None = None) -> str:
    """이메일 본문용 주문 요약 텍스트"""
    lines = [
        f"주문ID:   {order['order_id']}",
        f"상품명:   {order.get('product_name', '')}",
        f"상품ID:   {order.get('product_id', '')}",
        f"옵션코드: {order.get('option_code', '')}",
        f"수량:     {order.get('quantity', '')}",
        f"수령인:   {order.get('receiver_name', '')} / {order.get('receiver_phone', '')}",
        f"주소:     {order.get('receiver_address', '')} ({order.get('receiver_zipcode', '')})",
        f"배송메모: {order.get('delivery_memo', '')}",
    ]
    if mapping:
        lines.append(
            f"도매처:   {mapping['supplier']} / 상품번호 {mapping['supplier_product_id']}"
        )
    return "\n".join(lines)


class OrderPlacer:
    def __init__(
        self,
        domaekkuk: DomaekkukAPI,
        domaemae: DomaemaeClient,
        mapping_repo: MappingRepository | None = None,
        order_repo: OrderRepository | None = None,
        notifier=None,
    ):
        self.clients   = {"domaekkuk": domaekkuk, "domaemae": domaemae}
        self._mappings = mapping_repo or MappingRepository()
        self._orders   = order_repo   or OrderRepository()
        self._notifier = notifier   # EmailNotifier | None

    # ── 진입점 ───────────────────────────────────────────────

    def run(self) -> dict:
        """NEW 상태 주문 전체 발주 처리. 반환값: {"total": n, "ordered": n, "error": n}"""
        pending = self._orders.find_by_status("NEW")
        stats   = {"total": len(pending), "ordered": 0, "error": 0}

        if not pending:
            logger.info("발주 대상 주문 없음")
            return stats

        logger.info("자동 발주 시작: %d건", stats["total"])
        for order in pending:
            success = self._place_one(order)
            if success:
                stats["ordered"] += 1
            else:
                stats["error"] += 1

        logger.info(
            "자동 발주 완료: 전체 %d건 / 성공 %d건 / 실패 %d건",
            stats["total"], stats["ordered"], stats["error"],
        )
        return stats

    # ── 단건 발주 ────────────────────────────────────────────

    def _place_one(self, order: dict) -> bool:
        """단건 발주 처리. 성공 True / 실패 False."""
        order_id = order["order_id"]

        # 1. 매핑 조회
        mapping = self._mappings.find(
            ss_product_id=order["product_id"],
            ss_option_id=order.get("option_code", ""),
        )
        if not mapping:
            self._handle_error(
                order=order,
                mapping=None,
                reason="매핑 없음",
                detail=(
                    f"상품ID '{order['product_id']}' / 옵션 '{order.get('option_code', '')}'"
                    "에 대한 도매처 매핑이 존재하지 않습니다.\n"
                    "상품 매핑 설정 탭에서 매핑을 추가해 주세요."
                ),
            )
            return False

        # 2. 도매처 발주 API 호출
        shipping = {
            "name":    order["receiver_name"],
            "phone":   order["receiver_phone"],
            "address": order["receiver_address"],
            "zipcode": order["receiver_zipcode"],
            "memo":    order.get("delivery_memo", ""),
        }
        try:
            client = self.clients[mapping["supplier"]]
            kwargs = {}
            if mapping["supplier"] == "domaemae":
                kwargs["option_name"] = order.get("option_code", "")
            result = client.place_order(
                mapping["supplier_product_id"], order["quantity"], shipping, **kwargs
            )
            supplier_order_no = _extract_supplier_order_no(result)
        except Exception as e:
            self._handle_error(
                order=order,
                mapping=mapping,
                reason="발주 API 오류",
                detail=str(e),
            )
            return False

        # 3. 발주 성공 저장
        try:
            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=order_id,
                    supplier=mapping["supplier"],
                    supplier_product_id=mapping["supplier_product_id"],
                    supplier_order_no=supplier_order_no,
                    quantity=order["quantity"],
                    status="ORDERED",
                ))
        except Exception as e:
            logger.error("SupplierOrder DB 저장 실패: order_id=%s, %s", order_id, e)

        self._orders.update_supplier_info(order_id, mapping["supplier"], supplier_order_no)
        self._orders.update_status(order_id, "ORDERED")
        logger.info(
            "발주 완료: order_id=%s → %s 발주번호=%s",
            order_id, mapping["supplier"], supplier_order_no or "(미확인)",
        )
        return True

    # ── 오류 처리 ────────────────────────────────────────────

    def _handle_error(self, order: dict, mapping: dict | None,
                      reason: str, detail: str):
        order_id = order["order_id"]
        logger.error("발주 실패 [%s]: order_id=%s — %s", reason, order_id, detail)
        self._orders.update_status(order_id, "ERROR")
        self._notify_error(order, mapping, reason, detail)

    def _notify_error(self, order: dict, mapping: dict | None,
                      reason: str, detail: str):
        if not self._notifier:
            return
        subject = f"[위탁판매] 발주 실패 알림 — {reason}"
        body = "\n".join([
            f"■ 발주 실패 사유: {reason}",
            "",
            "■ 주문 정보",
            _order_summary(order, mapping),
            "",
            "■ 오류 상세",
            detail,
        ])
        try:
            self._notifier.send(subject=subject, body=body)
        except Exception as e:
            logger.error("발주실패 이메일 전송 오류: %s", e)
