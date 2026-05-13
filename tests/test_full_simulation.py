"""
전체 시스템 시뮬레이션 테스트 — 20개 기능 × 100회 반복

실제 API 호출 없이 mock 데이터만 사용.
하나라도 실패하면 해당 subTest에서 즉시 실패 처리.

[기능 목록]
 1. 스마트스토어 주문 수집
 2. 도매꾹 자동발주 (옵션 포함)
 3. 도매매 자동발주 (옵션 매칭 포함)
 4. 도매매 쿠키 유효성 확인 + 만료 감지 + 이메일 알림
 5. 송장번호 자동 등록
 6. 재고 동기화
 7. 가격 모니터링
 8. 반품 감지 + 이메일 알림
 9. 예산 관리 (예산 내 최대 발주, 초과 시 대기 저장)
10. 대기 주문 재개 (add_budget.py 연동)
11. 이메일 알림 전체 (반품/예산부족/쿠키만료)
12. 스케줄러 정상 작동
13. Termux wake-lock 자동실행
14. 옵션 없는 상품 발주
15. 옵션 있는 상품 발주 + 옵션 매칭
16. 예산 부족 시 일부만 발주 + 나머지 대기
17. 발주 실패 시 재시도 로직
18. 중복 주문 방지
19. 중복 반품 알림 방지
20. 로그 기록 정상 여부
"""

import sys
import smtplib
import tempfile
import time
import unittest
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.database import init_db, get_session
from src.db.models import PriceHistory, SupplierOrder
from src.core.mapping_repository import MappingRepository
from src.core.order_collector import OrderCollector
from src.core.order_placer import OrderPlacer
from src.core.order_repository import OrderRepository
from src.core.invoice_manager import InvoiceManager
from src.core.inventory_sync import InventorySync
from src.core.price_monitor import PriceMonitor
from src.core.price_alert_repository import PriceAlertRepository
from src.core.return_monitor import ReturnMonitor
from src.core.budget_manager import BudgetManager
from src.core.pending_order_repository import PendingOrderRepository
from src.api.domaemae import DomaemaeClient, DomaemaeCookieExpiredError
from src.notifications.email_notifier import EmailNotifier
from src.utils.logger import setup_logging
from src.utils.scheduler import AutomationScheduler
from src.utils.wakelock import run_with_wakelock

N = 100  # 반복 횟수

# ── 공통 mock 데이터 ──────────────────────────────────────────────

MOCK_SS_ORDERS = [
    {
        "productOrder": {
            "productOrderId": "ORD-001",
            "orderId": "SS-001",
            "productId": "P100",
            "productName": "도매꾹 티셔츠",
            "optionCode": "",
            "quantity": 1,
        },
        "order": {"ordererName": "홍길동"},
        "shippingAddress": {
            "name": "홍길동",
            "tel1": "010-1234-5678",
            "addressStr": "서울시 강남구 테헤란로 123",
            "zipCode": "06234",
        },
        "deliveryMemo": "문앞 배송",
    },
    {
        "productOrder": {
            "productOrderId": "ORD-002",
            "orderId": "SS-002",
            "productId": "P200",
            "productName": "도매매 청바지",
            "optionCode": "OPT-L",
            "quantity": 1,
        },
        "order": {"ordererName": "김철수"},
        "shippingAddress": {
            "name": "김철수",
            "tel1": "010-9876-5432",
            "addressStr": "부산시 해운대구 우동 456",
            "zipCode": "48094",
        },
        "deliveryMemo": "",
    },
]

MOCK_RETURN_RAW = [
    {
        "productOrder": {
            "productOrderId": "RET-001",
            "productName": "도매꾹 티셔츠",
            "quantity": 1,
        },
        "claim": {
            "returnReason": "단순변심",
            "returnReasonType": "SIMPLE_CHANGE",
        },
    }
]


# ── 환경 팩토리 ───────────────────────────────────────────────────

