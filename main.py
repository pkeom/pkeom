"""백그라운드 자동화 데몬 진입점"""
import logging
import signal
import sys

from src.utils.config_loader import load_config
from src.utils.logger import setup_logging
from src.utils.scheduler import AutomationScheduler
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

logger = logging.getLogger(__name__)


def main():
    cfg = load_config()
    setup_logging(**cfg["logging"])

    # SupplierOrder / PriceHistory 는 여전히 DB 사용
    init_db(cfg["database"]["path"])

    ss_cfg = {k: v for k, v in cfg["smartstore"].items()
              if k in ("client_id", "client_secret")}
    ss_api   = SmartstoreAPI(**ss_cfg)
    dk_api   = DomaekkukAPI(**cfg["domaekkuk"])
    dm_cli   = DomaemaeClient(**cfg["domaemae"])
    notifier = EmailNotifier(**cfg["email"])

    collector = OrderCollector(ss_api)
    placer    = OrderPlacer(dk_api, dm_cli)
    invoicer  = InvoiceManager(ss_api, dk_api, dm_cli, notifier=notifier)
    inventory = InventorySync(ss_api, dk_api, dm_cli, notifier=notifier)
    price_mon = PriceMonitor(dk_api, dm_cli, notifier)

    sched     = cfg["schedule"]
    scheduler = AutomationScheduler()
    scheduler.add_job(collector.run,  sched["order_collect_interval"],  "order_collect")
    scheduler.add_job(placer.run,     sched["order_collect_interval"],  "order_place")
    scheduler.add_job(invoicer.run,   sched["invoice_sync_interval"],   "invoice_sync")
    scheduler.add_job(inventory.run,  sched["inventory_sync_interval"], "inventory_sync")
    scheduler.add_job(price_mon.run,  sched["price_monitor_interval"],  "price_monitor")

    scheduler.start()
    logger.info("자동화 시스템 시작 완료")

    def _shutdown(sig, frame):
        logger.info("종료 신호 수신, 스케줄러 종료 중...")
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.pause()


if __name__ == "__main__":
    main()
