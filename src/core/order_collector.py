"""스마트스토어 신규 주문 자동 수집 → data/orders.json 저장"""
import logging
import traceback
from datetime import datetime
from src.api.smartstore import SmartstoreAPI
from src.core.order_repository import OrderRepository

logger = logging.getLogger(__name__)


_raw_logged = False  # 첫 번째 raw 항목만 전체 구조 출력


def _find_paths(obj, target: str, path: str = "") -> list[tuple[str, str]]:
    """obj 전체를 재귀 탐색해 target 문자열이 포함된 모든 경로 반환."""
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            results.extend(_find_paths(v, target, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            results.extend(_find_paths(v, target, f"{path}[{i}]"))
    elif isinstance(obj, str) and target in obj:
        results.append((path, obj))
    return results


def _parse_order_item(raw) -> dict | None:
    """Naver Commerce GET /v1/pay-order/seller/product-orders 응답 1건 정규화.

    실제 응답 구조:
      content.order.ordererName          → 수령인 이름
      content.order.ordererTel           → 전화번호
      content.productOrder.shippingAddress (없으면 takingAddress)
                           .zipCode / .baseAddress / .detailAddress  → 주소
      content.productOrder.productOrderId / channelProductNo / ...
      content.buyer.buyerName
    """
    global _raw_logged

    if not isinstance(raw, dict):
        logger.warning("파싱 불가 항목 스킵 (타입=%s): %s", type(raw).__name__, str(raw)[:200])
        return None

    import json as _json

    if not _raw_logged:
        _raw_logged = True
        key_types = {k: type(v).__name__ for k, v in raw.items()}
        logger.info("[SS API 진단] raw 최상위 keys+types=%s", key_types)
        logger.info("[SS API 진단] raw 전체(첫 1건):\n%s",
                    _json.dumps(raw, ensure_ascii=False, default=str)[:5000])

    # 수령인 이름·전화번호 경로 진단 (매 건마다 출력)
    for _target in ("유상환", "010-3564-8630"):
        _paths = _find_paths(raw, _target)
        if _paths:
            logger.info("[수령인 경로 탐색] '%s' 발견 경로: %s", _target, _paths)
        else:
            logger.info("[수령인 경로 탐색] '%s' raw에서 찾지 못함", _target)

    # "010-3564-8630" 전체 raw에서 경로 탐색
    _phone_paths = _find_paths(raw, "010-3564-8630")
    if _phone_paths:
        logger.info("[전화번호 경로 탐색] '010-3564-8630' 발견 경로: %s", _phone_paths)
    else:
        logger.info("[전화번호 경로 탐색] '010-3564-8630' raw에서 찾지 못함")

    inner = raw.get("content", raw)
    order = inner.get("order", {})
    po    = inner.get("productOrder", {})
    buyer = inner.get("buyer", {})

    order_id = (po.get("productOrderId")
                or raw.get("productOrderId")
                or order.get("orderId"))
    if not order_id:
        logger.warning("productOrderId 없음 — 건너뜀. keys=%s", list(raw.keys()))
        return None

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

    # 수령인 이름 · 전화번호 · 주소: productOrder.shippingAddress
    addr = po.get("shippingAddress") or {}
    receiver_name    = str(addr.get("name", "") or "")
    receiver_phone   = str(addr.get("tel1", "") or addr.get("tel", "") or "")
    receiver_zipcode = str(addr.get("zipCode",       "") or "")
    base             = str(addr.get("baseAddress",   "") or "")
    detail           = str(addr.get("detailAddress", "") or "")
    receiver_address = " ".join(filter(None, [base, detail]))

    # receiver_phone fallback: shippingAddress.tel이 비어 있으면 다른 필드 시도
    if not receiver_phone:
        receiver_phone = str(
            _first_valid(
                order.get("ordererTel"),
                buyer.get("buyerTel1"),
                buyer.get("buyerTel"),
                addr.get("tel1"),
                addr.get("receiverTel"),
            ) or ""
        )
        if receiver_phone:
            logger.info("[전화번호 fallback] shippingAddress.tel 없음 → %r 사용", receiver_phone)
        else:
            # 어디에도 없으면 raw 전체에서 전화번호 패턴 탐색해 경로 출력
            import re as _re
            _phone_pat = _re.compile(r"01[016789]-?\d{3,4}-?\d{4}")
            for _k, _v in _find_paths(raw, "010"):
                if _phone_pat.search(_v):
                    logger.info("[전화번호 경로 탐색] 발견: path=%s value=%r", _k, _v)
            logger.warning("[전화번호] 모든 fallback 실패 — 전화번호 없음")

    delivery_memo = str(po.get("deliveryMemo", "") or raw.get("deliveryMemo", "") or "")

    logger.info(
        "[SS 파싱 결과] order_id=%s | name=%r tel=%r zip=%r address=%r",
        order_id, receiver_name, receiver_phone, receiver_zipcode, receiver_address,
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
        "receiver_address":        receiver_address,
        "base_address":            base,
        "detail_address":          detail,
        "receiver_address_detail": detail,
        "receiver_zipcode":        receiver_zipcode,
        "delivery_memo":    delivery_memo,
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
