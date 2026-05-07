"""도매처 재고 확인 → 품절/재입고 시 스마트스토어 판매상태 자동 전환

흐름:
  활성 매핑 전체 → 도매처 재고 조회
    이전과 동일  → 스킵 (API 호출 없음)
    품절 발생    → 스마트스토어 판매중지
    재입고 발생  → 스마트스토어 판매재개
    오류 발생    → 이메일 알림 (상태 변경 없음)

상태 캐시: data/stock_cache.json
  {"status": {"domaekkuk:12345": true, "domaemae:67890": false}}
  최초 실행 시 캐시 없음 → 모든 상품 API 동기화 후 캐시 생성
"""
import json
import logging
import threading
from pathlib import Path
from src.api.smartstore import SmartstoreAPI
from src.api.domaekkuk import DomaekkukAPI
from src.api.domaemae import DomaemaeClient
from src.core.mapping_repository import MappingRepository

logger = logging.getLogger(__name__)

_cache_lock = threading.Lock()

_DEFAULT_CACHE_PATH = Path("data/stock_cache.json")


class InventorySync:
    def __init__(
        self,
        ss_api: SmartstoreAPI,
        domaekkuk: DomaekkukAPI,
        domaemae: DomaemaeClient,
        mapping_repo: MappingRepository | None = None,
        notifier=None,
        cache_path: Path | str | None = None,
    ):
        self.ss_api     = ss_api
        self.clients    = {"domaekkuk": domaekkuk, "domaemae": domaemae}
        self._mappings  = mapping_repo or MappingRepository()
        self._notifier  = notifier
        self._cache_path = Path(cache_path) if cache_path else _DEFAULT_CACHE_PATH

    def _load_cache(self) -> dict:
        if not self._cache_path.exists():
            return {}
        with open(self._cache_path, encoding="utf-8") as f:
            return json.load(f).get("status", {})

    def _save_cache(self, status: dict):
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._cache_path, "w", encoding="utf-8") as f:
            json.dump({"status": status}, f, ensure_ascii=False, indent=2)

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
            cache = self._load_cache()
            for mapping in active:
                result = self._sync_one(mapping, cache)
                stats[result] += 1
            self._save_cache(cache)

        logger.info(
            "재고 동기화 완료: 전체 %d건 / 판매중지 %d건 / 재개 %d건 / 변동없음 %d건 / 오류 %d건",
            stats["total"], stats["paused"], stats["resumed"], stats["unchanged"], stats["error"],
        )
        return stats

    # ── 단건 처리 ────────────────────────────────────────────

    def _sync_one(self, mapping: dict, cache: dict) -> str:
        """단건 재고 동기화. 반환값: 'paused' | 'resumed' | 'unchanged' | 'error'"""
        supplier      = mapping["supplier"]
        supplier_pid  = mapping["supplier_product_id"]
        ss_product_id = mapping["ss_product_id"]
        cache_key     = f"{supplier}:{supplier_pid}"

        # 1. 도매처 재고 조회
        try:
            stock    = self.clients[supplier].get_stock(supplier_pid)
            in_stock = stock > 0
        except Exception as e:
            logger.error("재고 조회 오류: %s/%s — %s", supplier, supplier_pid, e)
            self._notify_error(ss_product_id, supplier, supplier_pid, "재고 조회 오류", str(e))
            return "error"

        was_in_stock = cache.get(cache_key)  # None = 최초 실행

        # 2. 상태 변동 없으면 스킵 (최초 실행은 반드시 동기화)
        if was_in_stock is not None and was_in_stock == in_stock:
            return "unchanged"

        # 3. 스마트스토어 판매 상태 변경
        try:
            self.ss_api.set_product_sale_status(ss_product_id, on_sale=in_stock)
        except Exception as e:
            logger.error("판매상태 변경 오류: ss_product=%s — %s", ss_product_id, e)
            self._notify_error(
                ss_product_id, supplier, supplier_pid,
                "스마트스토어 판매상태 변경 오류", str(e),
            )
            return "error"

        # 4. 캐시 갱신 및 결과 반환
        cache[cache_key] = in_stock

        if not in_stock:
            logger.warning("품절 → 판매중지: ss_product=%s (%s:%s)",
                           ss_product_id, supplier, supplier_pid)
            return "paused"

        if was_in_stock is False:  # False → True: 재입고
            logger.info("재입고 → 판매재개: ss_product=%s (%s:%s) stock=%d",
                        ss_product_id, supplier, supplier_pid, stock)
            return "resumed"

        # 최초 실행 & 재고 있음 — 판매중 상태 확인만
        logger.debug("초기 동기화 (판매중): ss_product=%s, stock=%d", ss_product_id, stock)
        return "unchanged"

    # ── 오류 알림 ────────────────────────────────────────────

    def _notify_error(self, ss_product_id: str, supplier: str,
                      supplier_pid: str, reason: str, detail: str):
        if not self._notifier:
            return
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
            self._notifier.send(subject=subject, body=body)
        except Exception as e:
            logger.error("재고오류 이메일 전송 실패: %s", e)
