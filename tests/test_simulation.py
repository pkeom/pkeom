"""
위탁판매 자동화 시스템 전체 시뮬레이션 테스트

실제 API 호출 없이 mock 데이터로 6개 시나리오 검증:
  1. 가상 주문 수집
  2. 매핑 테이블 기반 발주
  3. 송장번호 등록
  4. 재고 동기화 (품절/재입고)
  5. 가격 변동 모니터링
  6. 이메일 알림
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.database import init_db, get_session
from src.db.models import PriceHistory, SupplierOrder
from src.core.mapping_repository import MappingRepository
from src.core.order_collector import OrderCollector
from src.core.order_placer import OrderPlacer
from src.core.order_repository import OrderRepository
from src.core.price_alert_repository import PriceAlertRepository
from src.core.price_monitor import PriceMonitor
from src.core.inventory_sync import InventorySync
from src.core.invoice_manager import InvoiceManager
from src.notifications.email_notifier import EmailNotifier


# ── 가상 주문 데이터 (네이버 API 중첩 포맷) ───────────────────

MOCK_RAW_ORDERS = [
    {
        "productOrder": {
            "productOrderId": "ORD-001",
            "orderId": "SS-001",
            "productId": "P100",
            "productName": "도매꾹 티셔츠",
            "optionCode": "",
            "quantity": 2,
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

EXTRA_ORDER_NO_MAPPING = {
    "order_id": "ORD-NOMAPPING",
    "ss_order_id": "SS-NOMAPPING",
    "product_id": "GHOST-PRODUCT",
    "product_name": "매핑없음",
    "option_code": "",
    "quantity": 1,
    "buyer_name": "테스터",
    "receiver_name": "테스터",
    "receiver_phone": "010-0000-0000",
    "receiver_address": "서울시",
    "receiver_zipcode": "00000",
    "delivery_memo": "",
    "status": "NEW",
    "collected_at": "2026-05-08T00:00:00",
    "updated_at": "2026-05-08T00:00:00",
}


class SimulationTestBase(unittest.TestCase):
    """임시 경로 + 인메모리 DB를 공유하는 베이스 클래스"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        tmp = Path(self.tmpdir)
        self.db_path = str(tmp / "test.db")
        self.orders_path = tmp / "orders.json"
        self.mappings_path = tmp / "mappings.json"
        self.alerts_path = tmp / "alerts.json"
        self.cache_path = tmp / "stock_cache.json"

        init_db(self.db_path)

        self.order_repo = OrderRepository(self.orders_path)
        self.mapping_repo = MappingRepository(self.mappings_path)
        self.alert_repo = PriceAlertRepository(self.alerts_path)

        # ── 스마트스토어 API mock ──
        self.mock_ss = MagicMock()
        self.mock_ss.get_orders.return_value = MOCK_RAW_ORDERS
        self.mock_ss.dispatch_order.return_value = {"result": "SUCCESS"}
        self.mock_ss.set_product_sale_status.return_value = None

        # ── 도매꾹 API mock ──
        self.mock_domaekkuk = MagicMock()
        self.mock_domaekkuk.get_product.return_value = {
            "title": "도매꾹 티셔츠", "price": 10000, "stock": 50, "seller_id": "s1",
        }
        self.mock_domaekkuk.get_stock.return_value = 50
        self.mock_domaekkuk.place_order.return_value = {"order_no": "DK-ORDER-001"}
        self.mock_domaekkuk.get_order_tracking.return_value = {
            "order_no": "DK-ORDER-001",
            "delivery_company": "CJ대한통운",
            "tracking_number": "1234567890",
        }

        # ── 도매매 client mock ──
        self.mock_domaemae = MagicMock()
        self.mock_domaemae.get_product.return_value = {
            "product_id": "DM-001", "price": 8000, "stock": 5,
        }
        self.mock_domaemae.get_stock.return_value = 5
        self.mock_domaemae.place_order.return_value = "DM-ORDER-001"
        self.mock_domaemae.get_order_tracking.return_value = {
            "order_no": "DM-ORDER-001",
            "delivery_company": "롯데택배",
            "tracking_number": "9876543210",
        }

        self.mock_notifier = MagicMock()

        # 기본 매핑: P100 → domaekkuk, P200/OPT-L → domaemae
        self.mapping_repo.add("P100", "domaekkuk", "56328525")
        self.mapping_repo.add("P200", "domaemae", "DM-001", ss_option_id="OPT-L")


