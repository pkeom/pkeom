"""로컬 자동화 스케줄러 진입점

사용법:
    python main.py

Ctrl+C 로 종료.
"""
import logging
import signal
import sys
import threading
import time

# ── 단일 인스턴스 잠금 (Windows) ─────────────────────────────────────
import ctypes, ctypes.wintypes as _wt

_MUTEX_NAME = "Global\\SmartStoreDropshipping_MainScheduler"
_mutex_handle = None

def _acquire_single_instance_lock() -> bool:
    global _mutex_handle
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if _mutex_handle and ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None
        return False
    return bool(_mutex_handle)

def _release_single_instance_lock():
    if _mutex_handle:
        ctypes.windll.kernel32.ReleaseMutex(_mutex_handle)
        ctypes.windll.kernel32.CloseHandle(_mutex_handle)

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
from src.core.return_monitor import ReturnMonitor
from src.core.cancel_monitor import CancelMonitor
from src.core.budget_manager import BudgetManager
from src.core.pending_order_repository import PendingOrderRepository
from src.core.stock_pending_repository import StockPendingRepository

logger = logging.getLogger(__name__)

_SCHEDULE_LABELS = {
    "order_collect":  ("주문 수집",     "order_collect_interval"),
    "order_place":    ("자동 발주",     "order_place_interval"),
    "invoice_sync":   ("송장 동기화",   "invoice_sync_interval"),
    "inventory_sync": ("재고 동기화",   "inventory_sync_interval"),
    "price_monitor":  ("가격 모니터링", "price_monitor_interval"),
    "return_monitor": ("반품 감지",     "return_monitor_interval"),
    "cancel_monitor": ("취소 처리",     "cancel_monitor_interval"),
}


def _build_notifier(cfg: dict):
    email_cfg = cfg.get("email", {})
    if not (email_cfg.get("smtp_host") and email_cfg.get("sender") and email_cfg.get("recipients")):
        logger.warning("이메일 설정 미완료 — 알림 비활성화")
        return None
    try:
        return EmailNotifier(**email_cfg)
    except Exception as e:
        logger.warning("EmailNotifier 초기화 실패 (알림 비활성화): %s", e)
        return None


def _print_schedule(sched_cfg: dict):
    lines = [
        "",
        "  ┌─────────────────────────────────────────┐",
        "  │         자동화 스케줄러 시작             │",
        "  ├─────────────────────────────────────────┤",
        f"  │  주문 수집        매 {sched_cfg['order_collect_interval']:>3d}분              │",
        f"  │  자동 발주        매 {sched_cfg.get('order_place_interval', 10):>3d}분              │",
        f"  │  송장 동기화      매 {sched_cfg['invoice_sync_interval']:>3d}분              │",
        f"  │  재고 동기화      매 {sched_cfg['inventory_sync_interval']:>3d}분              │",
        f"  │  가격 모니터링    매 {sched_cfg['price_monitor_interval']:>3d}분              │",
        f"  │  반품 감지        매 {sched_cfg.get('return_monitor_interval', 60):>3d}분              │",
        f"  │  취소 처리        매 {sched_cfg.get('cancel_monitor_interval', 10):>3d}분              │",
        "  ├─────────────────────────────────────────┤",
        "  │  Ctrl+C 로 종료                          │",
        "  └─────────────────────────────────────────┘",
        "",
    ]
    print("\n".join(lines), flush=True)


def _status_printer(scheduler: AutomationScheduler, stop_event: threading.Event):
    """10분마다 다음 실행 예정 시각을 콘솔에 출력"""
    while not stop_event.wait(timeout=600):
        times = scheduler.next_run_times()
        if times:
            print("\n[다음 실행 예정]", flush=True)
            for job_id, next_dt in times.items():
                label = _SCHEDULE_LABELS.get(job_id, (job_id, ""))[0]
                ts = next_dt.strftime("%H:%M:%S") if next_dt else "-"
                print(f"  {label:<12} {ts}", flush=True)
            print("", flush=True)