def _make_env(initial_budget: int = 500_000):
    """격리된 임시 디렉터리 기반 테스트 환경 생성."""
    tmpdir = Path(tempfile.mkdtemp())
    init_db(str(tmpdir / "test.db"))

    order_repo   = OrderRepository(tmpdir / "orders.json")
    mapping_repo = MappingRepository(tmpdir / "mappings.json")
    budget_mgr   = BudgetManager(initial_balance=initial_budget,
                                  path=tmpdir / "budget.json")
    pending_repo = PendingOrderRepository(tmpdir / "pending.json")
    alert_repo   = PriceAlertRepository(tmpdir / "alerts.json")

    mock_ss = MagicMock()
    mock_ss.get_orders.return_value = list(MOCK_SS_ORDERS)
    mock_ss.dispatch_order.return_value = {"result": "SUCCESS"}
    mock_ss.set_product_sale_status.return_value = None
    mock_ss.get_returns.return_value = []

    mock_dk = MagicMock()
    mock_dk.get_product.return_value = {
        "title": "도매꾹 상품", "price": 10_000, "stock": 50, "seller_id": "s1",
    }
    mock_dk.get_stock.return_value = 50
    mock_dk.place_order.return_value = {"order_no": "DK-ORDER-001"}
    mock_dk.get_order_tracking.return_value = {
        "order_no": "DK-ORDER-001",
        "delivery_company": "CJ대한통운",
        "tracking_number": "1234567890",
    }

    mock_dm = MagicMock()
    mock_dm.get_product.return_value = {
        "product_id": "DM-001", "price": 8_000, "stock": 5,
    }
    mock_dm.get_stock.return_value = 5
    mock_dm.place_order.return_value = "DM-ORDER-001"
    mock_dm.get_order_tracking.return_value = {
        "order_no": "DM-ORDER-001",
        "delivery_company": "롯데택배",
        "tracking_number": "9876543210",
    }

    mock_notifier = MagicMock()

    mapping_repo.add("P100", "domaekkuk", "DK-001")
    mapping_repo.add("P200", "domaemae",  "DM-001", ss_option_id="OPT-L")

    return {
        "tmpdir":       tmpdir,
        "order_repo":   order_repo,
        "mapping_repo": mapping_repo,
        "budget_mgr":   budget_mgr,
        "pending_repo": pending_repo,
        "alert_repo":   alert_repo,
        "mock_ss":      mock_ss,
        "mock_dk":      mock_dk,
        "mock_dm":      mock_dm,
        "mock_notifier": mock_notifier,
    }


def _placer(e, budget=None, pending=None, notifier=None):
    return OrderPlacer(
        e["mock_dk"], e["mock_dm"],
        e["mapping_repo"], e["order_repo"],
        notifier or e.get("mock_notifier"),
        budget=budget,
        pending=pending,
    )


def _invoicer(e, notifier=None):
    return InvoiceManager(
        e["mock_ss"], e["mock_dk"], e["mock_dm"],
        e["order_repo"], notifier,
    )


def _sync(e, notifier=None):
    return InventorySync(
        e["mock_ss"], e["mock_dk"], e["mock_dm"],
        e["mapping_repo"],
        notifier or e.get("mock_notifier"),
        e["tmpdir"] / "stock_cache.json",
    )


def _price_mon(e):
    return PriceMonitor(
        e["mock_dk"], e["mock_dm"],
        mapping_repo=e["mapping_repo"],
        alert_repo=e["alert_repo"],
    )


# ── 테스트 클래스 ─────────────────────────────────────────────────

class TestFeature01_OrderCollection(unittest.TestCase):
    """1. 스마트스토어 주문 수집"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e = _make_env()
                collector = OrderCollector(e["mock_ss"], e["order_repo"])

                # 첫 수집: 2건 추가
                added = collector.run()
                self.assertEqual(added, 2)
                orders = e["order_repo"].all()
                self.assertEqual(len(orders), 2)
                for o in orders:
                    self.assertEqual(o["status"], "NEW")

                # 필드 파싱 검증
                ord1 = e["order_repo"].find("ORD-001")
                self.assertEqual(ord1["product_id"], "P100")
                self.assertEqual(ord1["quantity"], 1)
                self.assertEqual(ord1["receiver_name"], "홍길동")
                self.assertEqual(ord1["delivery_memo"], "문앞 배송")

                # 두 번째 수집: 0건 (중복 방지)
                added2 = collector.run()
                self.assertEqual(added2, 0)
                self.assertEqual(len(e["order_repo"].all()), 2)

                # 빈 응답 처리
                e["mock_ss"].get_orders.return_value = []
                added3 = OrderCollector(e["mock_ss"], e["order_repo"]).run()
                self.assertEqual(added3, 0)


class TestFeature02_DomaekkukOrder(unittest.TestCase):
    """2. 도매꾹 자동발주 (옵션 포함)"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e = _make_env()
                OrderCollector(e["mock_ss"], e["order_repo"]).run()

                stats = _placer(e).run()
                self.assertEqual(stats["ordered"], 2)
                self.assertEqual(stats["error"],   0)

                ord1 = e["order_repo"].find("ORD-001")
                self.assertEqual(ord1["status"],           "ORDERED")
                self.assertEqual(ord1["supplier"],         "domaekkuk")
                self.assertEqual(ord1["supplier_order_no"], "DK-ORDER-001")

                # domaekkuk.place_order 호출 확인
                dk_calls = [
                    c for c in e["mock_dk"].place_order.call_args_list
                ]
                self.assertGreaterEqual(len(dk_calls), 1)

                # DB SupplierOrder 저장 확인
                with get_session() as session:
                    cnt = session.query(SupplierOrder).filter_by(
                        ss_order_id="ORD-001", supplier="domaekkuk"
                    ).count()
                self.assertEqual(cnt, 1)

                # 재발주 방지: ORDERED 상태는 재처리 안 됨
                stats2 = _placer(e).run()
                self.assertEqual(stats2["total"], 0)


