"""스마트스토어 신규 주문 자동 수집 → data/orders.json 저장"""
import logging
import traceback
from datetime import datetime
from src.api.smartstore import SmartstoreAPI
from src.core.order_repository import OrderRepository

logger = logging.getLogger(__name__)


_raw_logged = False  # 첫 번째 raw 항목만 전체 구조 출력


def _find_paths(obj, target: str, path: str = "") -> list[tuple[str, str]]:
    """obj 안에서 target 문자열을 포함하는 모든 리프 값의 경로를 반환."""
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
      raw["order"]        → orderId
      raw["productOrder"] → productOrderId, channelProductNo, productName,
                            quantity, optionCode,
                            takingAddress.name/tel/zipCode/baseAddress/detailAddress  ← 배송지
      raw["buyer"]        → buyerName
    """
    global _raw_logged

    if not isinstance(raw, dict):
        logger.warning("파싱 불가 항목 스킵 (타입=%s): %s", type(raw).__name__, str(raw)[:200])
        return None

    import json as _json

    # 첫 번째 항목: 최상위 키 + 타입 출력
    if not _raw_logged:
        _raw_logged = True
        key_types = {k: type(v).__name__ for k, v in raw.items()}
        logger.info("[SS API 진단] raw 최상위 keys+types=%s", key_types)
        logger.info("[SS API 진단] raw 전체(첫 1건):\n%s",
                    _json.dumps(raw, ensure_ascii=False, default=str)[:5000])

    # 매 항목마다 '유승윤' 경로 탐색 → 수령인 필드 위치 확인용
    _NAME_TARGET = "유승윤"
    _found = _find_paths(raw, _NAME_TARGET)
    if _found:
        logger.info(
            "[SS 수령인 경로 탐색] '%s' 발견 경로: %s",
            _NAME_TARGET,
            [(p, v) for p, v in _found],
        )
    else:
        logger.info("[SS 수령인 경로 탐색] '%s' 이번 항목에서 미발견", _NAME_TARGET)

    inner = raw.get("content", raw)
    order = inner.get("order", {})
    po    = inner.get("productOrder", {})
    buyer = inner.get("buyer", {})

    logger.info("[SS productOrder 키 목록] keys=%s", list(po.keys()))
    logger.info("[SS productOrder 전체값]\n%s",
                _json.dumps(po, ensure_ascii=False, default=str)[:3000])

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
    logger.info(
        "product_id 추출: channelProductNo=%r productId=%r originProductNo=%r → product_id=%r",
        _cpno, _pid, _origno, product_id,
    )

    # 배송지: productOrder.takingAddress — 실제 SS API 응답에서 수령인 정보가 여기에 있음
    addr = po.get("takingAddress", {})
    logger.info("[SS takingAddress 키 목록] keys=%s", list(addr.keys()))
    logger.info("[SS takingAddress 전체값]\n%s",
                _json.dumps(addr, ensure_ascii=False, default=str))
    receiver_name    = str(addr.get("name",          "") or "")
    logger.info(
        "[SS takingAddress.name 진단] order_id=%s | takingAddress.name=%r (수령인 이름 확인용)",
        po.get("productOrderId") or order.get("orderId"), receiver_name,
    )
    _phone_from_addr = addr.get("tel", "") or ""
    _phone_fallback  = (buyer.get("buyerTel1", "") or
                        order.get("ordererTel", "") or "")
    if not _phone_from_addr and _phone_fallback:
        logger.info(
            "[SS receiver_phone fallback] order_id=%s takingAddress.tel 없음 → buyerTel1/ordererTel 사용: %r",
            po.get("productOrderId") or order.get("orderId"), _phone_fallback,
        )
    receiver_phone   = str(_phone_from_addr or _phone_fallback)
    receiver_zipcode = str(addr.get("zipCode",       "") or "")
    base             = str(addr.get("baseAddress",   "") or "")
    detail           = str(addr.get("detailAddress", "") or "")
    receiver_address = " ".join(filter(None, [base, detail]))
    delivery_memo    = str(po.get("deliveryMemo", "") or raw.get("deliveryMemo", "") or "")

    logger.info(
        "[SS delivery 파싱 결과] order_id=%s | name=%r tel=%r zipCode=%r address=%r",
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
        "receiver_address": receiver_address,
        "base_address":     base,
        "detail_address":   detail,
        "receiver_zipcode": receiver_zipcode,
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