# ══════════════════════════════════════════════════════════════
# 시나리오 1: 주문 수집
# ══════════════════════════════════════════════════════════════

class TestScenario1_OrderCollection(SimulationTestBase):

    def test_collects_two_new_orders(self):
        added = OrderCollector(self.mock_ss, self.order_repo).run()
        self.assertEqual(added, 2)
        self.assertEqual(len(self.order_repo.all()), 2)

    def test_all_orders_start_as_new(self):
        OrderCollector(self.mock_ss, self.order_repo).run()
        for order in self.order_repo.all():
            self.assertEqual(order["status"], "NEW")

    def test_no_duplicate_on_second_run(self):
        collector = OrderCollector(self.mock_ss, self.order_repo)
        self.assertEqual(collector.run(), 2)
        self.assertEqual(collector.run(), 0, "이미 수집된 주문 중복 방지")
        self.assertEqual(len(self.order_repo.all()), 2)

    def test_order_fields_parsed_correctly(self):
        OrderCollector(self.mock_ss, self.order_repo).run()
        order = self.order_repo.find("ORD-001")
        self.assertIsNotNone(order)
        self.assertEqual(order["product_id"], "P100")
        self.assertEqual(order["quantity"], 2)
        self.assertEqual(order["receiver_name"], "홍길동")
        self.assertEqual(order["receiver_phone"], "010-1234-5678")
        self.assertEqual(order["delivery_memo"], "문앞 배송")

    def test_empty_api_response_returns_zero(self):
        self.mock_ss.get_orders.return_value = []
        self.assertEqual(OrderCollector(self.mock_ss, self.order_repo).run(), 0)


# ══════════════════════════════════════════════════════════════
# 시나리오 2: 발주
# ══════════════════════════════════════════════════════════════

class TestScenario2_OrderPlacement(SimulationTestBase):

    def setUp(self):
        super().setUp()
        OrderCollector(self.mock_ss, self.order_repo).run()

    def _make_placer(self, notifier=None):
        return OrderPlacer(
            self.mock_domaekkuk, self.mock_domaemae,
            self.mapping_repo, self.order_repo, notifier,
        )

    def test_both_orders_placed(self):
        stats = self._make_placer().run()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["ordered"], 2)
        self.assertEqual(stats["error"], 0)

    def test_order_status_becomes_ordered(self):
        self._make_placer().run()
        self.assertEqual(self.order_repo.find("ORD-001")["status"], "ORDERED")
        self.assertEqual(self.order_repo.find("ORD-002")["status"], "ORDERED")

    def test_supplier_info_recorded(self):
        self._make_placer().run()
        ord1 = self.order_repo.find("ORD-001")
        self.assertEqual(ord1["supplier"], "domaekkuk")
        self.assertEqual(ord1["supplier_order_no"], "DK-ORDER-001")
        ord2 = self.order_repo.find("ORD-002")
        self.assertEqual(ord2["supplier"], "domaemae")
        self.assertEqual(ord2["supplier_order_no"], "DM-ORDER-001")

    def test_supplier_order_saved_to_db(self):
        self._make_placer().run()
        with get_session() as session:
            self.assertEqual(session.query(SupplierOrder).count(), 2)

    def test_unmapped_order_becomes_error_and_sends_email(self):
        self.order_repo.add_many([EXTRA_ORDER_NO_MAPPING])
        stats = self._make_placer(self.mock_notifier).run()
        self.assertEqual(self.order_repo.find("ORD-NOMAPPING")["status"], "ERROR")
        self.mock_notifier.send.assert_called()
        subject = self.mock_notifier.send.call_args[1]["subject"]
        self.assertIn("발주 실패", subject)

    def test_no_double_placement_on_rerun(self):
        self._make_placer().run()
        stats = self._make_placer().run()
        self.assertEqual(stats["total"], 0, "ORDERED 상태는 재발주 대상이 아님")


# ══════════════════════════════════════════════════════════════
# 시나리오 3: 송장 등록
# ══════════════════════════════════════════════════════════════