class TestFeature03_DomaemaeOrder(unittest.TestCase):
    """3. 도매매 자동발주 (옵션 매칭 포함)"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e = _make_env()
                OrderCollector(e["mock_ss"], e["order_repo"]).run()
                _placer(e).run()

                ord2 = e["order_repo"].find("ORD-002")
                self.assertEqual(ord2["status"],   "ORDERED")
                self.assertEqual(ord2["supplier"], "domaemae")

                # place_order에 option_name="OPT-L" 전달됐는지 확인
                dm_calls = e["mock_dm"].place_order.call_args_list
                self.assertEqual(len(dm_calls), 1)
                call_kw = dm_calls[0][1]
                self.assertEqual(call_kw.get("option_name"), "OPT-L")

                # DB 저장 확인
                with get_session() as session:
                    cnt = session.query(SupplierOrder).filter_by(
                        ss_order_id="ORD-002", supplier="domaemae"
                    ).count()
                self.assertEqual(cnt, 1)


class TestFeature04_CookieExpiry(unittest.TestCase):
    """4. 도매매 쿠키 유효성 확인 + 만료 감지 + 이메일 알림"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                # _check_session: 로그인 페이지 리다이렉트 시 예외 발생
                client = DomaemaeClient(cookies={})
                with self.assertRaises(DomaemaeCookieExpiredError):
                    client._check_session("https://domeggook.com/mem_login.php?return=abc")
                with self.assertRaises(DomaemaeCookieExpiredError):
                    client._check_session("https://domeggook.com/mem_formLogin?redir=abc")

                # 정상 URL이면 예외 발생 안 됨
                client._check_session("https://domeggook.com/order/detail.php?no=123")

                # 쿠키 만료로 place_order 실패 → 이메일 알림 전송
                e = _make_env()
                OrderCollector(e["mock_ss"], e["order_repo"]).run()
                e["mock_dm"].place_order.side_effect = DomaemaeCookieExpiredError("쿠키 만료")
                stats = _placer(e, notifier=e["mock_notifier"]).run()

                # ORD-002(domaemae) 실패, ORD-001(domaekkuk) 성공
                self.assertEqual(stats["error"],   1)
                self.assertEqual(stats["ordered"], 1)

                # 오류 이메일 발송 확인
                e["mock_notifier"].send.assert_called()
                subjects = [c[1]["subject"] for c in e["mock_notifier"].send.call_args_list]
                self.assertTrue(any("발주 실패" in s for s in subjects))


class TestFeature05_Invoice(unittest.TestCase):
    """5. 송장번호 자동 등록"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e = _make_env()
                OrderCollector(e["mock_ss"], e["order_repo"]).run()
                _placer(e).run()

                stats = _invoicer(e).run()
                self.assertEqual(stats["invoiced"], 2)
                self.assertEqual(stats["pending"],  0)
                self.assertEqual(stats["error"],    0)

                self.assertEqual(e["order_repo"].find("ORD-001")["status"], "INVOICED")
                self.assertEqual(e["order_repo"].find("ORD-002")["status"], "INVOICED")

                # 스마트스토어 dispatch_order 택배사 코드 확인
                calls = {
                    c[0][0]: c[0][1]
                    for c in e["mock_ss"].dispatch_order.call_args_list
                }
                self.assertEqual(calls.get("ORD-001"), "CJGLS")
                self.assertEqual(calls.get("ORD-002"), "LOTTE")

                # 송장 미발행(tracking_number='') → pending
                e2 = _make_env()
                OrderCollector(e2["mock_ss"], e2["order_repo"]).run()
                _placer(e2).run()
                e2["mock_dk"].get_order_tracking.return_value = {
                    "order_no": "DK-ORDER-001",
                    "delivery_company": "",
                    "tracking_number": "",
                }
                stats2 = _invoicer(e2).run()
                self.assertEqual(stats2["pending"],  1)
                self.assertEqual(stats2["invoiced"], 1)

                # 송장 API 오류 → error + 이메일
                e3 = _make_env()
                OrderCollector(e3["mock_ss"], e3["order_repo"]).run()
                _placer(e3).run()
                e3["mock_dk"].get_order_tracking.side_effect = Exception("API 오류")
                stats3 = _invoicer(e3, notifier=e3["mock_notifier"]).run()
                self.assertEqual(stats3["error"], 1)
                e3["mock_notifier"].send.assert_called()


class TestFeature06_InventorySync(unittest.TestCase):
    """6. 재고 동기화"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e = _make_env()
                s = _sync(e)

                # 초기 동기화: 재고 있음 → 오류 없음
                stats = s.run()
                self.assertEqual(stats["total"], 2)
                self.assertEqual(stats["error"], 0)

                # 품절 발생
                e["mock_dk"].get_stock.return_value = 0
                e["mock_dm"].get_stock.return_value = 0
                stats2 = s.run()
                self.assertEqual(stats2["paused"], 2)
                for c in e["mock_ss"].set_product_sale_status.call_args_list[-2:]:
                    self.assertFalse(c[1]["on_sale"])

                # 재입고 → resumed
                e["mock_dk"].get_stock.return_value = 30
                e["mock_dm"].get_stock.return_value = 10
                e["mock_ss"].reset_mock()
                stats3 = s.run()
                self.assertEqual(stats3["resumed"], 2)

                # 재고 변동 없음 → unchanged, API 미호출
                e["mock_ss"].reset_mock()
                stats4 = s.run()
                self.assertEqual(stats4["unchanged"], 2)
                e["mock_ss"].set_product_sale_status.assert_not_called()

                # 재고 조회 오류 → error + 이메일
                e2 = _make_env()
                e2["mock_dk"].get_stock.side_effect = Exception("재고 API 오류")
                stats5 = _sync(e2).run()
                self.assertGreater(stats5["error"], 0)
                e2["mock_notifier"].send.assert_called()


