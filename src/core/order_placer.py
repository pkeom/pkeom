"""매핑 테이블 기반 도매처 자동 발주 + SS 발주확인

흐름:
  orders.json [NEW]
    → 취소 요청 확인 (SS CANCEL_REQUEST)
        취소 요청 있음 → orders.json [CANCELLED] → 발주 건너뜀
    → 매핑 조회 → 재고 확인 → (예산 확인) → 도매처 발주 API 호출
        재고 없음  → stock_pending_orders.json [STOCK_PENDING] → SS 품절 처리 → 이메일 알림
        예산 충분  → 발주 → SupplierOrder DB 저장 → orders.json [ORDERED] → 예산 차감
        예산 부족  → pending_orders.json [PENDING] → orders.json [PENDING] → 이메일 알림
        매핑 없음  → orders.json [ERROR] → 이메일 알림
    → 발주 성공 건 일괄 SS 발주확인 (30건씩 배치)
        확인 성공  → data/confirm_log.json 저장
        확인 실패  → 이메일 알림
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone
import requests
from src.api.domaekkuk import DomaekkukAPI
from src.api.domaemae import DomaemaeClient
from src.core.exceptions import EmoneyShortageError
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
        f"구매자:   {order.get('buyer_name', '')}",
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
    _resume_lock = threading.Lock()  # resume_pending() 동시 실행 방지

    def __init__(
        self,
        domaekkuk: DomaekkukAPI,
        domaemae: DomaemaeClient,
        mapping_repo: MappingRepository | None = None,
        order_repo: OrderRepository | None = None,
        notifier=None,
        budget=None,           # BudgetManager | None
        pending=None,          # PendingOrderRepository | None
        ss_api=None,           # SmartstoreAPI | None — 품절 처리에 사용
        stock_pending=None,    # StockPendingRepository | None
        dry_run: bool = False,
    ):
        self.clients        = {"domaekkuk": domaekkuk, "domaemae": domaemae}
        self._mappings      = mapping_repo or MappingRepository()
        self._orders        = order_repo   or OrderRepository()
        self._notifier      = notifier
        self._budget        = budget
        self._pending       = pending
        self._ss_api        = ss_api
        self._stock_pending = stock_pending
        self._dry_run       = dry_run

    # ── 매핑 조회 헬퍼 ──────────────────────────────────────────

    def _find_mapping(self, product_id: str, option_code: str) -> dict | None:
        """매핑 조회: option_code로 먼저 찾고, 없으면 ss_option_id=''로 재시도.

        단일 상품(옵션 없음)의 경우 Naver API가 optionCode=productId 값을 내려보내
        orders.json에 option_code가 비어있지 않을 수 있음.
        매핑은 ss_option_id=''로 등록되어 있으므로 fallback이 필요.
        """
        mapping = self._mappings.find(ss_product_id=product_id, ss_option_id=option_code)
        if mapping is None and option_code:
            mapping = self._mappings.find(ss_product_id=product_id, ss_option_id="")
            if mapping:
                logger.info(
                    "매핑 fallback 성공: product_id=%r option_code=%r → ss_option_id='' 매핑 사용",
                    product_id, option_code,
                )
        return mapping

    # ── 진입점 ───────────────────────────────────────────────

    def run(self) -> dict:
        """NEW 상태 주문 전체 발주 처리 + SS 발주확인.

        반환값: {total, ordered, error, deferred, stock_pending,
                 cancelled, ss_confirmed, ss_confirm_failed}
        """
        # 진단 로그: 파일 경로 · 전체 건수 · 상태별 건수
        all_orders   = self._orders.all()
        status_count = {}
        for o in all_orders:
            s = o.get("status", "?")
            status_count[s] = status_count.get(s, 0) + 1
        logger.info("orders.json 경로: %s | 전체 %d건 | 상태별: %s",
                    getattr(self._orders, '_path', '?'), len(all_orders), status_count)

        new_orders = self._orders.find_by_status("NEW")
        stats = {
            "total": len(new_orders), "ordered": 0, "error": 0,
            "deferred": 0, "stock_pending": 0,
            "cancelled": 0,          # 취소 요청으로 발주 건너뜀
            "ss_confirmed": 0,       # SS 발주확인 성공
            "ss_confirm_failed": 0,  # SS 발주확인 실패
        }

        if not new_orders:
            logger.info("발주 대상 주문 없음 (NEW 상태 0건)")
            return stats

        logger.info("자동 발주 시작: %d건", stats["total"])

        # 1. 취소 요청 주문 사전 필터링
        new_orders = self._filter_cancel_requests(new_orders, stats)

        # 2. 도매처 발주 (성공 건 to_confirm에 수집)
        to_confirm: list[str] = []
        if self._budget is None:
            for i, order in enumerate(new_orders):
                try:
                    result = self._place_one(order)
                except EmoneyShortageError as e:
                    self._handle_emoney_shortage(new_orders[i:], stats, str(e))
                    break
                stats[result] += 1
                if result == "ordered":
                    to_confirm.append(order["order_id"])
        else:
            self._run_with_budget(new_orders, stats, to_confirm)

        logger.info(
            "자동 발주 완료: 전체 %d건 / 성공 %d건 / 취소건너뜀 %d건 / "
            "실패 %d건 / 대기 %d건 / 재고부족 %d건",
            stats["total"], stats["ordered"], stats["cancelled"],
            stats["error"], stats["deferred"], stats["stock_pending"],
        )

        # 3. SS 발주확인 (발주 성공 건만, 30건씩 배치)
        if to_confirm and self._ss_api:
            self._run_ss_confirm(to_confirm, stats)

        # 재고부족 대기 주문이 있으면 배치 이메일
        if stats["stock_pending"] > 0:
            stock_pending_orders = self._stock_pending.all() if self._stock_pending else []
            self._notify_stock_pending(stock_pending_orders)

        return stats

    # ── 예산 관리 발주 ───────────────────────────────────────

    def _run_with_budget(self, orders: list[dict], stats: dict, to_confirm: list[str]):
        """예산 범위 내 최대 발주 + 초과분 대기 전환"""
        balance = self._budget.get_balance()

        # 매핑 조회 + 비용 추정
        costed: list[tuple[dict, dict, int]] = []  # (order, mapping, cost)
        for order in orders:
            mapping = self._find_mapping(
                product_id=order["product_id"],
                option_code=order.get("option_code", ""),
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
        placed_in_loop: set[str] = set()
        for order, mapping, cost in to_place:
            try:
                result = self._place_one(order, mapping=mapping)
            except EmoneyShortageError as e:
                unplaced = [o for o, _, _ in to_place if o["order_id"] not in placed_in_loop]
                self._handle_emoney_shortage(unplaced, stats, str(e))
                break
            placed_in_loop.add(order["order_id"])
            if result == "ordered":
                stats["ordered"] += 1
                to_confirm.append(order["order_id"])
                if cost > 0:
                    self._budget.deduct(
                        cost, f"발주 완료: {order['order_id']}", order["order_id"]
                    )
            elif result == "stock_pending":
                stats["stock_pending"] += 1
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

            # 예산 부족 → 전체 상품 판매중지
            if self._ss_api:
                try:
                    from src.core.budget_sale_guard import suspend_all
                    suspend_all(self._ss_api, self._mappings, self._notifier)
                except Exception as e:
                    logger.error("예산 부족 판매중지 처리 실패: %s", e)

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
        반환값: {"ordered": n, "still_pending": n, "error": n, "cancelled": n}

        동시 실행 방지: _resume_lock으로 중복 호출 즉시 스킵.
        """
        if not OrderPlacer._resume_lock.acquire(blocking=False):
            logger.warning("resume_pending 이미 실행 중 — 스킵")
            return {"ordered": 0, "still_pending": 0, "error": 0, "cancelled": 0}
        try:
            return self._resume_pending_inner()
        finally:
            OrderPlacer._resume_lock.release()

    def _resume_pending_inner(self) -> dict:
        if self._pending is None or self._budget is None:
            return {"ordered": 0, "still_pending": 0, "error": 0, "cancelled": 0}

        items = self._pending.all()
        stats = {"ordered": 0, "still_pending": 0, "error": 0, "cancelled": 0}

        if not items:
            logger.info("재개할 대기 주문 없음")
            return stats

        balance = self._budget.get_balance()
        logger.info("대기 주문 재개 시작: %d건, 현재 잔액 %s원", len(items), f"{balance:,}")

        # 비용 오름차순 정렬
        items.sort(key=lambda x: x.get("estimated_cost", 0))

        # 취소 요청 사전 조회 (24시간 이내)
        cancel_ids: set[str] = set()
        if self._ss_api:
            try:
                cancel_data = self._ss_api.get_cancellations(hours=24)
                cancel_ids = {
                    item.get("productOrder", {}).get("productOrderId", "")
                    for item in cancel_data
                    if item.get("productOrder", {}).get("productOrderId")
                }
                if cancel_ids:
                    logger.info("대기 주문 재개: 취소 요청 %d건 감지", len(cancel_ids))
            except Exception as e:
                logger.warning("대기 주문 재개 시 취소 요청 확인 실패 — 취소 확인 없이 진행: %s", e)

        running    = 0
        placed_ids: set[str] = set()

        for item in items:
            order = item["order"]
            cost  = item.get("estimated_cost", 0)
            oid   = order["order_id"]

            # 취소 요청 감지 → orders.json CANCELLED, pending 제거, cancel_monitor 위임
            if oid in cancel_ids:
                self._orders.update_status(oid, "CANCELLED")
                placed_ids.add(oid)
                stats["cancelled"] += 1
                logger.info("대기 주문 취소 감지 → cancel_monitor 위임: order_id=%s", oid)
                continue

            if running + cost > balance:
                stats["still_pending"] += 1
                continue

            # PENDING → NEW 로 되돌려서 _place_one이 정상 처리하도록
            self._orders.update_status(oid, "NEW")

            try:
                result = self._place_one(order)
            except EmoneyShortageError as e:
                self._orders.update_status(oid, "PENDING")  # NEW로 바꿨던 것 원복
                remaining = [it["order"] for it in items
                             if it["order"]["order_id"] not in placed_ids]
                logger.error("e머니 잔액 부족으로 대기 주문 재개 중단: %s", e)
                self._notify_emoney_shortage(remaining, str(e))
                break

            if result == "ordered":
                stats["ordered"] += 1
                running += cost
                if cost > 0:
                    self._budget.deduct(cost, f"대기 주문 재개: {oid}", oid)
            else:
                stats["error"] += 1

            placed_ids.add(oid)  # 성공/실패/재고부족 모두 pending에서 제거

        if placed_ids:
            self._pending.remove_many(placed_ids)

        logger.info(
            "대기 주문 재개 완료: 발주 %d건 / 취소 %d건 / 계속 대기 %d건 / 오류 %d건",
            stats["ordered"], stats["cancelled"], stats["still_pending"], stats["error"],
        )
        return stats

    # ── 재고부족 대기 주문 재개 ───────────────────────────────

    def resume_stock_pending(
        self, supplier: str, supplier_product_id: str, ss_product_id: str
    ) -> int:
        """재입고 시 해당 상품의 재고부족 대기 주문을 재발주.
        반환값: 발주 성공 건수
        """
        if self._stock_pending is None:
            return 0

        entries = self._stock_pending.find_by_supplier(supplier, supplier_product_id)
        if not entries:
            return 0

        logger.info(
            "재고부족 대기 주문 재개: %s/%s — %d건",
            supplier, supplier_product_id, len(entries),
        )

        placed_ids: set[str] = set()
        placed_count = 0

        for entry in entries:
            order = entry["order"]
            oid   = order["order_id"]
            self._orders.update_status(oid, "NEW")
            result = self._place_one(order, skip_stock_check=True)
            if result == "ordered":
                placed_count += 1
            placed_ids.add(oid)

        if placed_ids:
            self._stock_pending.remove_many(placed_ids)

        logger.info(
            "재고부족 대기 재개 완료: %s/%s — 발주 %d건 / 전체 %d건",
            supplier, supplier_product_id, placed_count, len(entries),
        )
        return placed_count

    # ── 단건 발주 ────────────────────────────────────────────

    def _place_one(
        self, order: dict, mapping: dict | None = None, skip_stock_check: bool = False
    ) -> str:
        """단건 발주 처리.
        반환값: 'ordered' | 'error' | 'stock_pending'
        """
        order_id = order["order_id"]

        # 1. 매핑 조회 (budget 경로에서는 이미 조회된 mapping 재사용)
        if mapping is None:
            pid       = order.get("product_id", "")
            opt_code  = order.get("option_code", "")
            if not pid:
                logger.error(
                    "product_id 없음 — order_id=%s orders.json 저장 당시 "
                    "channelProductNo/productId 모두 빈 값. 주문 재수집 필요.",
                    order_id,
                )
                self._handle_error(
                    order=order, mapping=None,
                    reason="product_id 없음",
                    detail=(
                        f"order_id '{order_id}' 의 product_id 가 비어 있습니다.\n"
                        "주문 재수집 후 channelProductNo/productId 가 올바르게 저장되는지 확인하세요."
                    ),
                )
                return "error"
            all_maps  = self._mappings.all()
            map_ids   = [m["ss_product_id"] for m in all_maps]
            logger.info(
                "매핑 조회: order_id=%s product_id=%r option_code=%r | "
                "mappings.json 등록 상품(%d개): %s",
                order_id, pid, opt_code, len(map_ids), map_ids,
            )
            mapping = self._find_mapping(
                product_id=pid,
                option_code=opt_code,
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
            return "error"

        # 2. 재고 확인 (stock_pending 활성화 시, skip_stock_check=False 일 때만)
        if self._stock_pending is not None and not skip_stock_check:
            supplier           = mapping["supplier"]
            supplier_pid       = mapping["supplier_product_id"]
            supplier_option_id = mapping.get("supplier_option_id", "")
            try:
                client = self.clients[supplier]
                if supplier == "domaemae" and supplier_option_id:
                    # 옵션 있는 도매매 상품: 해당 옵션의 재고(selectOpt.sup)만 조회
                    stock = client.get_option_stock(supplier_pid, supplier_option_id)
                    logger.debug(
                        "도매매 옵션 재고 조회: %s opt=%s → stock=%s",
                        supplier_pid, supplier_option_id, stock,
                    )
                else:
                    product = client.get_product(supplier_pid)
                    stock   = product.get("stock")
                if stock is None:
                    logger.warning(
                        "재고 조회 None — 발주 계속 진행: %s/%s", supplier, supplier_pid,
                    )
                else:
                    stock = int(stock)
                    if stock == 0:
                        return self._handle_stock_pending(order, mapping)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    logger.error(
                        "재고 조회 404 (상품 없음) → ERROR: %s/%s", supplier, supplier_pid,
                    )
                    self._handle_error(
                        order=order, mapping=mapping,
                        reason="재고 조회 404 (도매처 상품 없음)", detail=str(e),
                    )
                    return "error"
                logger.warning(
                    "재고 조회 실패 — 발주 계속 진행: %s/%s — %s", supplier, supplier_pid, e,
                )
            except Exception as e:
                logger.warning(
                    "재고 조회 실패 — 발주 계속 진행: %s/%s — %s", supplier, supplier_pid, e,
                )

        # 3. 도매처 발주 API 호출
        shipping = {
            "name":           order.get("receiver_name", ""),
            "phone":          order.get("receiver_phone", ""),
            "address":        order.get("receiver_address", ""),
            "base_address":            order.get("base_address", ""),
            "receiver_address_detail": order.get("detail_address", ""),
            "zipcode":        order.get("receiver_zipcode", ""),
            "memo":           order.get("delivery_memo", ""),
        }
        _missing = [k for k, v in [
            ("receiver_name",    shipping["name"]),
            ("receiver_phone",   shipping["phone"]),
            ("receiver_address", shipping["address"]),
            ("receiver_zipcode", shipping["zipcode"]),
        ] if not v]
        if _missing:
            logger.warning(
                "발주 배송지 정보 누락 (order_id=%s): %s — "
                "SmartStore 주문 수집 시 delivery.receiverName/receiverTel1/baseAddress/zipCode 값 확인 필요",
                order_id, ", ".join(_missing),
            )
        supplier_option_id = mapping.get("supplier_option_id", "")
        try:
            client = self.clients[mapping["supplier"]]
            kwargs = {}
            if mapping["supplier"] == "domaekkuk":
                # supplier_option_id가 있으면 addOrder 'opt' 필드로 전달
                if supplier_option_id:
                    kwargs["supplier_option_id"] = supplier_option_id
                    logger.info(
                        "도매꾹 발주 옵션 전달: order_id=%s, opt=%s",
                        order_id, supplier_option_id,
                    )
            elif mapping["supplier"] == "domaemae":
                if supplier_option_id:
                    # 매핑의 도매처 옵션 코드를 직접 전달 (SS 숫자코드 대신)
                    kwargs["option_id"] = supplier_option_id
                    logger.info(
                        "도매매 발주 옵션 직접 전달: order_id=%s, option_id=%s",
                        order_id, supplier_option_id,
                    )
                else:
                    # supplier_option_id 없음 → SS option_code로 이름 매칭 폴백
                    ss_option = order.get("option_code", "")
                    kwargs["option_name"] = ss_option
                    if ss_option:
                        logger.warning(
                            "도매매 발주 옵션: supplier_option_id 없음 "
                            "— SS option_code '%s' 로 이름 매칭 시도 (order_id=%s). "
                            "매핑에 supplier_option_id를 등록하면 정확도가 높아집니다.",
                            ss_option, order_id,
                        )
            if self._dry_run:
                kwargs["dry_run"] = True
            result = client.place_order(
                mapping["supplier_product_id"], order["quantity"], shipping, **kwargs
            )
            supplier_order_no = _extract_supplier_order_no(result)
        except EmoneyShortageError:
            raise  # 호출자(run / resume_pending)가 일괄 대기 처리
        except Exception as e:
            import traceback
            self._handle_error(
                order=order,
                mapping=mapping,
                reason="발주 API 오류",
                detail=f"{e}\n{traceback.format_exc()}",
            )
            return "error"

        # 4. 발주 성공 저장
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
                session.commit()
        except Exception as e:
            logger.error("SupplierOrder DB 저장 실패: order_id=%s, %s", order_id, e)

        self._orders.update_supplier_info(order_id, mapping["supplier"], supplier_order_no)
        self._orders.update_status(order_id, "ORDERED")
        logger.info(
            "발주 완료: order_id=%s → %s 발주번호=%s",
            order_id, mapping["supplier"], supplier_order_no or "(미확인)",
        )
        return "ordered"

    # ── 재고부족 처리 ─────────────────────────────────────────

    def _handle_stock_pending(self, order: dict, mapping: dict) -> str:
        """재고 없음 → 재고부족 대기 저장 + 스마트스토어 품절 처리"""
        order_id      = order["order_id"]
        supplier      = mapping["supplier"]
        supplier_pid  = mapping["supplier_product_id"]
        ss_product_id = order.get("product_id", "")

        logger.warning(
            "재고 없음 → 재고부족 대기: order_id=%s, %s/%s",
            order_id, supplier, supplier_pid,
        )

        self._stock_pending.add({
            "order_id":            order_id,
            "supplier":            supplier,
            "supplier_product_id": supplier_pid,
            "ss_product_id":       ss_product_id,
            "order":               order,
        })

        # 스마트스토어 품절 처리
        api_product_id = mapping.get("origin_product_no") or ""
        if self._ss_api and api_product_id:
            try:
                self._ss_api.set_product_sale_status(api_product_id, on_sale=False)
                logger.info("스마트스토어 품절 처리: ss_product=%s", ss_product_id)
            except Exception as e:
                logger.error("스마트스토어 품절 처리 실패: ss_product=%s — %s", ss_product_id, e)

        self._orders.update_status(order_id, "STOCK_PENDING")
        return "stock_pending"

    def _notify_stock_pending(self, pending_orders: list[dict]):
        """재고부족 대기 배치 이메일"""
        if not self._notifier or not pending_orders:
            return
        count   = len(pending_orders)
        subject = f"[재고부족 대기] {count}건 주문 재고부족으로 대기 중"
        lines   = [
            f"■ {count}건의 주문이 재고부족으로 대기 상태로 전환됐습니다.",
            "",
            "■ 대기 주문 목록",
        ]
        for entry in pending_orders:
            order = entry.get("order", {})
            lines.append(
                f"  주문번호: {entry.get('order_id', '')} | "
                f"상품명: {order.get('product_name', '(상품명 없음)')} | "
                f"도매처: {entry.get('supplier', '')} / {entry.get('supplier_product_id', '')}"
            )
        lines += [
            "",
            "재입고 시 자동으로 발주가 재개됩니다.",
        ]
        try:
            self._notifier.send(subject=subject, body="\n".join(lines))
        except Exception as e:
            logger.error("재고부족 대기 이메일 전송 실패: %s", e)

    # ── 취소 요청 필터 ───────────────────────────────────────

    def _filter_cancel_requests(
        self, orders: list[dict], stats: dict
    ) -> list[dict]:
        """SS CANCEL_REQUEST 상태 주문을 발주 전에 걸러낸다.

        SS API에서 최근 24시간 취소 요청 목록을 한 번 조회해
        취소 요청된 order_id는 CANCELLED로 마킹하고 발주 대상에서 제외한다.
        """
        if not self._ss_api:
            return orders
        try:
            cancel_data = self._ss_api.get_cancellations(hours=24)
            cancel_ids: set[str] = {
                item.get("productOrder", {}).get("productOrderId", "")
                for item in cancel_data
                if item.get("productOrder", {}).get("productOrderId")
            }
        except Exception as e:
            logger.warning("취소 요청 조회 실패 — 발주 계속 진행: %s", e)
            return orders

        if not cancel_ids:
            return orders

        filtered: list[dict] = []
        for order in orders:
            if order["order_id"] in cancel_ids:
                self._orders.update_status(order["order_id"], "CANCELLED")
                stats["cancelled"] += 1
                logger.info(
                    "취소 요청 감지 → 발주 건너뜀: order_id=%s / %s",
                    order["order_id"], order.get("product_name", ""),
                )
            else:
                filtered.append(order)

        if stats["cancelled"]:
            logger.info(
                "취소 요청 %d건 제외 → 실발주 대상 %d건",
                stats["cancelled"], len(filtered),
            )
        return filtered

    # ── SS 발주확인 ──────────────────────────────────────────

    def _run_ss_confirm(self, order_ids: list[str], stats: dict):
        """발주 성공 건에 대해 SS 발주확인 API를 30건씩 배치 호출한다."""
        logger.info("SS 발주확인 시작: %d건", len(order_ids))
        try:
            result = self._ss_api.confirm_orders(order_ids)
            confirmed = result.get("confirmed", [])
            failed    = result.get("failed", [])
            stats["ss_confirmed"]      = len(confirmed)
            stats["ss_confirm_failed"] = len(failed)
            if confirmed:
                self._save_confirm_log(confirmed)
                logger.info("SS 발주확인 완료: %d건", len(confirmed))
            if failed:
                logger.error("SS 발주확인 실패: %d건 — %s", len(failed), failed[:5])
                self._notify_confirm_failed(failed)
        except Exception as e:
            logger.error("SS 발주확인 전체 실패: %s", e)
            stats["ss_confirm_failed"] = len(order_ids)
            self._notify_confirm_failed(order_ids)

    def _save_confirm_log(self, confirmed_ids: list[str]):
        """발주확인 이력을 data/confirm_log.json에 누적 저장."""
        log_file = "data/confirm_log.json"
        try:
            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = {"confirms": []}

            now = datetime.now(timezone.utc).isoformat()
            for oid in confirmed_ids:
                data["confirms"].append({"order_id": oid, "confirmed_at": now})

            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("발주확인 로그 저장 실패: %s", e)

    def _notify_confirm_failed(self, failed_ids: list[str]):
        if not self._notifier or not failed_ids:
            return
        subject = f"[위탁판매] SS 발주확인 실패 — {len(failed_ids)}건"
        body = "\n".join([
            f"■ 발주확인 API 호출에 실패한 주문이 {len(failed_ids)}건 있습니다.",
            "  스마트스토어 센터에서 수동으로 발주확인 처리해 주세요.",
            "",
            "■ 실패 주문번호 (최대 10건)",
            *[f"  {oid}" for oid in failed_ids[:10]],
        ])
        try:
            self._notifier.send(subject=subject, body=body)
        except Exception as e:
            logger.error("발주확인 실패 이메일 전송 오류: %s", e)

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
            f"■ 알림 종류 : 발주 실패",
            f"■ 실패 사유 : {reason}",
            f"■ 현재 상태 : ERROR (발주 미완료)",
            "",
            "■ 주문 정보",
            _order_summary(order, mapping),
            "",
            "■ 오류 상세",
            detail,
            "",
            "■ 필요한 조치",
            "  도매꾹/도매매 사이트에서 직접 발주 후 스마트스토어 센터에서",
            "  해당 주문의 발주확인을 수동으로 처리해 주세요.",
        ])
        try:
            self._notifier.send(subject=subject, body=body)
        except Exception as e:
            logger.error("발주실패 이메일 전송 오류: %s", e)

    def _handle_emoney_shortage(self, orders: list[dict], stats: dict, error_msg: str):
        """e머니 부족으로 발주 못 한 주문들을 PENDING으로 전환 후 알림."""
        if not orders:
            return
        for order in orders:
            self._orders.update_status(order["order_id"], "PENDING")
        if self._pending:
            self._pending.add_many([{"order": o, "estimated_cost": 0} for o in orders])
        stats["deferred"] = stats.get("deferred", 0) + len(orders)
        logger.error("e머니 잔액 부족 — %d건 발주 대기 전환: %s", len(orders), error_msg)
        self._notify_emoney_shortage(orders, error_msg)

    def _notify_emoney_shortage(self, orders: list[dict], error_msg: str):
        """e머니 부족 이메일+로그 알림."""
        count = len(orders)
        logger.error("[긴급] e머니 잔액 부족 — %d건 발주 중단", count)
        if not self._notifier:
            return
        subject = f"[긴급] 도매처 e머니 잔액 부족 — {count}건 발주 대기"
        lines = [
            f"■ 도매처 e머니 잔액 부족으로 {count}건의 발주가 중단되었습니다.",
            f"■ 오류: {error_msg}",
            "",
            "■ 조치 방법",
            "  도매꾹/도매매 사이트에서 e머니(도머니)를 충전하세요.",
            "  충전 후 다음 발주 주기에 자동으로 재시도됩니다.",
            "",
            "■ 대기 전환된 주문 (최대 10건)",
            *[f"  - {o.get('order_id','')} / {o.get('product_name','')}"
              for o in orders[:10]],
            *(["  … (이하 생략)"] if len(orders) > 10 else []),
        ]
        try:
            self._notifier.send(subject=subject, body="\n".join(lines))
        except Exception as e:
            logger.error("e머니 부족 알림 전송 실패: %s", e)

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