class TestScenario3_InvoiceRegistration(SimulationTestBase):

    def setUp(self):
        super().setUp()
        OrderCollector(self.mock_ss, self.order_repo).run()
        OrderPlacer(
            self.mock_domaekkuk, self.mock_domaemae,
            self.mapping_repo, self.order_repo,
        ).run()

    def _make_manager(self, notifier=None):
        return InvoiceManager(
            self.mock_ss, self.mock_domaekkuk, self.mock_domaemae,
            self.order_repo, notifier,
        )

    def test_both_invoices_registered(self):
        stats = self._make_manager().run()
        self.assertEqual(stats["invoiced"], 2)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["error"], 0)

    def test_smartstore_dispatch_called_with_correct_codes(self):
        self._make_manager().run()
        calls = {
            c[0][0]: (c[0][1], c[0][2])
            for c in self.mock_ss.dispatch_order.call_args_list
        }
        # CJ대한통운 → CJGLS
        self.assertEqual(calls["ORD-001"], ("CJGLS", "1234567890"))
        # 롯데택배 → LOTTE
        self.assertEqual(calls["ORD-002"], ("LOTTE", "9876543210"))

    def test_status_updated_to_invoiced(self):
        self._make_manager().run()
        self.assertEqual(self.order_repo.find("ORD-001")["status"], "INVOICED")
        self.assertEqual(self.order_repo.find("ORD-002")["status"], "INVOICED")

    def test_supplier_order_db_updated_to_shipped(self):
        self._make_manager().run()
        with get_session() as session:
            sup = session.query(SupplierOrder).filter_by(
                ss_order_id="ORD-001", supplier="domaekkuk"
            ).first()
            self.assertIsNotNone(sup)
            self.assertEqual(sup.status, "SHIPPED")
            self.assertEqual(sup.tracking_number, "1234567890")

    def test_pending_when_no_tracking_number(self):
        self.mock_domaekkuk.get_order_tracking.return_value = {
            "order_no": "DK-ORDER-001", "delivery_company": "", "tracking_number": "",
        }
        stats = self._make_manager().run()
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["invoiced"], 1)
        self.assertEqual(self.order_repo.find("ORD-001")["status"], "ORDERED")

    def test_tracking_api_error_sends_email(self):
        self.mock_domaekkuk.get_order_tracking.side_effect = Exception("API 오류")
        self._make_manager(self.mock_notifier).run()
        self.mock_notifier.send.assert_called()
        subject = self.mock_notifier.send.call_args_list[0][1]["subject"]
        self.assertIn("송장", subject)


# ══════════════════════════════════════════════════════════════
# 시나리오 4: 재고 동기화
# ══════════════════════════════════════════════════════════════

class TestScenario4_InventorySync(SimulationTestBase):

    def _make_sync(self):
        return InventorySync(
            self.mock_ss, self.mock_domaekkuk, self.mock_domaemae,
            self.mapping_repo, self.mock_notifier, self.cache_path,
        )

    def test_initial_sync_no_error(self):
        stats = self._make_sync().run()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["error"], 0)

    def test_out_of_stock_pauses_sale(self):
        self.mock_domaekkuk.get_stock.return_value = 0
        self.mock_domaemae.get_stock.return_value = 0
        stats = self._make_sync().run()
        self.assertEqual(stats["paused"], 2)
        for c in self.mock_ss.set_product_sale_status.call_args_list:
            self.assertFalse(c[1]["on_sale"])

    def test_restock_resumes_sale(self):
        # 1차: 품절
        self.mock_domaekkuk.get_stock.return_value = 0
        self.mock_domaemae.get_stock.return_value = 0
        sync = self._make_sync()
        sync.run()
        # 2차: 재입고
        self.mock_domaekkuk.get_stock.return_value = 30
        self.mock_domaemae.get_stock.return_value = 10
        self.mock_ss.reset_mock()
        stats = sync.run()
        self.assertEqual(stats["resumed"], 2)
        for c in self.mock_ss.set_product_sale_status.call_args_list:
            self.assertTrue(c[1]["on_sale"])

    def test_unchanged_stock_skips_api_call(self):
        sync = self._make_sync()
        sync.run()
        self.mock_ss.reset_mock()
        stats = sync.run()
        self.assertEqual(stats["unchanged"], 2)
        self.mock_ss.set_product_sale_status.assert_not_called()

    def test_stock_api_error_increments_error_count(self):
        self.mock_domaekkuk.get_stock.side_effect = Exception("재고 API 오류")
        stats = self._make_sync().run()
        self.assertGreater(stats["error"], 0)
        self.mock_notifier.send.assert_called()


# ══════════════════════════════════════════════════════════════
# 시나리오 5: 가격 변동 모니터링
# ══════════════════════════════════════════════════════════════