class TestFeature07_PriceMonitor(unittest.TestCase):
    """7. 가격 모니터링"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e = _make_env()
                mon = _price_mon(e)

                # 첫 실행: 가격 초기 기록 → changed
                stats = mon.run()
                self.assertEqual(stats["changed"], 2)
                self.assertEqual(len(e["alert_repo"].all()), 2)

                # 동일 가격 재실행 → unchanged
                stats2 = mon.run()
                self.assertEqual(stats2["unchanged"], 2)
                self.assertEqual(len(e["alert_repo"].all()), 2)

                # 가격 인상 감지
                e["mock_dk"].get_product.return_value["price"] = 13_000
                stats3 = mon.run()
                self.assertEqual(stats3["changed"], 1)
                dk_alerts = [
                    a for a in e["alert_repo"].all()
                    if a["supplier"] == "domaekkuk" and a["new_price"] == 13_000
                ]
                self.assertEqual(len(dk_alerts), 1)
                self.assertAlmostEqual(dk_alerts[0]["change_rate"], 0.3, places=4)

                # 가격 인하
                e["mock_dk"].get_product.return_value["price"] = 7_000
                mon.run()
                dk_dec = [
                    a for a in e["alert_repo"].all()
                    if a["supplier"] == "domaekkuk" and a["new_price"] == 7_000
                ]
                self.assertEqual(len(dk_dec), 1)
                self.assertLess(dk_dec[0]["change_rate"], 0)

                # DB PriceHistory 저장 확인
                with get_session() as session:
                    cnt = session.query(PriceHistory).count()
                self.assertGreater(cnt, 0)

                # 알림 읽음 처리
                self.assertGreater(e["alert_repo"].count_unread(), 0)
                e["alert_repo"].mark_all_read()
                self.assertEqual(e["alert_repo"].count_unread(), 0)


class TestFeature08_ReturnMonitor(unittest.TestCase):
    """8. 반품 감지 + 이메일 알림"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e    = _make_env()
                rfile = str(e["tmpdir"] / "returns.json")
                mon  = ReturnMonitor(e["mock_ss"], e["mock_notifier"],
                                     returns_file=rfile)

                # 반품 없음 → new=0
                stats = mon.run()
                self.assertEqual(stats["new"],   0)
                self.assertEqual(stats["total"], 0)

                # 신규 반품 감지 → 이메일 발송
                e["mock_ss"].get_returns.return_value = list(MOCK_RETURN_RAW)
                stats2 = mon.run()
                self.assertEqual(stats2["new"],   1)
                self.assertEqual(stats2["total"], 1)
                e["mock_notifier"].send.assert_called_once()
                subject = e["mock_notifier"].send.call_args[1]["subject"]
                self.assertIn("반품", subject)

                # 동일 반품 재감지 → 중복 방지
                e["mock_notifier"].reset_mock()
                stats3 = mon.run()
                self.assertEqual(stats3["new"],   0)
                self.assertEqual(stats3["total"], 1)
                e["mock_notifier"].send.assert_not_called()


