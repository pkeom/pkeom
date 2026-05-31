"""도매처 가격 변동 감지 → price_alerts.json 저장

흐름:
  활성 매핑 전체 → 도매처 상품 가격 조회
    변동 없음  → 스킵
    가격 변동  → PriceHistory DB 기록 + price_alerts.json 저장
    오류 발생  → 이메일 알림 (가격 변동 시에는 이메일 미발송)
"""
import logging
from src.api.domaekkuk import DomaekkukAPI
from src.api.domaemae import DomaemaeClient
from src.core.mapping_repository import MappingRepository
from src.core.price_alert_repository import PriceAlertRepository
from src.db.database import get_session
from src.db.models import PriceHistory

logger = logging.getLogger(__name__)


class PriceMonitor:
    def __init__(
        self,
        domaekkuk: DomaekkukAPI,
        domaemae: DomaemaeClient,
        notifier=None,
        mapping_repo: MappingRepository | None = None,
        alert_repo: PriceAlertRepository | None = None,
    ):
        self.clients   = {"domaekkuk": domaekkuk, "domaemae": domaemae}
        self._notifier = notifier
        self._mappings = mapping_repo or MappingRepository()
        self._alerts   = alert_repo   or PriceAlertRepository()

    # ── 진입점 ───────────────────────────────────────────────

    def run(self) -> dict:
        """활성 매핑 전체 가격 모니터링.
        반환값: {"total": n, "changed": n, "unchanged": n, "error": n}
        """
        active = [m for m in self._mappings.all() if m.get("is_active", True)]
        stats  = {"total": len(active), "changed": 0, "unchanged": 0, "error": 0}

        if not active:
            logger.info("가격 모니터링 대상 없음")
            return stats

        logger.info("가격 모니터링 시작: %d건", stats["total"])
        with get_session() as session:
            for mapping in active:
                result = self._check_one(session, mapping)
                stats[result] += 1
            session.commit()

        if stats["changed"]:
            logger.info("가격 변동 감지: %d건 — price_alerts.json 저장 완료", stats["changed"])

        logger.info(
            "가격 모니터링 완료: 전체 %d건 / 변동 %d건 / 동일 %d건 / 오류 %d건",
            stats["total"], stats["changed"], stats["unchanged"], stats["error"],
        )
        return stats

    # ── 단건 처리 ────────────────────────────────────────────

    def _check_one(self, session, mapping: dict) -> str:
        """단건 가격 확인. 반환값: 'changed' | 'unchanged' | 'error'"""
        supplier      = mapping["supplier"]
        supplier_pid  = mapping["supplier_product_id"]
        ss_product_id = mapping["ss_product_id"]

        # 1. 도매처 가격 조회
        try:
            product      = self.clients[supplier].get_product(supplier_pid)
            new_price    = product.get("price")
            product_name = product.get("title", "")
        except Exception as e:
            logger.error("가격 조회 오류: %s/%s — %s", supplier, supplier_pid, e)
            self._notify_error(ss_product_id, supplier, supplier_pid, str(e))
            return "error"

        if new_price is None:
            logger.warning("가격 정보 없음: %s/%s", supplier, supplier_pid)
            return "unchanged"

        # 2. 직전 가격과 비교
        last = (
            session.query(PriceHistory)
            .filter_by(supplier=supplier, supplier_product_id=supplier_pid)
            .order_by(PriceHistory.detected_at.desc())
            .first()
        )
        old_price = last.new_price if last else None

        if old_price == new_price:
            return "unchanged"

        # 3. 변동 감지 — DB 기록 + 알림 저장
        session.add(PriceHistory(
            supplier=supplier,
            supplier_product_id=supplier_pid,
            old_price=old_price,
            new_price=new_price,
        ))

        alert = self._alerts.add(
            ss_product_id=ss_product_id,
            supplier=supplier,
            supplier_product_id=supplier_pid,
            product_name=product_name,
            old_price=old_price,
            new_price=new_price,
        )

        direction = "▲ 인상" if new_price > (old_price or 0) else "▼ 인하"
        logger.info(
            "가격 변동 [%s]: %s/%s  %s원 → %s원 (%.1f%%) — ss_product=%s",
            direction, supplier, supplier_pid,
            f"{old_price or 0:,}", f"{new_price:,}",
            alert["change_rate"] * 100,
            ss_product_id,
        )
        return "changed"

    # ── 오류 알림 ────────────────────────────────────────────

    def _notify_error(self, ss_product_id: str, supplier: str,
                      supplier_pid: str, detail: str):
        if not self._notifier:
            return
        subject = "[위탁판매] 가격 모니터링 오류"
        body = "\n".join([
            "■ 가격 조회 중 오류가 발생했습니다.",
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
            logger.error("가격오류 이메일 전송 실패: %s", e)