class TestScenario5_PriceMonitoring(SimulationTestBase):

    def _make_monitor(self):
        return PriceMonitor(
            self.mock_domaekkuk, self.mock_domaemae,
            mapping_repo=self.mapping_repo,
            alert_repo=self.alert_repo,
        )

    def test_first_run_records_initial_prices(self):
        stats = self._make_monitor().run()
        self.assertEqual(stats["changed"], 2)
        self.assertEqual(len(self.alert_repo.all()), 2)

    def test_same_price_no_additional_alert(self):
        m = self._make_monitor()
        m.run()
        count_before = len(self.alert_repo.all())
        m.run()
        self.assertEqual(len(self.alert_repo.all()), count_before)

    def test_price_increase_alert_with_correct_rate(self):
        m = self._make_monitor()
        m.run()  # 기준가 10000원 기록
        self.mock_domaekkuk.get_product.return_value["price"] = 13000
        m.run()
        dk_alerts = [
            a for a in self.alert_repo.all()
            if a["supplier"] == "domaekkuk" and a["new_price"] == 13000
        ]
        self.assertEqual(len(dk_alerts), 1)
        self.assertEqual(dk_alerts[0]["old_price"], 10000)
        self.assertAlmostEqual(dk_alerts[0]["change_rate"], 0.3, places=4)

    def test_price_decrease_negative_change_rate(self):
        m = self._make_monitor()
        m.run()
        self.mock_domaekkuk.get_product.return_value["price"] = 7000
        m.run()
        dk_alerts = [
            a for a in self.alert_repo.all()
            if a["supplier"] == "domaekkuk" and a["new_price"] == 7000
        ]
        self.assertEqual(len(dk_alerts), 1)
        self.assertLess(dk_alerts[0]["change_rate"], 0)

    def test_price_history_saved_to_db(self):
        self._make_monitor().run()
        with get_session() as session:
            count = session.query(PriceHistory).count()
        self.assertEqual(count, 2)

    def test_mark_all_alerts_read(self):
        self._make_monitor().run()
        self.assertGreater(self.alert_repo.count_unread(), 0)
        self.alert_repo.mark_all_read()
        self.assertEqual(self.alert_repo.count_unread(), 0)


# ══════════════════════════════════════════════════════════════
# 시나리오 6: 이메일 알림
# ══════════════════════════════════════════════════════════════

class TestScenario6_EmailNotification(SimulationTestBase):

    def test_placement_error_email_subject(self):
        self.order_repo.add_many([EXTRA_ORDER_NO_MAPPING])
        OrderPlacer(
            self.mock_domaekkuk, self.mock_domaemae,
            self.mapping_repo, self.order_repo, self.mock_notifier,
        ).run()
        self.mock_notifier.send.assert_called_once()
        subject = self.mock_notifier.send.call_args[1]["subject"]
        self.assertIn("발주 실패", subject)

    def test_invoice_error_email_subject(self):
        OrderCollector(self.mock_ss, self.order_repo).run()
        OrderPlacer(
            self.mock_domaekkuk, self.mock_domaemae,
            self.mapping_repo, self.order_repo,
        ).run()
        self.mock_domaekkuk.get_order_tracking.side_effect = Exception("연결 오류")
        InvoiceManager(
            self.mock_ss, self.mock_domaekkuk, self.mock_domaemae,
            self.order_repo, self.mock_notifier,
        ).run()
        self.mock_notifier.send.assert_called()
        subject = self.mock_notifier.send.call_args_list[0][1]["subject"]
        self.assertIn("송장", subject)

    def test_inventory_error_email_sent(self):
        self.mock_domaekkuk.get_stock.side_effect = Exception("재고 오류")
        InventorySync(
            self.mock_ss, self.mock_domaekkuk, self.mock_domaemae,
            self.mapping_repo, self.mock_notifier, self.cache_path,
        ).run()
        self.mock_notifier.send.assert_called()

    def test_email_notifier_smtp_handshake(self):
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            notifier = EmailNotifier(
                smtp_host="smtp.gmail.com",
                smtp_port=587,
                sender="test@gmail.com",
                password="pw",
                recipients=["recv@gmail.com"],
            )
            notifier.send(subject="테스트", body="본문")
            mock_smtp_cls.assert_called_once_with("smtp.gmail.com", 587)
            mock_server.starttls.assert_called_once()
            mock_server.login.assert_called_once_with("test@gmail.com", "pw")
            mock_server.sendmail.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