def main():
    if not _acquire_single_instance_lock():
        print("이미 실행 중인 스케줄러가 있습니다. 중복 실행을 방지합니다.", flush=True)
        sys.exit(1)

    cfg = load_config()
    setup_logging(**cfg["logging"])

    init_db(cfg["database"]["path"])

    ss_cfg = {k: v for k, v in cfg["smartstore"].items()
              if k in ("client_id", "client_secret", "account_type")}
    ss_api   = SmartstoreAPI(**ss_cfg)
    dk_api   = DomaekkukAPI(**cfg["domaekkuk"])
    _dm = cfg.get("domaemae", {})
    dm_cli = DomaemaeClient(
        api_key  = _dm.get("api_key") or cfg["domaekkuk"].get("api_key", ""),
        user_id  = _dm.get("user_id", ""),
        password = _dm.get("password", ""),
    )
    notifier = _build_notifier(cfg)

    budget_amount = cfg.get("budget", 0)
    budget        = BudgetManager(initial_balance=budget_amount) if budget_amount > 0 else None
    pending       = PendingOrderRepository() if budget is not None else None
    stock_pending = StockPendingRepository()

    collector = OrderCollector(ss_api)
    placer    = OrderPlacer(
        dk_api, dm_cli,
        notifier=notifier, budget=budget, pending=pending,
        ss_api=ss_api, stock_pending=stock_pending,
    )
    invoicer  = InvoiceManager(ss_api, dk_api, dm_cli, notifier=notifier)
    inventory = InventorySync(ss_api, dk_api, dm_cli, notifier=notifier, order_placer=placer)
    price_mon   = PriceMonitor(dk_api, dm_cli, notifier)
    return_mon  = ReturnMonitor(ss_api, notifier)
    cancel_mon  = CancelMonitor(ss_api, notifier, dk_api=dk_api, dm_cli=dm_cli)

    sched_cfg = cfg["schedule"]
    scheduler = AutomationScheduler()

    # run_now=True: start() 직후 각 작업을 즉시 1회 실행 후 interval 반복
    scheduler.add_job(collector.run,   sched_cfg["order_collect_interval"],               "order_collect",  run_now=True)
    scheduler.add_job(placer.run,      sched_cfg.get("order_place_interval", 10),         "order_place",    run_now=True)
    scheduler.add_job(invoicer.run,    sched_cfg["invoice_sync_interval"],   "invoice_sync",   run_now=True)
    scheduler.add_job(inventory.run,   sched_cfg["inventory_sync_interval"], "inventory_sync", run_now=True)
    scheduler.add_job(price_mon.run,    sched_cfg["price_monitor_interval"],   "price_monitor",  run_now=True)
    scheduler.add_job(return_mon.run,   sched_cfg.get("return_monitor_interval", 60), "return_monitor", run_now=True)
    scheduler.add_job(cancel_mon.run,   sched_cfg.get("cancel_monitor_interval", 10), "cancel_monitor", run_now=True)

    _print_schedule(sched_cfg)
    scheduler.start()
    logger.info("자동화 스케줄러 시작 완료 (즉시 첫 실행 포함)")

    # 웹 대시보드 시작 (별도 데몬 스레드)
    try:
        from dashboard import run_dashboard
        dash_thread = threading.Thread(
            target=run_dashboard, daemon=True, name="dashboard"
        )
        dash_thread.start()
        print("  대시보드: http://localhost:2713", flush=True)
        logger.info("웹 대시보드 시작: http://localhost:2713")
    except Exception as e:
        logger.warning("웹 대시보드 시작 실패 (무시하고 계속): %s", e)

    stop_event = threading.Event()

    # 주기적 상태 출력 스레드
    status_thread = threading.Thread(
        target=_status_printer,
        args=(scheduler, stop_event),
        daemon=True,
    )
    status_thread.start()

    def _shutdown(sig=None, frame=None):
        print("\n종료 신호 수신 — 스케줄러 종료 중...", flush=True)
        logger.info("종료 신호 수신, 스케줄러 종료 중...")
        scheduler.stop()
        stop_event.set()
        _release_single_instance_lock()

    signal.signal(signal.SIGINT, _shutdown)
    # SIGTERM은 Windows에 없으므로 존재할 때만 등록
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    # Windows 호환 메인 루프 (signal.pause() 대체)
    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown()
    except Exception as e:
        logger.error("예기치 않은 오류로 종료: %s", e, exc_info=True)
        _shutdown()

    print("종료 완료.", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
