"""스마트스토어 신규 주문 자동 수집 → data/orders.json 저장"""
import logging
from datetime import datetime
from src.api.smartstore import SmartstoreAPI
from src.core.order_repository import OrderRepository

logger = logging.getLogger(__name__)


def _parse_order_item(raw) -> dict | None:
    """Naver Commerce GET /v1/pay-order/seller/product-orders 응답 1건 정규화.

    실제 응답 구조:
      raw["order"]         → orderId, orderDate
      raw["productOrder"]  → productOrderId, productId, channelProductNo,
                             productName, quantity, optionCode
      raw["buyer"]         → buyerName, buyerTel1
      raw["delivery"]      → receiverName, receiverTel1,
                             baseAddress, detailAddress, zipCode
    """
    if not isinstance(raw, dict):
        logger.warning("파싱 불가 항목 스킵 (타입=%s): %s", type(raw).__name__, str(raw)[:200])
        return None

    # 실제 응답: {"productOrderId": "...", "content": {"order":{}, "productOrder":{}, ...}}
    inner = raw.get("content", raw)
    order = inner.get("order", {})
    po    = inner.get("productOrder", {})
    buyer = inner.get("buyer", {})
    dlv   = inner.get("delivery", {})

    order_id = (po.get("productOrderId")
                or raw.get("productOrderId")
                or order.get("orderId"))
    if not order_id:
        logger.warning("productOrderId 없음 — 건너뜀. keys=%s", list(raw.keys()))
        return None

    product_id = str(po.get("productId") or po.get("channelProductNo") or "")

    address = " ".join(filter(None, [
        dlv.get("baseAddress", ""),
        dlv.get("detailAddress", ""),
    ]))

    now = datetime.now().isoformat(timespec="seconds")
    return {
        "order_id":         str(order_id),
        "ss_order_id":      str(order.get("orderId") or order_id),
        "product_id":       product_id,
        "product_name":     po.get("productName", ""),
        "option_code":      str(po.get("optionCode") or po.get("optionId") or ""),
        "quantity":         int(po.get("quantity", 1)),
        "buyer_name":       buyer.get("buyerName", ""),
        "receiver_name":    dlv.get("receiverName", ""),
        "receiver_phone":   dlv.get("receiverTel1") or dlv.get("receiverTel2", ""),
        "receiver_address": address,
        "receiver_zipcode": dlv.get("zipCode", ""),
        "delivery_memo":    dlv.get("deliveryMemo") or raw.get("deliveryMemo", ""),
        "status":           "NEW",
        "collected_at":     now,
        "updated_at":       now,
    }


class OrderCollector:
    def __init__(self, api: SmartstoreAPI,
                 repo: OrderRepository | None = None):
        self.api   = api
        self._repo = repo or OrderRepository()

    def run(self) -> int:
        """신규 주문 수집. 반환값: 추가된 건수."""
        logger.info("주문 수집 시작")
        try:
            raw_orders = self.api.get_orders(status="PAYED", days=3)
            if not raw_orders:
                logger.info("신규 주문 없음")
                return 0

            parsed = []
            for raw in raw_orders:
                order = _parse_order_item(raw)
                if order:
                    parsed.append(order)

            added = self._repo.add_many(parsed)
            logger.info("주문 수집 완료: API %d건 중 신규 %d건 저장", len(parsed), added)
            return added

        except Exception as e:
            logger.error("주문 수집 오류: %s", e, exc_info=True)
            raise
