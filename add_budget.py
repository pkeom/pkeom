"""예산 충전 CLI

사용법:
    python add_budget.py 500000
    python add_budget.py 500000 "2차 충전"
"""
import sys
from src.utils.config_loader import load_config
from src.core.budget_manager import BudgetManager
from src.core.order_placer import OrderPlacer
from src.core.pending_order_repository import PendingOrderRepository
from src.api.domaekkuk import DomaekkukAPI
from src.api.domaemae import DomaemaeClient
from src.notifications.email_notifier import EmailNotifier


def main():
    if len(sys.argv) < 2:
        print("사용법: python add_budget.py <금액> [사유]")
        sys.exit(1)

    try:
        amount = int(sys.argv[1])
    except ValueError:
        print(f"오류: 금액은 정수여야 합니다 (입력값: {sys.argv[1]})")
        sys.exit(1)

    if amount <= 0:
        print(f"오류: 금액은 양수여야 합니다 (입력값: {amount})")
        sys.exit(1)

    reason = sys.argv[2] if len(sys.argv) > 2 else "수동 충전"

    cfg = load_config()

    budget  = BudgetManager()
    pending = PendingOrderRepository()

    new_balance = budget.charge(amount, reason)
    print(f"예산 충전 완료: +{amount:,}원 ({reason})")
    print(f"현재 잔액: {new_balance:,}원")

    # 대기 주문이 있으면 자동 재개 시도
    count = pending.count()
    if count == 0:
        print("대기 주문 없음.")
        return

    print(f"\n대기 주문 {count}건 재개 시도 중...")

    # 알림 설정
    notifier = None
    try:
        email_cfg = cfg.get("email", {})
        if email_cfg.get("smtp_host") and email_cfg.get("sender") and email_cfg.get("recipients"):
            notifier = EmailNotifier(**email_cfg)
    except Exception:
        pass

    dk_api = DomaekkukAPI(**cfg["domaekkuk"])
    _dm = cfg.get("domaemae", {})
    dm_cli = DomaemaeClient(
        api_key  = _dm.get("api_key") or cfg["domaekkuk"].get("api_key", ""),
        user_id  = _dm.get("user_id", ""),
        password = _dm.get("password", ""),
    )

    placer = OrderPlacer(
        dk_api, dm_cli,
        notifier=notifier,
        budget=budget,
        pending=pending,
    )
    stats = placer.resume_pending()
    print(
        f"재개 결과: 발주 {stats['ordered']}건 / "
        f"계속 대기 {stats['still_pending']}건 / "
        f"오류 {stats['error']}건"
    )


if __name__ == "__main__":
    main()
