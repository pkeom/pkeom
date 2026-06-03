"""스마트스토어 신규 주문 자동 수집 → data/orders.json 저장"""
import logging
from datetime import datetime
from src.api.smartstore import SmartstoreAPI
from src.core.order_repository import OrderRepository

logger = logging.getLogger(__name__)


def _parse_order_item(raw: dict) -> dict | None:
    """Naver Commerce product-orders/query 응답 1건을 정규화.

    API는 중첩 구조를 반환:
      raw["productOrder"]     → 상품주문 정보
      raw["order"]            → 구매자 정보
      raw["shippingAddress"]  → 수령인/배송지
      raw["deliveryMemo"]     → 배송 메모
    """
    po   = raw.get("productOrder", raw)   # 중첩 또는 flat 모두 대응
    ord_ = raw.get("order", {})
    addr = raw.get("shippingAddress", {})

    order_id = (
        po.get("productOrderId")
        or po.get("orderId")
        or raw.get("productOrderId")
        or raw.get("orderId")
    )
    if not order_id:
        logger.warning("order_id를 찾을 수 없는 응답 항목 건너뜀: %s", list(raw.keys()))
        return None

    now = datetime.now().isoformat(timespec="seconds")
    return {
        "order_id":         str(order_id),
        "ss_order_id":      str(po.get("orderId") or ord_.get("orderId") or order_id),
        "product_id":       str(po.get("productId", "")),
        "product_name":     po.get("productName", ""),
        "option_code":      str(po.get("optionCode") or po.get("optionId") or ""),
        "quantity":         int(po.get("quantity", 1)),
        "buyer_name":       ord_.get("ordererName") or raw.get("buyerName", ""),
        "receiver_name":    addr.get("name") or raw.get("receiverName", ""),
        "receiver_phone":   (addr.get("tel1") or addr.get("tel2")
                             or raw.get("receiverTel", "")),
        "receiver_address": addr.get("addressStr", "") or raw.get("receiverAddr", ""),
        "receiver_zipcode": addr.get("zipCode", "") or raw.get("receiverZipcode", ""),
        "delivery_memo":    raw.get("deliveryMemo", ""),
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
