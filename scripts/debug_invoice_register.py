"""운송장 SS 등록 실제 테스트 (1회성 디버그)

ORDERED 상태 주문의 도매처 운송장을 조회하고 SS에 발송처리 등록한다.
dispatch_order 호출 시 PreparedRequest URL/Body와 응답 전체를 출력한다.

실행:
    python scripts/debug_invoice_register.py
"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from src.utils.config_loader import load_config
from src.api.smartstore import SmartstoreAPI
from src.api.domaekkuk import DomaekkukAPI
from src.api.domaemae import DomaemaeClient
from src.core.invoice_manager import InvoiceManager
from src.core.order_repository import OrderRepository

SEP = "=" * 70


class DebugSmartstoreAPI(SmartstoreAPI):
    """dispatch_order에 PreparedRequest 로깅을 추가한 디버그용 서브클래스."""

    def dispatch_order(
        self,
        product_order_id: str,
        delivery_company_code: str,
        tracking_number: str,
    ) -> dict:
        url = f"{self.BASE_URL}/v1/pay-order/seller/product-orders/dispatch"
        body = {
            "dispatchProductOrders": [
                {
                    "productOrderId":      product_order_id,
                    "deliveryMethod":      "DELIVERY",
                    "deliveryCompanyCode": delivery_company_code,
                    "trackingNumber":      tracking_number,
                }
            ]
        }

        # PreparedRequest로 실제 전송 URL·body 캡처
        req = requests.Request("POST", url, headers=self._headers(), json=body)
        prepared = req.prepare()

        print(f"[PreparedRequest] URL : {prepared.url}", flush=True)
        print(f"[PreparedRequest] Body: {prepared.body}", flush=True)

        resp = requests.Session().send(prepared, timeout=10)

        print(f"[Response] HTTP {resp.status_code}", flush=True)
        try:
            resp_body = resp.json()
            print(
                f"[Response] Body:\n{json.dumps(resp_body, ensure_ascii=False, indent=2)}",
                flush=True,
            )
        except Exception:
            print(f"[Response] Body(raw): {resp.text}", flush=True)

        resp.raise_for_status()
        return resp.json()


def main():
    cfg = load_config()

    ss_cfg = {k: v for k, v in cfg["smartstore"].items()
              if k in ("client_id", "client_secret", "account_type")}
    api = DebugSmartstoreAPI(**ss_cfg)

    dk_api = DomaekkukAPI(**cfg["domaekkuk"])

    _dm = cfg.get("domaemae", {})
    dm_cli = DomaemaeClient(
        api_key    = _dm.get("api_key") or cfg["domaekkuk"].get("api_key", ""),
        user_id    = _dm.get("user_id", ""),
        password   = _dm.get("password", ""),
        store_name = cfg.get("store_name", "엘에이(LA)"),
    )

    order_repo = OrderRepository(Path(__file__).parent.parent / "data" / "orders.json")
    manager = InvoiceManager(
        ss_api     = api,
        domaekkuk  = dk_api,
        domaemae   = dm_cli,
        order_repo = order_repo,
    )

    ordered = order_repo.find_by_status("ORDERED")
    print(f"\nORDERED 주문 {len(ordered)}건 대상\n")

    if not ordered:
        print("처리할 주문 없음.")
        return

    for order in ordered:
        order_id = order["order_id"]
        print(SEP)
        print(
            f"[주문] order_id={order_id}"
            f"  supplier={order.get('supplier', '')}"
            f"  supplier_order_no={order.get('supplier_order_no', '')}"
        )
        result = manager._sync_one(order)
        print(f"[결과] {result}", flush=True)

    print(SEP)
    print(f"완료: {len(ordered)}건 처리")


if __name__ == "__main__":
    main()