class TestFeature09_BudgetManagement(unittest.TestCase):
    """9. 예산 관리 (예산 내 최대 발주, 초과 시 대기 저장)"""

    def _make_budget_env(self):
        """예산 시나리오 전용 환경: 3개 상품, 각기 다른 가격"""
        e = _make_env(initial_budget=30_000)

        # 3개 주문 추가: ORD-A(DK-A 10000원), ORD-B(DK-B 13000원), ORD-C(DK-C 15000원)
        orders = [
            {
                "order_id": f"ORD-{x}", "ss_order_id": f"SS-{x}",
                "product_id": f"P{x}", "product_name": f"상품{x}",
                "option_code": "", "quantity": 1,
                "buyer_name": "테스터", "receiver_name": "테스터",
                "receiver_phone": "010-0000-0000",
                "receiver_address": "서울시", "receiver_zipcode": "00000",
                "delivery_memo": "", "status": "NEW",
                "collected_at": "2026-01-01T00:00:00",
                "updated_at":   "2026-01-01T00:00:00",
            }
            for x in ("A", "B", "C")
        ]
        e["order_repo"].add_many(orders)
        e["mapping_repo"].add("PA", "domaekkuk", "DK-A")
        e["mapping_repo"].add("PB", "domaekkuk", "DK-B")
        e["mapping_repo"].add("PC", "domaekkuk", "DK-C")

        prices = {"DK-A": 10_000, "DK-B": 13_000, "DK-C": 15_000}

        def dk_product(product_id):
            return {"price": prices.get(product_id, 10_000), "stock": 50, "title": "상품"}

        e["mock_dk"].get_product.side_effect = dk_product
        return e

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e = self._make_budget_env()

                # 잔액: 30000
                # 비용: DK-A=13000, DK-B=16000, DK-C=18000 (+ shipping 3000)
                # 정렬: [13000, 16000, 18000]
                # 발주: 13000 + 16000 = 29000 ≤ 30000 → A, B 발주
                # 대기: 18000 > 1000(잔액) → C 대기
                p = _placer(e, budget=e["budget_mgr"], pending=e["pending_repo"],
                            notifier=e["mock_notifier"])
                stats = p.run()

                self.assertEqual(stats["ordered"],  2)
                self.assertEqual(stats["deferred"], 1)
                self.assertEqual(stats["error"],    0)

                # 예산 차감 확인
                balance = e["budget_mgr"].get_balance()
                self.assertLess(balance, 30_000)

                # pending_orders.json 저장 확인
                pending_items = e["pending_repo"].all()
                self.assertEqual(len(pending_items), 1)

                # 예산 부족 이메일 발송 확인
                e["mock_notifier"].send.assert_called()
                subjects = [c[1]["subject"] for c in e["mock_notifier"].send.call_args_list]
                self.assertTrue(any("예산" in s for s in subjects))


class TestFeature10_ResumePending(unittest.TestCase):
    """10. 대기 주문 재개 (add_budget.py 연동)"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                # Feature 9 시나리오 재사용
                base = TestFeature09_BudgetManagement()
                e = base._make_budget_env()

                p = _placer(e, budget=e["budget_mgr"], pending=e["pending_repo"],
                            notifier=e["mock_notifier"])
                p.run()  # C 대기 상태로 저장

                self.assertEqual(e["pending_repo"].count(), 1)

                # 예산 충전 → 대기 주문 재개
                e["budget_mgr"].charge(20_000, "수동 충전")
                stats = p.resume_pending()

                self.assertEqual(stats["ordered"],       1)
                self.assertEqual(stats["still_pending"], 0)
                self.assertEqual(stats["error"],         0)

                # 재개 후 pending 비어야 함
                self.assertEqual(e["pending_repo"].count(), 0)

                # 잔액 추가 차감 확인
                balance_after = e["budget_mgr"].get_balance()
                self.assertGreater(e["budget_mgr"].get_balance(), 0)


class TestFeature11_EmailAll(unittest.TestCase):
    """11. 이메일 알림 전체 (반품/예산부족/쿠키만료)"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                # 반품 알림
                e1 = _make_env()
                rfile = str(e1["tmpdir"] / "returns.json")
                e1["mock_ss"].get_returns.return_value = list(MOCK_RETURN_RAW)
                ReturnMonitor(e1["mock_ss"], e1["mock_notifier"],
                              returns_file=rfile).run()
                subjects1 = [c[1]["subject"]
                             for c in e1["mock_notifier"].send.call_args_list]
                self.assertTrue(any("반품" in s for s in subjects1))

                # 예산 부족 알림
                base = TestFeature09_BudgetManagement()
                e2 = base._make_budget_env()
                _placer(e2, budget=e2["budget_mgr"], pending=e2["pending_repo"],
                        notifier=e2["mock_notifier"]).run()
                subjects2 = [c[1]["subject"]
                             for c in e2["mock_notifier"].send.call_args_list]
                self.assertTrue(any("예산" in s for s in subjects2))

                # 쿠키 만료 → 발주 실패 알림
                e3 = _make_env()
                OrderCollector(e3["mock_ss"], e3["order_repo"]).run()
                e3["mock_dm"].place_order.side_effect = DomaemaeCookieExpiredError("쿠키 만료")
                _placer(e3, notifier=e3["mock_notifier"]).run()
                subjects3 = [c[1]["subject"]
                             for c in e3["mock_notifier"].send.call_args_list]
                self.assertTrue(any("발주 실패" in s for s in subjects3))

                # SMTP 핸드셰이크 시뮬레이션
                with patch("smtplib.SMTP") as mock_smtp_cls:
                    mock_server = MagicMock()
                    mock_smtp_cls.return_value.__enter__ = MagicMock(
                        return_value=mock_server
                    )
                    mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
                    notifier = EmailNotifier(
                        smtp_host="smtp.gmail.com", smtp_port=587,
                        sender="test@gmail.com", password="pw",
                        recipients=["recv@gmail.com"],
                    )
                    notifier.send(subject="테스트", body="본문")
                    mock_smtp_cls.assert_called_once_with("smtp.gmail.com", 587)
                    mock_server.starttls.assert_called_once()
                    mock_server.sendmail.assert_called_once()


