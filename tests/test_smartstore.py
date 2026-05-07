"""스마트스토어 API 클라이언트 테스트"""
import pytest
from unittest.mock import patch, MagicMock
from src.api.smartstore import SmartstoreAPI


@pytest.fixture
def api():
    return SmartstoreAPI(client_id="test_id", client_secret="test_secret")


@patch("src.api.smartstore.requests.post")
def test_get_token(mock_post, api):
    mock_post.return_value.json.return_value = {"access_token": "tok", "expires_in": 3600}
    mock_post.return_value.raise_for_status = MagicMock()
    token = api._get_token()
    assert token == "tok"


@patch("src.api.smartstore.requests.get")
def test_get_orders(mock_get, api):
    api._token = "tok"
    api._token_expires_at = 9999999999
    mock_get.return_value.json.return_value = {"data": [{"orderId": "123"}]}
    mock_get.return_value.raise_for_status = MagicMock()
    orders = api.get_orders()
    assert len(orders) == 1
    assert orders[0]["orderId"] == "123"
