"""주문 수집 → 발주 통합 흐름 테스트"""
import pytest
from unittest.mock import MagicMock, patch
from src.core.order_collector import OrderCollector
from src.db.database import init_db, get_session
from src.db.models import Order


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)


def test_collect_new_orders():
    mock_api = MagicMock()
    mock_api.get_orders.return_value = [{
        "orderId": "ORD001",
        "productId": "PROD001",
        "optionId": "",
        "quantity": 2,
        "buyerName": "홍길동",
        "receiverName": "홍길동",
        "receiverTel": "010-1234-5678",
        "receiverAddr": "서울시 강남구",
        "receiverZipcode": "12345",
        "deliveryMemo": "",
    }]

    collector = OrderCollector(mock_api)
    collector.run()

    with get_session() as session:
        order = session.query(Order).filter_by(ss_order_id="ORD001").first()
        assert order is not None
        assert order.quantity == 2


def test_duplicate_order_not_collected():
    mock_api = MagicMock()
    mock_api.get_orders.return_value = [{"orderId": "ORD002", "productId": "P", "quantity": 1,
                                          "buyerName": "", "receiverName": "", "receiverTel": "",
                                          "receiverAddr": "", "receiverZipcode": "", "optionId": "", "deliveryMemo": ""}]

    collector = OrderCollector(mock_api)
    collector.run()
    collector.run()  # 두 번 실행해도 중복 저장 안 됨

    with get_session() as session:
        count = session.query(Order).filter_by(ss_order_id="ORD002").count()
        assert count == 1
