"""운송장 SS 등록 실제 테스트 (1회성 디버그)

실행:
    # 특정 주문 강제 dispatch (status 무시)
    python scripts/debug_invoice_register.py 2026060895134531

    # ORDERED 전체 처리 (기존 동작)
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
    """dispatch_order에 요청/응답 전체 로깅을 추가한 디버그용 서브클래스."""

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

        headers = self._headers()

        # 요청 전체 출력 (인증 헤더 마스킹)
        masked_headers = {
            k: ("[MASKED]" if k.lower() == "authorization" else v)
            for k, v in headers.items()
        }
        print(f"[Request] POST {url}", flush=True)
        print(f"[Request] Headers: {masked_headers}", flush=True)
        print(
            f"[Request] Body:\n{json.dumps(body, ensure_ascii=False, indent=2)}",
            flush=True,
        )

        req = requests.Request("POST", url, headers=headers, json=body)
        prepared = req.prepare()
        resp = requests.Session().send(prepared, timeout=10)

        # 응답 전체 출력 (raise 전에 반드시 출력)
        print(f"[Response] HTTP {resp.status_code}", flush=True)
        print(f"[Response] Body(raw):\n{resp.text}", flush=True)

        resp.raise_for_status()
        return resp.json()


class DebugInvoiceManager(InvoiceManager):
    """_sync_one에 도매처 운송장값 로깅을 추가한 디버그용 서브클래스."""

    def _sync_one(self, order: dict) -> str:
        supplier          = order.get("supplier", "")
        supplier_order_no = order.get("supplier_order_no", "")

        if supplier and supplier_order_no:
            try:
                client   = self.clients[supplier]
                tracking = client.get_order_tracking(supplier_order_no)
                print(
                    f"[도매처 운송장] 택배사={tracking.get('delivery_company', '')}  "
                    f"송장번호={tracking.get('tracking_number', '')}",
                    flush=True,
                )
            except Exception as e:
                print(f"[도매처 운송장] 조회 실패: {e}", flush=True)

        return super()._sync_one(order)


def _build_manager(cfg) -> tuple[DebugInvoiceManager, OrderRepository]:
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
    manager = DebugInvoiceManager(
        ss_api     = api,
        domaekkuk  = dk_api,
        domaemae   = dm_cli,
        order_repo = order_repo,
    )
    return manager, order_repo


def run_single(order_id: str):
    """특정 order_id 강제 dispatch (status 무시)."""
    cfg = load_config()
    manager, order_repo = _build_manager(cfg)

    order = next(
        (o for o in order_repo.all() if o["order_id"] == order_id),
        None,
    )

    if order is None:
        print(f"orders.json에서 order_id={order_id} 를 찾을 수 없습니다.")
        return

    print(f"[현재 status] {order.get('status', '(없음)')}  order_id={order_id}")
    print(SEP)
    print(
        f"[주문] order_id={order_id}"
        f"  supplier={order.get('supplier', '')}"
        f"  supplier_order_no={order.get('supplier_order_no', '')}"
    )

    result = manager._sync_one(order)
    print(f"[결과] {result}", flush=True)

    # dispatch 후 실제로 status가 어떻게 바뀌었는지 확인
    updated = next(
        (o for o in order_repo.all() if o["order_id"] == order_id),
        None,
    )
    if updated:
        print(f"[dispatch 후 status] {updated.get('status', '(없음)')}", flush=True)


def run_all_ordered():
    """ORDERED 상태 전체 처리 (기존 동작)."""
    cfg = load_config()
    manager, order_repo = _build_manager(cfg)

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


def main():
    if len(sys.argv) >= 2:
        run_single(sys.argv[1])
    else:
        run_all_ordered()


if __name__ == "__main__":
    main()
