"""주문 수집 → 발주 통합 흐름 테스트"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from src.core.order_collector import OrderCollector
from src.core.order_repository import OrderRepository
from src.db.database import init_db


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    init_db(str(tmp_path / "test.db"))


@pytest.fixture
def order_repo(tmp_path):
    return OrderRepository(tmp_path / "orders.json")


def test_collect_new_orders(order_repo):
    mock_api = MagicMock()
    mock_api.get_orders.return_value = [{
        "productOrder": {
            "productOrderId": "ORD001",
            "orderId": "ORD001",
            "productId": "PROD001",
            "optionCode": "",
            "quantity": 2,
        },
        "order": {"ordererName": "홍길동"},
        "shippingAddress": {
            "name": "홍길동",
            "tel1": "010-1234-5678",
            "addressStr": "서울시 강남구",
            "zipCode": "12345",
        },
        "deliveryMemo": "",
    }]

    collector = OrderCollector(mock_api, order_repo)
    added = collector.run()

    assert added == 1
    order = order_repo.find("ORD001")
    assert order is not None
    assert order["quantity"] == 2
    assert order["status"] == "NEW"


def test_duplicate_order_not_collected(order_repo):
    mock_api = MagicMock()
    mock_api.get_orders.return_value = [{
        "productOrder": {
            "productOrderId": "ORD002",
            "orderId": "ORD002",
            "productId": "P",
            "optionCode": "",
            "quantity": 1,
        },
        "order": {},
        "shippingAddress": {},
        "deliveryMemo": "",
    }]

    collector = OrderCollector(mock_api, order_repo)
    collector.run()
    collector.run()  # 두 번 실행해도 중복 저장 안 됨

    assert len(order_repo.all()) == 1
