"""매핑 테이블 기반 도매처 자동 발주

흐름:
  orders.json [NEW] → 매핑 조회 → (예산 확인) → 도매처 발주 API 호출
    예산 충분  → 발주 → SupplierOrder DB 저장 → orders.json [ORDERED] → 예산 차감
    예산 부족  → pending_orders.json [PENDING] → orders.json [PENDING] → 이메일 알림
    매핑 없음  → orders.json [ERROR]   → 이메일 알림
"""
import logging
from src.api.domaekkuk import DomaekkukAPI
from src.api.domaemae import DomaemaeClient
from src.core.mapping_repository import MappingRepository
from src.core.order_repository import OrderRepository
from src.db.database import get_session
from src.db.models import SupplierOrder

logger = logging.getLogger(__name__)

DEFAULT_SHIPPING = 3_000  # 배송비 기본값(원): 도매처 배송비를 모를 경우 사용


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
        budget=None,        # BudgetManager | None
        pending=None,       # PendingOrderRepository | None
        dry_run: bool = False,
    ):
        self.clients   = {"domaekkuk": domaekkuk, "domaemae": domaemae}
        self._mappings = mapping_repo or MappingRepository()
        self._orders   = order_repo   or OrderRepository()
        self._notifier = notifier
        self._budget   = budget
        self._pending  = pending
        self._dry_run  = dry_run

    # ── 진입점 ───────────────────────────────────────────────

    def run(self) -> dict:
        """NEW 상태 주문 전체 발주 처리.
        반환값: {"total": n, "ordered": n, "error": n, "deferred": n}
        """
        new_orders = self._orders.find_by_status("NEW")
        stats = {"total": len(new_orders), "ordered": 0, "error": 0, "deferred": 0}

        if not new_orders:
            logger.info("발주 대상 주문 없음")
            return stats

        logger.info("자동 발주 시작: %d건", stats["total"])

        if self._budget is None:
            # 예산 관리 없음 — 기존 동작
            for order in new_orders:
                if self._place_one(order):
                    stats["ordered"] += 1
                else:
                    stats["error"] += 1
        else:
            self._run_with_budget(new_orders, stats)

        logger.info(
            "자동 발주 완료: 전체 %d건 / 성공 %d건 / 실패 %d건 / 대기 %d건",
            stats["total"], stats["ordered"], stats["error"], stats["deferred"],
        )
        return stats

    # ── 예산 관리 발주 ───────────────────────────────────────

    def _run_with_budget(self, orders: list[dict], stats: dict):
        """예산 범위 내 최대 발주 + 초과분 대기 전환"""
        balance = self._budget.get_balance()

        # 매핑 조회 + 비용 추정
        costed: list[tuple[dict, dict, int]] = []  # (order, mapping, cost)
        for order in orders:
            mapping = self._mappings.find(
                ss_product_id=order["product_id"],
                ss_option_id=order.get("option_code", ""),
            )
            if not mapping:
                self._handle_error(
                    order=order, mapping=None, reason="매핑 없음",
                    detail=(
                        f"상품ID '{order['product_id']}' / 옵션 '{order.get('option_code', '')}'"
                        "에 대한 도매처 매핑이 존재하지 않습니다."
                    ),
                )
                stats["error"] += 1
                continue
            cost = self._estimate_cost(mapping, order.get("quantity", 1))
            costed.append((order, mapping, cost))

        # 금액 오름차순 정렬 → 예산 내 최대 건수 확보
        costed.sort(key=lambda x: x[2])

        # 그리디 선택
        running = 0
        to_place: list[tuple[dict, dict, int]] = []
        to_defer: list[tuple[dict, dict, int]] = []
        for order, mapping, cost in costed:
            if running + cost <= balance:
                to_place.append((order, mapping, cost))
                running += cost
            else:
                to_defer.append((order, mapping, cost))

        logger.info(
            "예산 확인 (잔액 %s원): 발주 가능 %d건(%s원) / 대기 %d건(%s원)",
            f"{balance:,}", len(to_place), f"{running:,}",
            len(to_defer), f"{sum(c for _,_,c in to_defer):,}",
        )

        # 발주
        for order, mapping, cost in to_place:
            if self._place_one(order, mapping=mapping):
                stats["ordered"] += 1
                if cost > 0:
                    self._budget.deduct(
                        cost, f"발주 완료: {order['order_id']}", order["order_id"]
                    )
            else:
                stats["error"] += 1

        # 대기 전환
        if to_defer:
            self._pending.add_many(
                [{"order": o, "estimated_cost": c} for o, _, c in to_defer]
            )
            for order, _, _ in to_defer:
                self._orders.update_status(order["order_id"], "PENDING")
            stats["deferred"] = len(to_defer)

            shortage = sum(c for _, _, c in to_defer)
            remaining = balance - running
            self._notify_budget_shortage(to_defer, remaining, shortage)

    def _estimate_cost(self, mapping: dict, quantity: int) -> int:
        """예상 발주 비용 = 도매처 단가 × 수량 + 배송비 기본값.
        가격 조회 실패 시 0 반환 (예산 부족으로 인한 오발주 방지용 — 차감도 0으로 처리).
        """
        try:
            client    = self.clients[mapping["supplier"]]
            product   = client.get_product(mapping["supplier_product_id"])
            unit_price = int(product.get("price") or 0)
            if unit_price > 0:
                return unit_price * quantity + DEFAULT_SHIPPING
        except Exception as e:
            logger.warning(
                "가격 조회 실패 — cost=0으로 처리 (발주는 정상 진행): %s/%s — %s",
                mapping["supplier"], mapping["supplier_product_id"], e,
            )
        return 0

    # ── 대기 주문 재개 ────────────────────────────────────────

    def resume_pending(self) -> dict:
        """예산 충전 후 대기 주문 재개 시도.
        반환값: {"ordered": n, "still_pending": n, "error": n}
        """
        if self._pending is None or self._budget is None:
            return {"ordered": 0, "still_pending": 0, "error": 0}

        items = self._pending.all()
        stats = {"ordered": 0, "still_pending": 0, "error": 0}

        if not items:
            logger.info("재개할 대기 주문 없음")
            return stats

        balance = self._budget.get_balance()
        logger.info("대기 주문 재개 시작: %d건, 현재 잔액 %s원", len(items), f"{balance:,}")

        # 비용 오름차순 정렬
        items.sort(key=lambda x: x.get("estimated_cost", 0))

        running    = 0
        placed_ids: set[str] = set()

        for item in items:
            order = item["order"]
            cost  = item.get("estimated_cost", 0)
            oid   = order["order_id"]

            if running + cost > balance:
                stats["still_pending"] += 1
                continue

            # PENDING → NEW 로 되돌려서 _place_one이 정상 처리하도록
            self._orders.update_status(oid, "NEW")

            if self._place_one(order):
                stats["ordered"] += 1
                running += cost
                if cost > 0:
                    self._budget.deduct(
                        cost, f"대기 주문 재개: {oid}", oid
                    )
            else:
                stats["error"] += 1

            placed_ids.add(oid)  # 성공/실패 모두 pending에서 제거

        if placed_ids:
            self._pending.remove_many(placed_ids)

        logger.info(
            "대기 주문 재개 완료: 발주 %d건 / 계속 대기 %d건 / 오류 %d건",
            stats["ordered"], stats["still_pending"], stats["error"],
        )
        return stats

    # ── 단건 발주 ────────────────────────────────────────────

    def _place_one(self, order: dict, mapping: dict | None = None) -> bool:
        """단건 발주 처리. 성공 True / 실패 False."""
        order_id = order["order_id"]

        # 1. 매핑 조회 (budget 경로에서는 이미 조회된 mapping 재사용)
        if mapping is None:
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
            if self._dry_run:
                kwargs["dry_run"] = True
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

    def _notify_budget_shortage(
        self,
        to_defer: list[tuple[dict, dict, int]],
        remaining_balance: int,
        shortage: int,
    ):
        if not self._notifier:
            return
        count   = len(to_defer)
        subject = f"[긴급] 예산 부족 - 대기 주문 {count}건"
        lines   = [
            f"■ 예산 부족으로 {count}건의 주문이 대기 상태로 전환됐습니다.",
            "",
            f"현재 잔액   : {remaining_balance:>12,}원",
            f"부족 금액   : {shortage:>12,}원",
            "",
            "■ 대기 주문 목록",
        ]
        for order, _, cost in to_defer:
            lines.append(
                f"  {order['order_id']}: {order.get('product_name', '(상품명 없음)')} "
                f"{order.get('quantity', 1)}개 — 예상 {cost:,}원"
            )
        lines += [
            "",
            f"아래 명령으로 예산을 충전하면 대기 주문이 자동 재개됩니다:",
            f"  python add_budget.py {shortage}",
        ]
        try:
            self._notifier.send(subject=subject, body="\n".join(lines))
        except Exception as e:
            logger.error("예산 부족 이메일 전송 실패: %s", e)
