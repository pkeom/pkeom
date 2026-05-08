"""스마트스토어 API 클라이언트 테스트"""
import pytest
from unittest.mock import patch, MagicMock
from src.api.smartstore import SmartstoreAPI


@pytest.fixture
def api():
    return SmartstoreAPI(client_id="test_id", client_secret="test_secret")


@patch("src.api.smartstore.bcrypt.hashpw", return_value=b"hashed_value")
@patch("src.api.smartstore.requests.post")
def test_get_token(mock_post, mock_hashpw, api):
    """_get_token(): bcrypt 서명 후 토큰 발급 요청"""
    mock_post.return_value.ok = True
    mock_post.return_value.json.return_value = {"access_token": "tok", "expires_in": 3600}
    mock_post.return_value.raise_for_status = MagicMock()

    token = api._get_token()

    assert token == "tok"
    mock_post.assert_called_once()


@patch("src.api.smartstore.requests.post")
@patch("src.api.smartstore.requests.get")
def test_get_orders(mock_get, mock_post, api):
    """get_orders(): last-changed-statuses GET → product-orders/query POST 2단계 흐름"""
    api._token = "tok"
    api._token_expires_at = 9999999999

    # 1단계: 주문 ID 목록 조회
    mock_get.return_value.json.return_value = {
        "lastChangeStatuses": [{"productOrderId": "ORDER-001"}]
    }
    mock_get.return_value.raise_for_status = MagicMock()

    # 2단계: 주문 상세 조회
    mock_post.return_value.json.return_value = {
        "data": [{"productOrder": {"productOrderId": "ORDER-001", "orderId": "123"}}]
    }
    mock_post.return_value.raise_for_status = MagicMock()

    orders = api.get_orders()

    assert len(orders) == 1
    assert orders[0]["productOrder"]["productOrderId"] == "ORDER-001"