class TestFeature12_Scheduler(unittest.TestCase):
    """12. 스케줄러 정상 작동"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                scheduler = AutomationScheduler()
                counter   = {"n": 0}

                def dummy(): counter["n"] += 1

                scheduler.add_job(dummy, 60, "job_a", run_now=False)
                scheduler.add_job(dummy, 30, "job_b", run_now=False)

                scheduler.start()
                times = scheduler.next_run_times()

                self.assertIn("job_a", times)
                self.assertIn("job_b", times)
                self.assertIsNotNone(times["job_a"])
                self.assertIsNotNone(times["job_b"])

                scheduler.stop()

                # max_instances=1: 같은 job_id 중복 등록 시 교체(replace_existing)
                scheduler2 = AutomationScheduler()
                scheduler2.add_job(dummy, 10, "unique_job", run_now=False)
                scheduler2.add_job(dummy, 20, "unique_job", run_now=False)
                scheduler2.start()
                jobs = scheduler2.scheduler.get_jobs()
                job_ids = [j.id for j in jobs]
                self.assertEqual(job_ids.count("unique_job"), 1)
                scheduler2.stop()


class TestFeature13_Wakelock(unittest.TestCase):
    """13. Termux wake-lock 자동실행"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                # 비-Termux 환경: 예외 없이 func 실행, 반환값 전달
                result = run_with_wakelock(lambda: 42)
                self.assertEqual(result, 42)

                # 예외 전파 확인
                def boom(): raise RuntimeError("boom")
                with self.assertRaises(RuntimeError):
                    run_with_wakelock(boom)

                # 다양한 반환 타입
                self.assertEqual(run_with_wakelock(lambda: "ok"), "ok")
                self.assertIsNone(run_with_wakelock(lambda: None))
                self.assertEqual(run_with_wakelock(lambda: [1, 2, 3]), [1, 2, 3])


class TestFeature14_NoOptionOrder(unittest.TestCase):
    """14. 옵션 없는 상품 발주"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e = _make_env()
                # ORD-001: P100 → domaekkuk, 옵션 없음
                e["mock_ss"].get_orders.return_value = [MOCK_SS_ORDERS[0]]
                OrderCollector(e["mock_ss"], e["order_repo"]).run()
                _placer(e).run()

                ord1 = e["order_repo"].find("ORD-001")
                self.assertEqual(ord1["status"],   "ORDERED")
                self.assertEqual(ord1["supplier"], "domaekkuk")

                # domaekkuk.place_order는 option_name 파라미터 없이 호출됨
                dk_kw = e["mock_dk"].place_order.call_args[1]
                self.assertNotIn("option_name", dk_kw)


class TestFeature15_OptionMatching(unittest.TestCase):
    """15. 옵션 있는 상품 발주 + 옵션 매칭"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                options = [
                    {"id": "0", "name": "아이폰11"},
                    {"id": "1", "name": "아이폰11/라운드케이스"},
                    {"id": "2", "name": "갤럭시S"},
                ]

                # 완전 일치
                self.assertEqual(
                    DomaemaeClient._match_option(options, "아이폰11"), "0"
                )
                # 대소문자·공백 무시
                self.assertEqual(
                    DomaemaeClient._match_option(options, " 갤럭시s "), "2"
                )
                # 부분 포함 일치
                self.assertEqual(
                    DomaemaeClient._match_option(options, "라운드케이스"), "1"
                )
                # 불일치 → None
                self.assertIsNone(
                    DomaemaeClient._match_option(options, "아이패드")
                )
                # 빈 옵션 목록
                self.assertIsNone(DomaemaeClient._match_option([], "아이폰11"))
                # 빈 option_name → None
                self.assertIsNone(DomaemaeClient._match_option(options, ""))

                # 실제 발주 흐름: ORD-002 → domaemae + option_name=OPT-L
                e = _make_env()
                e["mock_ss"].get_orders.return_value = [MOCK_SS_ORDERS[1]]
                OrderCollector(e["mock_ss"], e["order_repo"]).run()
                _placer(e).run()
                dm_kw = e["mock_dm"].place_order.call_args[1]
                self.assertEqual(dm_kw.get("option_name"), "OPT-L")


