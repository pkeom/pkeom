"""스마트스토어 신규 주문 자동 수집 → data/orders.json 저장"""
import logging
import traceback
from datetime import datetime
from src.api.smartstore import SmartstoreAPI
from src.core.order_repository import OrderRepository

logger = logging.getLogger(__name__)


_raw_logged = False  # 첫 번째 raw 항목만 전체 구조 출력


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
    global _raw_logged

    if not isinstance(raw, dict):
        logger.warning("파싱 불가 항목 스킵 (타입=%s): %s", type(raw).__name__, str(raw)[:200])
        return None

    # 첫 번째 항목의 전체 구조를 INFO로 출력 → 실제 API 응답 경로 파악용
    if not _raw_logged:
        _raw_logged = True
        import json as _json
        logger.info("[SS API 응답 구조 진단] raw 최상위 keys=%s", list(raw.keys()))
        logger.info("[SS API 응답 구조 진단] raw 전체(첫 1건):\n%s",
                    _json.dumps(raw, ensure_ascii=False, default=str)[:5000])

    # 실제 응답: {"productOrderId": "...", "content": {"order":{}, "productOrder":{}, ...}}
    inner = raw.get("content", raw)
    order = inner.get("order", {})
    po    = inner.get("productOrder", {})
    buyer = inner.get("buyer", {})

    # delivery 위치를 여러 경로에서 탐색
    dlv = (
        inner.get("delivery")                           # 표준 경로
        or inner.get("shippingAddress")                 # 일부 버전 대안
        or raw.get("delivery")                          # content 없는 플랫 응답
        or raw.get("shippingAddress")
        or {}
    )

    logger.debug(
        "[SS delivery 진단] inner keys=%s | delivery keys=%s | dlv=%s",
        list(inner.keys()),
        list(dlv.keys()) if isinstance(dlv, dict) else type(dlv).__name__,
        str(dlv)[:500],
    )

    order_id = (po.get("productOrderId")
                or raw.get("productOrderId")
                or order.get("orderId"))
    if not order_id:
        logger.warning("productOrderId 없음 — 건너뜀. keys=%s", list(raw.keys()))
        return None

    # channelProductNo 우선 (mappings.json의 ss_product_id와 동일 체계)
    # → productId → originProductNo 순으로 fallback
    # nested(productOrder) 와 flat(raw 최상위) 양쪽을 모두 시도
    def _first_valid(*vals):
        for v in vals:
            if v not in (None, "", 0):
                return v
        return None

    _cpno   = _first_valid(po.get("channelProductNo"), raw.get("channelProductNo"))
    _pid    = _first_valid(po.get("productId"),        raw.get("productId"))
    _origno = _first_valid(po.get("originProductNo"),  raw.get("originProductNo"))
    product_id = str(_cpno if _cpno is not None else
                     _pid  if _pid  is not None else
                     _origno if _origno is not None else "")
    logger.info(
        "product_id 추출: channelProductNo=%r productId=%r originProductNo=%r → product_id=%r",
        _cpno, _pid, _origno, product_id,
    )

    # receiver 필드: 표준 키 + 대안 키 순으로 탐색
    receiver_name = (
        dlv.get("receiverName")
        or dlv.get("name")
        or dlv.get("recipientName")
        or buyer.get("buyerName", "")
    ) or ""
    receiver_phone = (
        dlv.get("receiverTel1")
        or dlv.get("receiverTel2")
        or dlv.get("tel")
        or dlv.get("mobile")
        or buyer.get("buyerTel1", "")
    ) or ""
    receiver_zipcode = (
        dlv.get("zipCode")
        or dlv.get("postcode")
        or dlv.get("addressInfoList", [{}])[0].get("zipCode", "") if isinstance(dlv.get("addressInfoList"), list) else ""
    ) or ""

    # 주소: baseAddress + detailAddress, 또는 address 단일 필드
    base   = dlv.get("baseAddress") or dlv.get("address", "")
    detail = dlv.get("detailAddress") or dlv.get("addressDetail", "")
    address = " ".join(filter(None, [base, detail]))

    logger.info(
        "[SS delivery 파싱 결과] order_id=%s | receiver_name=%r phone=%r zipcode=%r address=%r",
        order_id, receiver_name, receiver_phone, receiver_zipcode, address,
    )

    now = datetime.now().isoformat(timespec="seconds")
    return {
        "order_id":         str(order_id),
        "ss_order_id":      str(order.get("orderId") or order_id),
        "product_id":       product_id,
        "product_name":     po.get("productName", ""),
        "option_code":      str(po.get("optionCode") or raw.get("optionCode") or ""),
        "quantity":         int(po.get("quantity", 1)),
        "buyer_name":       buyer.get("buyerName", ""),
        "receiver_name":    receiver_name,
        "receiver_phone":   receiver_phone,
        "receiver_address": address,
        "receiver_zipcode": receiver_zipcode,
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
            for idx, raw in enumerate(raw_orders):
                try:
                    order = _parse_order_item(raw)
                    if order:
                        parsed.append(order)
                except Exception:
                    logger.error(
                        "주문 항목 파싱 예외 (index=%d) raw=%s\n%s",
                        idx, str(raw)[:300], traceback.format_exc(),
                    )

            # ERROR 상태 주문 재시도: 매 회차마다 무조건 NEW로 전환
            # (API 반환 여부 무관 — 이전 로직은 parsed_ids 조건으로 오래된 ERROR 주문이 영구 고착됨)
            error_orders = self._repo.find_by_status("ERROR")
            retried = 0
            for err_order in error_orders:
                self._repo.update_status(err_order["order_id"], "NEW")
                retried += 1
                logger.info("ERROR → NEW 재시도: order_id=%s", err_order["order_id"])

            added = self._repo.add_many(parsed)
            logger.info(
                "주문 수집 완료: API %d건 중 파싱성공 %d건 / 신규저장 %d건 / ERROR→NEW 재시도 %d건",
                len(raw_orders), len(parsed), added, retried,
            )
            return added + retried

        except Exception as e:
            logger.error("주문 수집 오류: %s\n%s", e, traceback.format_exc())
            raise
