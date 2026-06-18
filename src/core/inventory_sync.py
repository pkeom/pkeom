"""도매처 재고 확인 → 품절/재입고 시 스마트스토어 판매상태 자동 전환

흐름:
  활성 매핑 전체 → 도매처 재고 조회
    이전과 동일  → 스킵 (API 호출 없음)
    품절 발생    → 스마트스토어 판매중지 + 이메일 알림
    재입고 발생  → 스마트스토어 판매재개
    재고 부족    → 이메일 알림 (60분 내 재발송 방지)
    오류 발생    → 이메일 알림 (상태 변경 없음)

상태 캐시: data/stock_cache.json
  {
    "status": {"domaekkuk:12345": true, "domaemae:67890": false},
    "low_stock_alerted_at": {"domaekkuk:12345": "2026-05-14T10:00:00+00:00"}
  }
  최초 실행 시 캐시 없음 → 모든 상품 API 동기화 후 캐시 생성
"""
import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
from src.api.smartstore import SmartstoreAPI
from src.api.domaekkuk import DomaekkukAPI
from src.api.domaemae import DomaemaeClient
from src.core.mapping_repository import MappingRepository

logger = logging.getLogger(__name__)

_cache_lock = threading.Lock()

_DEFAULT_CACHE_PATH = Path("data/stock_cache.json")

LOW_STOCK_THRESHOLD = 50
LOW_STOCK_COOLDOWN_MINUTES = 60
ERROR_ALERT_COOLDOWN_HOURS = 24   # 같은 상품·같은 오류는 24시간에 1통 (EmailNotifier dedup)