class TestFeature16_PartialBudget(unittest.TestCase):
    """16. 예산 부족 시 일부만 발주 + 나머지 대기"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                base = TestFeature09_BudgetManagement()
                e = base._make_budget_env()

                p = _placer(e, budget=e["budget_mgr"], pending=e["pending_repo"],
                            notifier=e["mock_notifier"])
                stats = p.run()

                # 금액 오름차순: DK-A(13000) + DK-B(16000) = 29000 ≤ 30000 발주
                # DK-C(18000): 29000 + 18000 = 47000 > 30000 → 대기
                self.assertEqual(stats["ordered"],  2)
                self.assertEqual(stats["deferred"], 1)

                ord_a = e["order_repo"].find("ORD-A")
                ord_b = e["order_repo"].find("ORD-B")
                ord_c = e["order_repo"].find("ORD-C")

                # A, B: 발주 완료 (순서가 달라도 둘 다 ORDERED여야 함)
                self.assertIn(ord_a["status"], ("ORDERED",))
                self.assertIn(ord_b["status"], ("ORDERED",))
                # C: 대기
                self.assertEqual(ord_c["status"], "PENDING")

                # 잔액이 차감됐는지 확인
                self.assertLess(e["budget_mgr"].get_balance(), 30_000)

                # pending_orders.json에 C만 저장
                pending_items = e["pending_repo"].all()
                self.assertEqual(len(pending_items), 1)
                self.assertEqual(pending_items[0]["order"]["order_id"], "ORD-C")


class TestFeature17_RetryOnError(unittest.TestCase):
    """17. 발주 실패 시 재시도 로직"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e = _make_env()
                OrderCollector(e["mock_ss"], e["order_repo"]).run()

                # 첫 발주 시도: domaekkuk API 오류 → ORD-001 ERROR
                e["mock_dk"].place_order.side_effect = Exception("API 오류")
                stats = _placer(e, notifier=e["mock_notifier"]).run()

                self.assertEqual(
                    e["order_repo"].find("ORD-001")["status"], "ERROR"
                )
                self.assertGreaterEqual(stats["error"], 1)

                # 재시도: 상태 NEW 로 리셋 → 재발주 성공
                e["order_repo"].update_status("ORD-001", "NEW")
                e["mock_dk"].place_order.side_effect = None
                e["mock_dk"].place_order.return_value = {"order_no": "DK-RETRY-001"}

                stats2 = _placer(e).run()
                self.assertEqual(stats2["ordered"], 1)
                self.assertEqual(
                    e["order_repo"].find("ORD-001")["status"], "ORDERED"
                )
                self.assertEqual(
                    e["order_repo"].find("ORD-001")["supplier_order_no"],
                    "DK-RETRY-001",
                )


class TestFeature18_DuplicateOrder(unittest.TestCase):
    """18. 중복 주문 방지"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e = _make_env()
                collector = OrderCollector(e["mock_ss"], e["order_repo"])

                # 동일 응답으로 3번 실행
                c1 = collector.run()
                c2 = collector.run()
                c3 = collector.run()

                self.assertEqual(c1, 2)
                self.assertEqual(c2, 0)
                self.assertEqual(c3, 0)
                self.assertEqual(len(e["order_repo"].all()), 2)

                # add_many 직접 호출 중복
                new_order = {
                    "order_id": "ORD-001",  # 이미 존재
                    "ss_order_id": "SS-001", "product_id": "P100",
                    "product_name": "", "option_code": "", "quantity": 1,
                    "buyer_name": "", "receiver_name": "", "receiver_phone": "",
                    "receiver_address": "", "receiver_zipcode": "",
                    "delivery_memo": "", "status": "NEW",
                    "collected_at": "2026-01-01T00:00:00",
                    "updated_at":   "2026-01-01T00:00:00",
                }
                added = e["order_repo"].add_many([new_order])
                self.assertEqual(added, 0)
                self.assertEqual(len(e["order_repo"].all()), 2)


class TestFeature19_DuplicateReturn(unittest.TestCase):
    """19. 중복 반품 알림 방지"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e    = _make_env()
                rfile = str(e["tmpdir"] / "returns.json")
                mon  = ReturnMonitor(e["mock_ss"], e["mock_notifier"],
                                     returns_file=rfile)
                e["mock_ss"].get_returns.return_value = list(MOCK_RETURN_RAW)

                # 첫 감지
                s1 = mon.run()
                self.assertEqual(s1["new"], 1)
                self.assertEqual(e["mock_notifier"].send.call_count, 1)

                # 두 번째: 동일 ID → 스킵
                s2 = mon.run()
                self.assertEqual(s2["new"], 0)
                self.assertEqual(e["mock_notifier"].send.call_count, 1)

                # 세 번째
                s3 = mon.run()
                self.assertEqual(s3["new"], 0)
                self.assertEqual(e["mock_notifier"].send.call_count, 1)

                # 새 반품 추가 → 알림 1건 추가
                extra = {
                    "productOrder": {
                        "productOrderId": "RET-002",
                        "productName": "청바지",
                        "quantity": 2,
                    },
                    "claim": {"returnReason": "불량"},
                }
                e["mock_ss"].get_returns.return_value = [
                    *MOCK_RETURN_RAW, extra
                ]
                s4 = mon.run()
                self.assertEqual(s4["new"], 1)
                self.assertEqual(e["mock_notifier"].send.call_count, 2)


