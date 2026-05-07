"""단일 작업 실행 CLI (GitHub Actions 호출용)

사용법:
    python scripts/run_task.py <collect|place|invoice|inventory|price>
"""
import logging
import os
import sys

# 프로젝트 루트를 sys.path에 추가 (scripts/ 서브디렉토리에서 실행 시)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.config_loader import load_config
from src.utils.logger import setup_logging
from src.db.database import init_db
from src.api.smartstore import SmartstoreAPI
from src.api.domaekkuk import DomaekkukAPI
from src.api.domaemae import DomaemaeClient
from src.notifications.email_notifier import EmailNotifier
from src.core.order_collector import OrderCollector
from src.core.order_placer import OrderPlacer
from src.core.invoice_manager import InvoiceManager
from src.core.inventory_sync import InventorySync
from src.core.price_monitor import PriceMonitor

TASKS = {
    "collect":   "주문 수집",
    "place":     "자동 발주",
    "invoice":   "송장 동기화",
    "inventory": "재고 동기화",
    "price":     "가격 모니터링",
}


def _build_clients(cfg: dict):
    ss_api = SmartstoreAPI(
        client_id=cfg["smartstore"]["client_id"],
        client_secret=cfg["smartstore"]["client_secret"],
    )
    dk_api = DomaekkukAPI(
        api_key=cfg["domaekkuk"]["api_key"],
        user_id=cfg["domaekkuk"].get("user_id", ""),
        password=cfg["domaekkuk"].get("password", ""),
    )
    dm_cli = DomaemaeClient(
        user_id=cfg["domaemae"]["user_id"],
        password=cfg["domaemae"]["password"],
    )
    # 이메일 미설정 시 notifier=None (오류 이메일만 건너뜀)
    email_cfg = cfg.get("email", {})
    notifier = None
    if email_cfg.get("smtp_host") and email_cfg.get("sender") and email_cfg.get("recipients"):
        try:
            notifier = EmailNotifier(**email_cfg)
        except Exception as e:
            logging.getLogger(__name__).warning("EmailNotifier 초기화 실패 (알림 비활성화): %s", e)

    return ss_api, dk_api, dm_cli, notifier


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in TASKS:
        print(f"사용법: python scripts/run_task.py <{'|'.join(TASKS)}>")
        sys.exit(1)

    task = sys.argv[1]
    cfg  = load_config()
    setup_logging(**cfg["logging"])
    logger = logging.getLogger(__name__)

    init_db(cfg["database"]["path"])
    ss_api, dk_api, dm_cli, notifier = _build_clients(cfg)

    logger.info("=" * 50)
    logger.info("[GitHub Actions] 작업 시작: %s", TASKS[task])

    if task == "collect":
        added = OrderCollector(ss_api).run()
        logger.info("주문 수집 완료: %d건 추가", added)

    elif task == "place":
        stats = OrderPlacer(dk_api, dm_cli, notifier=notifier).run()
        logger.info("자동 발주 완료: %s", stats)

    elif task == "invoice":
        stats = InvoiceManager(ss_api, dk_api, dm_cli, notifier=notifier).run()
        logger.info("송장 동기화 완료: %s", stats)

    elif task == "inventory":
        stats = InventorySync(ss_api, dk_api, dm_cli, notifier=notifier).run()
        logger.info("재고 동기화 완료: %s", stats)

    elif task == "price":
        stats = PriceMonitor(dk_api, dm_cli, notifier=notifier).run()
        logger.info("가격 모니터링 완료: %s", stats)

    logger.info("[GitHub Actions] 작업 완료: %s", TASKS[task])
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