class InventorySync:
    def __init__(
        self,
        ss_api: SmartstoreAPI,
        domaekkuk: DomaekkukAPI,
        domaemae: DomaemaeClient,
        mapping_repo: MappingRepository | None = None,
        notifier=None,
        cache_path: Path | str | None = None,
        order_placer=None,   # OrderPlacer | None — 재고부족 대기 주문 재개에 사용
    ):
        self.ss_api        = ss_api
        self.clients       = {"domaekkuk": domaekkuk, "domaemae": domaemae}
        self._mappings     = mapping_repo or MappingRepository()
        self._notifier     = notifier
        self._cache_path   = Path(cache_path) if cache_path else _DEFAULT_CACHE_PATH
        self._order_placer     = order_placer

    def _load_cache(self) -> tuple[dict, dict]:
        if not self._cache_path.exists():
            return {}, {}
        with open(self._cache_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("status", {}), data.get("low_stock_alerted_at", {})

    def _save_cache(self, status: dict, low_stock_alerted_at: dict):
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._cache_path, "w", encoding="utf-8") as f:
            json.dump(
                {"status": status, "low_stock_alerted_at": low_stock_alerted_at},
                f, ensure_ascii=False, indent=2,
            )

    # ── 진입점 ───────────────────────────────────────────────

    def run(self) -> dict:
        """활성 매핑 전체 재고 동기화.
        반환값: {"total": n, "paused": n, "resumed": n, "unchanged": n, "error": n}
        """
        active = [m for m in self._mappings.all() if m.get("is_active", True)]
        stats  = {"total": len(active), "paused": 0, "resumed": 0, "unchanged": 0, "error": 0}

        if not active:
            logger.info("재고 동기화 대상 없음")
            return stats

        logger.info("재고 동기화 시작: %d건", stats["total"])
        with _cache_lock:
            cache, low_stock_cache = self._load_cache()
            for mapping in active:
                result = self._sync_one(mapping, cache, low_stock_cache)
                stats[result] += 1
            self._save_cache(cache, low_stock_cache)

        logger.info(
            "재고 동기화 완료: 전체 %d건 / 판매중지 %d건 / 재개 %d건 / 변동없음 %d건 / 오류 %d건",
            stats["total"], stats["paused"], stats["resumed"], stats["unchanged"], stats["error"],
        )
        return stats

    # ── 단건 처리 ────────────────────────────────────────────

    def _sync_one(self, mapping: dict, cache: dict, low_stock_cache: dict) -> str:
        """단건 재고 동기화. 반환값: 'paused' | 'resumed' | 'unchanged' | 'error'"""
        supplier      = mapping["supplier"]
        supplier_pid  = mapping["supplier_product_id"]
        ss_product_id    = mapping["ss_product_id"]
        origin_product_no = mapping.get("origin_product_no") or ""
        supplier_url  = mapping.get("supplier_url", "")
        cache_key     = f"{supplier}:{supplier_pid}"

        # 1. 도매처 재고·상품명 조회 (get_product 한 번으로 모두 획득)
        try:
            product      = self.clients[supplier].get_product(supplier_pid)
            stock        = product.get("stock")
            if stock is None:
                logger.warning("재고 조회 None — 건너뜀: %s/%s", supplier, supplier_pid)
                return "unchanged"
            stock        = int(stock)
            product_name = product.get("title", "") or mapping.get("memo", "") or ss_product_id
            in_stock     = stock > 0
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.warning("재고 조회 404 — 건너뜀: %s/%s", supplier, supplier_pid)
                return "unchanged"
            logger.error("재고 조회 오류: %s/%s — %s", supplier, supplier_pid, e)
            self._notify_error(ss_product_id, supplier, supplier_pid, "재고 조회 오류", str(e))
            return "error"
        except Exception as e:
            logger.error("재고 조회 오류: %s/%s — %s", supplier, supplier_pid, e)
            self._notify_error(ss_product_id, supplier, supplier_pid, "재고 조회 오류", str(e))
            return "error"

        was_in_stock = cache.get(cache_key)  # None = 최초 실행

        # 2. 재고 부족 알림 (품절이 아닌데 임계치 미만으로 떨어진 경우)
        if in_stock and stock < LOW_STOCK_THRESHOLD:
            self._notify_low_stock_if_needed(
                cache_key, low_stock_cache,
                product_name, stock, supplier, supplier_pid, supplier_url,
            )

        # 3. 상태 변동 없으면 스킵 (최초 실행은 반드시 동기화)
        if was_in_stock is not None and was_in_stock == in_stock:
            return "unchanged"

        # 4. 스마트스토어 판매 상태 변경
        if not origin_product_no:
            logger.warning("origin_product_no 없음 — 판매상태 변경 건너뜀: ss_product=%s", ss_product_id)
            cache[cache_key] = in_stock
            return "unchanged"
        try:
            self.ss_api.set_product_sale_status(origin_product_no, on_sale=in_stock)
        except Exception as e:
            logger.error("판매상태 변경 오류: ss_product=%s — %s", ss_product_id, e)
            self._notify_error(
                ss_product_id, supplier, supplier_pid,
                "스마트스토어 판매상태 변경 오류", str(e),
            )
            return "error"

        # 5. 캐시 갱신 및 결과 반환
        cache[cache_key] = in_stock

        if not in_stock:
            logger.warning("품절 → 판매중지: ss_product=%s (%s:%s)",
                           ss_product_id, supplier, supplier_pid)
            self._notify_out_of_stock(product_name, ss_product_id, supplier, supplier_pid)
            return "paused"

        if was_in_stock is False:  # False → True: 재입고
            logger.info("재입고 → 판매재개: ss_product=%s (%s:%s) stock=%d",
                        ss_product_id, supplier, supplier_pid, stock)
            if self._order_placer is not None:
                placed = self._order_placer.resume_stock_pending(
                    supplier, supplier_pid, ss_product_id
                )
                if placed:
                    logger.info(
                        "재고부족 대기 주문 %d건 자동 재개: %s/%s",
                        placed, supplier, supplier_pid,
                    )
            return "resumed"

        # 최초 실행 & 재고 있음 — 판매중 상태 확인만
        logger.debug("초기 동기화 (판매중): ss_product=%s, stock=%d", ss_product_id, stock)
        return "unchanged"

    # ── 품절 알림 ─────────────────────────────────────────────

    def _notify_out_of_stock(self, product_name: str, ss_product_id: str,
                              supplier: str, supplier_pid: str):
        if not self._notifier:
            return
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = f"[품절] {product_name} - 판매 중지됨"
        body = "\n".join([
            f"■ 상품명: {product_name}",
            "",
            f"스마트스토어 상품ID: {ss_product_id}",
            f"도매처: {supplier} / 상품번호: {supplier_pid}",
            "",
            f"■ 품절 시각: {now_str}",
        ])
        try:
            self._notifier.send(subject=subject, body=body)
        except Exception as e:
            logger.error("품절 이메일 전송 실패: %s", e)

    # ── 재고 부족 알림 ────────────────────────────────────────

    def _notify_low_stock_if_needed(
        self, cache_key: str, low_stock_cache: dict,
        product_name: str, stock: int,
        supplier: str, supplier_pid: str, supplier_url: str,
    ):
        if not self._notifier:
            return
        now = datetime.now(timezone.utc)
        last_alerted_str = low_stock_cache.get(cache_key)
        if last_alerted_str:
            last_alerted = datetime.fromisoformat(last_alerted_str)
            if now - last_alerted < timedelta(minutes=LOW_STOCK_COOLDOWN_MINUTES):
                return
        subject = f"[재고부족] {product_name} - 재고 {stock}개 남음"
        body = "\n".join([
            f"■ 상품명: {product_name}",
            f"■ 현재 재고 수량: {stock}개",
            "",
            f"도매처: {supplier} / 상품번호: {supplier_pid}",
            f"상품 링크: {supplier_url}",
        ])
        try:
            self._notifier.send(subject=subject, body=body)
            low_stock_cache[cache_key] = now.isoformat()
        except Exception as e:
            logger.error("재고부족 이메일 전송 실패: %s", e)

    # ── 오류 알림 ────────────────────────────────────────────

    def _notify_error(self, ss_product_id: str, supplier: str,
                      supplier_pid: str, reason: str, detail: str):
        if not self._notifier:
            return
        # dedup: 같은 (잡종류+상품ID+오류유형)은 24시간에 1통 (EmailNotifier가 영속 관리)
        dedup_key = f"inventory_sync:{ss_product_id}:{reason}"
        subject = f"[위탁판매] 재고 동기화 오류 — {reason}"
        body = "\n".join([
            f"■ 오류 사유: {reason}",
            "",
            f"스마트스토어 상품ID: {ss_product_id}",
            f"도매처: {supplier} / 상품번호: {supplier_pid}",
            "",
            "■ 오류 상세",
            detail,
        ])
        try:
            self._notifier.send(
                subject=subject, body=body,
                dedup_key=dedup_key, cooldown_hours=ERROR_ALERT_COOLDOWN_HOURS,
            )
        except Exception as e:
            logger.error("재고오류 이메일 전송 실패: %s", e)