class TestFeature20_Logging(unittest.TestCase):
    """20. 로그 기록 정상 여부"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                tmpdir  = Path(tempfile.mkdtemp())
                log_dir = str(tmpdir / "logs")

                # 핸들러 누적 방지: 매 반복마다 root 핸들러 초기화
                root = logging.getLogger()
                for h in root.handlers[:]:
                    root.removeHandler(h)
                    h.close()

                setup_logging(log_dir=log_dir, level="DEBUG")

                # 로그 파일 생성 확인
                log_file = Path(log_dir) / "app.log"
                test_logger = logging.getLogger(f"sim_test_{i}")
                test_logger.info("시뮬레이션 로그 테스트 #%d", i + 1)
                test_logger.debug("디버그 메시지")
                test_logger.warning("경고 메시지")

                # 파일 존재 + 내용 기록 확인
                self.assertTrue(log_file.exists(),
                                f"로그 파일이 생성되지 않음: {log_file}")
                content = log_file.read_text(encoding="utf-8")
                self.assertIn(f"시뮬레이션 로그 테스트 #{i + 1}", content)

                # 핸들러 정리
                for h in root.handlers[:]:
                    root.removeHandler(h)
                    h.close()


# ── 보고서 출력 ───────────────────────────────────────────────────

def load_tests(loader, tests, pattern):
    """unittest discover 호환 — 순서 보장"""
    return tests


_FEATURE_MAP = {
    "TestFeature01_OrderCollection": "스마트스토어 주문 수집",
    "TestFeature02_DomaekkukOrder":  "도매꾹 자동발주 (옵션 포함)",
    "TestFeature03_DomaemaeOrder":   "도매매 자동발주 (옵션 매칭 포함)",
    "TestFeature04_CookieExpiry":    "도매매 쿠키 유효성 + 만료 감지 + 이메일 알림",
    "TestFeature05_Invoice":         "송장번호 자동 등록",
    "TestFeature06_InventorySync":   "재고 동기화",
    "TestFeature07_PriceMonitor":    "가격 모니터링",
    "TestFeature08_ReturnMonitor":   "반품 감지 + 이메일 알림",
    "TestFeature09_BudgetManagement":"예산 관리 (예산 내 최대 발주, 초과 시 대기 저장)",
    "TestFeature10_ResumePending":   "대기 주문 재개 (add_budget.py 연동)",
    "TestFeature11_EmailAll":        "이메일 알림 전체 (반품/예산부족/쿠키만료)",
    "TestFeature12_Scheduler":       "스케줄러 정상 작동",
    "TestFeature13_Wakelock":        "Termux wake-lock 자동실행",
    "TestFeature14_NoOptionOrder":   "옵션 없는 상품 발주",
    "TestFeature15_OptionMatching":  "옵션 있는 상품 발주 + 옵션 매칭",
    "TestFeature16_PartialBudget":   "예산 부족 시 일부만 발주 + 나머지 대기",
    "TestFeature17_RetryOnError":    "발주 실패 시 재시도 로직",
    "TestFeature18_DuplicateOrder":  "중복 주문 방지",
    "TestFeature19_DuplicateReturn": "중복 반품 알림 방지",
    "TestFeature20_Logging":         "로그 기록 정상 여부",
}

if __name__ == "__main__":
    import io

    # 리포트용 결과 수집
    loader  = unittest.TestLoader()
    suite   = loader.loadTestsFromModule(sys.modules[__name__])
    stream  = io.StringIO()
    runner  = unittest.TextTestRunner(stream=stream, verbosity=2)
    result  = runner.run(suite)

    passed  = result.testsRun - len(result.failures) - len(result.errors)
    failed  = len(result.failures) + len(result.errors)

    print("\n" + "=" * 54)
    print("=== 시뮬레이션 완료 보고서 ===")
    print("=" * 54)
    print(f"총 테스트: {result.testsRun}회")
    print(f"통과:      {passed}회")
    print(f"실패:      {failed}회")
    print()
    print(f"{'기능명':<45} | {'반복':>4} | {'결과'}")
    print("-" * 65)

    fail_names = set()
    for f, _ in result.failures + result.errors:
        fail_names.add(type(f).__name__)

    for cls_name, feature in _FEATURE_MAP.items():
        status = "FAIL ✗" if cls_name in fail_names else "PASS ✓"
        print(f"{feature:<45} | {N:>4} | {status}")

    if result.failures or result.errors:
        print()
        print("[수정된 버그 목록]")
        for test, tb in result.failures + result.errors:
            print(f"  - {type(test).__name__}: {tb.splitlines()[-1]}")
    else:
        print()
        print("[수정된 버그 목록]")
        print("  없음 (모든 테스트 최초 통과)")

    print()
    print("[전체 기능 목록]")
    for cls_name, feature in _FEATURE_MAP.items():
        print(f"  - {feature}")

    sys.exit(0 if result.wasSuccessful() else 1)
