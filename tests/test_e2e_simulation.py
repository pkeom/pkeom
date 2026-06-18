"""
E2E 시뮬레이션 테스트 — 스마트스토어 드롭셔핑 자동화 전체 시스템

모든 외부 API를 Mock으로 대체하여 전체 흐름·분기를 시뮬레이션한다.
100회 반복 실행하며, 실패 시 상세 진단 정보를 출력한다.

실행:
    pytest tests/test_e2e_simulation.py -v          # pytest 개별 실행
    python tests/test_e2e_simulation.py             # 100회 반복 스탠드얼론
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# ── 프로젝트 루트를 sys.path에 추가 ──────────────────────────────────
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from src.db.database import init_db, get_session
from src.db.models import SupplierOrder

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("e2e")


# ═══════════════════════════════════════════════════════════════════════════════
# §1  유틸리티
# ═══════════════════════════════════════════════════════════════════════════════

def _business_days_ago(n: int) -> str:
    """cancel_monitor._business_days_since(result) 가 정확히 n을 반환하는 ISO 타임스탬프.

    _business_days_since 는 start+1일 부터 today(포함) 사이의 영업일을 셈.
    오늘이 주말이면 today 자체가 제외되므로 역산해서 맞춤.
    """
    today = date.today()
    bdays_seen = 0
    d = today
    # 오늘부터 거슬러 올라가 n번째 영업일을 찾음
    while bdays_seen < n:
        if d.weekday() < 5:
            bdays_seen += 1
        if bdays_seen < n:
            d -= timedelta(days=1)
    # d = n번째 영업일. _business_days_since 의 start = d - 1 일 이면 count == n
    start = d - timedelta(days=1)
    return datetime(start.year, start.month, start.day, tzinfo=timezone.utc).isoformat()


def _make_raw_order(order_id: str, product_id: str = "SS_PROD_001",
                    product_name: str = "테스트상품", quantity: int = 1) -> dict:
    return {
        "productOrder": {
            "productOrderId": order_id,
            "orderId": order_id,
            "productId": product_id,
            "productName": product_name,
            "optionCode": "",
            "quantity": quantity,
        },
        "order": {"ordererName": "홍길동"},
        "shippingAddress": {
            "name": "홍길동",
            "tel1": "010-1234-5678",
            "addressStr": "서울시 강남구 테헤란로 123",
            "zipCode": "06234",
        },
        "deliveryMemo": "",
    }


def _make_cancel_raw(order_id: str, reason: str = "변심") -> dict:
    return {
        "productOrder": {
            "productOrderId": order_id,
            "orderId":        f"ORD_{order_id}",
            "productName":    "테스트상품",
            "quantity":       2,
            "invoiceNumber":  "INVNUM_001",
        },
        "order": {
            "ordererName": "홍길동",
            "ordererTel":  "010-1111-2222",
        },
        "shippingAddress": {
            "name":       "이순신",
            "tel1":       "010-3333-4444",
            "addressStr": "서울시 강남구 테헤란로 123",
            "zipCode":    "06234",
        },
        "claim": {"cancelReason": reason},
    }


def _make_return_raw(order_id: str, reason: str = "불량") -> dict:
    return {
        "productOrder": {
            "productOrderId":     order_id,
            "orderId":            f"ORD_{order_id}",
            "productId":          "SS_PROD_001",
            "productName":        "테스트상품",
            "optionCode":         "OPT_001",
            "optionName":         "빨간색",
            "quantity":           2,
            "unitPrice":          15000,
            "totalPaymentAmount": 30000,
            "invoiceNumber":      "1234567890",
            "deliveryCompany":    "CJ대한통운",
        },
        "order": {
            "ordererName": "홍길동",
            "ordererTel":  "010-1111-2222",
        },
        "shippingAddress": {
            "name":          "이순신",
            "tel1":          "010-3333-4444",
            "addressStr":    "서울시 강남구 테헤란로 123 456동 789호",
            "roadAddress":   "서울시 강남구 테헤란로 123",
            "detailAddress": "456동 789호",
            "zipCode":       "06234",
        },
        "deliveryMemo": "문 앞에 놔주세요",
        "claim": {
            "returnReason":           reason,
            "returnReasonType":       "DEFECT",
            "claimStatus":            "RETURN_REQUEST",
            "claimRequestDate":       "2026-05-31T10:00:00",
            "returnDeliveryCompany":  "롯데택배",
            "returnTrackingNumber":   "9876543210",
            "refundExpectedDate":     "2026-06-03",
            "deliveryFeePayType":     "SELLER",
            "claimPrice": {
                "refundPayAmount": 28000,
            },
        },
    }


def _order_record(order_id, product_id="SS_PROD_001", product_name="테스트상품",
                  status="NEW", quantity=1):
    now = datetime.now().isoformat()
    return {
        "order_id": order_id, "ss_order_id": order_id,
        "product_id": product_id, "product_name": product_name,
        "option_code": "", "quantity": quantity,
        "buyer_name": "홍길동", "receiver_name": "홍길동",
        "receiver_phone": "010-1234-5678",
        "receiver_address": "서울시 강남구", "receiver_zipcode": "06234",
        "delivery_memo": "", "status": status,
        "collected_at": now, "updated_at": now,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# §2  Mock 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class MockEmailNotifier:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, subject: str, body: str, **kwargs):
        self.sent.append({"subject": subject, "body": body})

    def last(self) -> dict | None:
        return self.sent[-1] if self.sent else None

    def subjects(self) -> list[str]:
        return [e["subject"] for e in self.sent]

    def clear(self):
        self.sent.clear()

    def has_subject_containing(self, keyword: str) -> bool:
        return any(keyword in s for s in self.subjects())

    def has_body_containing(self, keyword: str) -> bool:
        return any(keyword in e["body"] for e in self.sent)


class MockSmartstoreAPI:
    BASE_URL = "https://mock.api.commerce.naver.com/external"

    def __init__(self):
        self.orders_to_return: list[dict] = []
        self.cancellations_to_return: list[dict] = []
        self.returns_to_return: list[dict] = []

        self.dispatch_fail = False
        self.confirm_fail = False
        self.sale_status_fail = False
        self.approve_cancel_fail = False
        self.image_upload_fail = False

        self.dispatched: list[dict] = []
        self.confirmed: list[str] = []
        self.sale_status_changes: list[dict] = []
        self.approved_cancels: list[str] = []
        self.image_upload_calls = 0

        self.category_result: tuple[str, str] = ("", "")
        self.kc_cert_status: tuple[bool, int] = (False, 0)

    def _headers(self):
        return {"Authorization": "Bearer mock_token"}

    def get_orders(self, status="PAYED", days=1):
        return self.orders_to_return

    def dispatch_order(self, product_order_id, delivery_company_code, tracking_number):
        if self.dispatch_fail:
            raise RuntimeError("SS dispatch 실패 (Mock)")
        self.dispatched.append({
            "order_id": product_order_id,
            "company": delivery_company_code,
            "tracking": tracking_number,
        })
        return {"success": True}

    def set_product_sale_status(self, product_id, on_sale):
        if self.sale_status_fail:
            raise RuntimeError("SS 판매상태 변경 실패 (Mock)")
        self.sale_status_changes.append({"product_id": product_id, "on_sale": on_sale})
        return {"success": True}

    def get_product(self, product_id):
        return {"id": product_id, "saleStatus": "ON_SALE"}

    def get_products(self, size=100):
        return []

    def confirm_orders(self, product_order_ids: list[str]) -> dict:
        if self.confirm_fail:
            return {"confirmed": [], "failed": product_order_ids}
        BATCH = 30
        confirmed = []
        for i in range(0, len(product_order_ids), BATCH):
            confirmed.extend(product_order_ids[i: i + BATCH])
        self.confirmed.extend(confirmed)
        return {"confirmed": confirmed, "failed": []}

    def get_cancellations(self, hours=1):
        # 1시간: 최근 취소 / 24시간: 레이스컨디션 감지용 (별도 목록)
        if hours <= 1:
            return self.cancellations_to_return
        return getattr(self, "cancellations_24h", self.cancellations_to_return)

    def approve_cancel(self, product_order_id):
        if self.approve_cancel_fail:
            raise RuntimeError("SS 취소승인 실패 (Mock)")
        self.approved_cancels.append(product_order_id)
        return {"success": True}

    def get_returns(self, hours=1):
        return self.returns_to_return

    def find_leaf_category(self, keyword, whole_cat=""):
        return self.category_result

    def get_kc_cert_status(self, category_id, cert_type_hint=""):
        return self.kc_cert_status

    def is_kc_cert_required(self, category_id):
        return self.kc_cert_status[0]

    def upload_image_data(self, data, content_type, filename):
        self.image_upload_calls += 1
        if self.image_upload_fail:
            raise RuntimeError("이미지 업로드 실패 (Mock)")
        return f"https://pstatic.net/mock/{filename}"

    def upload_image(self, image_url):
        return self.upload_image_data(b"fake", "image/jpeg", "img.jpg")


class MockDomaekkukAPI:
    def __init__(self):
        self.products: dict[str, dict] = {}
        self.order_results: dict[str, str] = {}
        self.tracking: dict[str, dict] = {}
        self.cancel_results: dict[str, str] = {}
        self.order_fail = False
        self.placed: list[dict] = []
        self.cancel_requests: list[str] = []

    def _def_product(self, pid):
        return {"title": f"도매꾹상품_{pid}", "price": 10000, "stock": 100, "seller_id": "s1"}

    def get_product(self, product_no):
        return self.products.get(str(product_no), self._def_product(product_no))

    def get_stock(self, product_no):
        return self.get_product(str(product_no)).get("stock", 0)

    def search_products(self, keyword, market="dome", page=1, size=20):
        return {"total": 0, "items": []}

    def get_options(self, product_no):
        return []

    def place_order(self, product_no, quantity, shipping_info, *,
                    supplier_option_id="", dry_run=False):
        if dry_run:
            return {"order_no": f"[DRY_RUN] {product_no}"}
        if self.order_fail:
            raise RuntimeError("도매꾹 발주 실패 (Mock)")
        order_no = self.order_results.get(str(product_no), f"DK_{product_no}")
        self.placed.append({
            "product_no": product_no, "qty": quantity,
            "order_no": order_no, "supplier_option_id": supplier_option_id,
        })
        return {"order_no": order_no}

    def cancel_order(self, order_no):
        self.cancel_requests.append(order_no)
        return {"success": True}

    def get_cancel_result(self, order_no):
        return self.cancel_results.get(str(order_no), "PENDING")

    def get_order_tracking(self, order_no):
        return self.tracking.get(str(order_no), {
            "order_no": order_no, "delivery_company": "", "tracking_number": "",
        })


class MockDomaemaeClient:
    def __init__(self):
        self.products: dict[str, dict] = {}
        self.order_results: dict[str, str] = {}
        self.tracking: dict[str, dict] = {}
        self.cancel_results: dict[str, str] = {}
        self.order_fail = False
        self.placed: list[dict] = []
        self.cancel_requests: list[str] = []

        self.api_key = "mock_api_key"
        self._sid = "mock_sid"
        self._sid_renew_date = datetime.now() + timedelta(hours=24)
        self._login_time = datetime.now()
        self._login_keep_seconds = 30 * 24 * 3600

    def _ensure_session(self):
        pass

    def _def_product(self, pid):
        return {"product_id": pid, "title": f"도매매상품_{pid}", "price": 8000, "stock": 50}

    def get_product(self, product_id):
        return self.products.get(str(product_id), self._def_product(product_id))

    def get_stock(self, product_id):
        return self.get_product(str(product_id)).get("stock", 0)

    def get_options(self, product_id):
        return []

    def place_order(self, product_id, quantity, shipping_info, *,
                    option_id="", option_name="", dry_run=False):
        if dry_run:
            return f"[DRY_RUN] {product_id}"
        if self.order_fail:
            raise RuntimeError("도매매 발주 실패 (Mock)")
        order_no = self.order_results.get(str(product_id), f"DM_{product_id}")
        self.placed.append({
            "product_id": product_id, "qty": quantity,
            "order_no": order_no,
            "option_id": option_id, "option_name": option_name,
        })
        return order_no

    def cancel_order(self, order_no):
        self.cancel_requests.append(order_no)
        return {"success": True}

    def get_cancel_result(self, order_no):
        return self.cancel_results.get(str(order_no), "PENDING")

    def get_order_tracking(self, order_no):
        return self.tracking.get(str(order_no), {
            "order_no": order_no, "delivery_company": "", "tracking_number": "",
        })


# ═══════════════════════════════════════════════════════════════════════════════
# §3  테스트 환경
# ═══════════════════════════════════════════════════════════════════════════════

@contextlib.contextmanager
def _env():
    """격리된 임시 작업 디렉토리 + DB + Mock 세트."""
    import src.db.database as _db_mod

    old_dir = os.getcwd()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        os.makedirs(os.path.join(tmpdir, "data"), exist_ok=True)
        os.chdir(tmpdir)
        init_db(os.path.join(tmpdir, "data", "orders.db"))

        ss  = MockSmartstoreAPI()
        dk  = MockDomaekkukAPI()
        dm  = MockDomaemaeClient()
        ntf = MockEmailNotifier()

        try:
            yield ss, dk, dm, ntf, Path(tmpdir)
        finally:
            if _db_mod._engine is not None:
                _db_mod._engine.dispose()
                _db_mod._engine = None
                _db_mod._SessionFactory = None
            os.chdir(old_dir)


def _build_placer(ss, dk, dm, ntf, budget_amount=0):
    from src.core.order_placer import OrderPlacer
    from src.core.budget_manager import BudgetManager
    from src.core.pending_order_repository import PendingOrderRepository
    from src.core.stock_pending_repository import StockPendingRepository

    budget  = BudgetManager(initial_balance=budget_amount) if budget_amount > 0 else None
    pending = PendingOrderRepository() if budget else None
    stock_p = StockPendingRepository()

    placer = OrderPlacer(dk, dm, notifier=ntf, budget=budget, pending=pending,
                         ss_api=ss, stock_pending=stock_p)
    return placer, budget, pending, stock_p


def _insert_sup_order(ss_order_id, supplier, supplier_order_no,
                      status="ORDERED", tracking_number="", delivery_company=""):
    with get_session() as session:
        session.add(SupplierOrder(
            ss_order_id=ss_order_id, supplier=supplier,
            supplier_product_id="PROD_001", supplier_order_no=supplier_order_no,
            quantity=1, status=status,
            tracking_number=tracking_number, delivery_company=delivery_company,
        ))


def _add_mapping(mapping_repo, ss_product_id="SS_PROD_001",
                 supplier="domaekkuk", supplier_product_id="DK_PROD_001"):
    mapping_repo.add(ss_product_id=ss_product_id, supplier=supplier,
                     supplier_url_or_id=supplier_product_id)


def _write_confirm_log(order_ids: list[str]):
    data = {"confirms": [
        {"order_id": oid, "confirmed_at": datetime.now(timezone.utc).isoformat()}
        for oid in order_ids
    ]}
    Path("data/confirm_log.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# §4  시나리오 함수
# ═══════════════════════════════════════════════════════════════════════════════

# ── A: 주문 수집 ─────────────────────────────────────────────────────────────

def scenario_A1_order_collection():
    """A1: 정상 주문 수집 → orders.json 저장 + 중복 방지"""
    from src.core.order_collector import OrderCollector
    from src.core.order_repository import OrderRepository

    with _env() as (ss, dk, dm, ntf, _):
        ss.orders_to_return = [
            _make_raw_order("ORD_001", "SS_PROD_001"),
            _make_raw_order("ORD_002", "SS_PROD_002"),
        ]
        repo = OrderRepository()
        collector = OrderCollector(ss, repo=repo)
        added = collector.run()

        assert added == 2
        orders = repo.all()
        ids = {o["order_id"] for o in orders}
        assert {"ORD_001", "ORD_002"} == ids
        assert all(o["status"] == "NEW" for o in orders)

        added2 = collector.run()
        assert added2 == 0, "중복 수집이 발생했습니다"


# ── B: 자동 발주 + 발주확인 ──────────────────────────────────────────────────

def scenario_B1_cancel_request_blocks_order():
    """B1: CANCEL_REQUEST 주문 → 발주 차단"""
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)
        repo.add_many([_order_record("ORD_CANCEL_001")])
        ss.cancellations_to_return = [_make_cancel_raw("ORD_CANCEL_001")]

        placer, *_ = _build_placer(ss, dk, dm, ntf)
        placer._mappings = mapping_repo
        placer._orders = repo

        stats = placer.run()
        assert stats["cancelled"] == 1
        assert stats["ordered"] == 0
        assert repo.find("ORD_CANCEL_001")["status"] == "CANCELLED"


def scenario_B2_budget_insufficient():
    """B2: 예산 부족 → 대기 전환 + 이메일"""
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)
        dk.products["DK_PROD_001"] = {"title": "고가상품", "price": 50000, "stock": 10, "seller_id": "s1"}
        repo.add_many([_order_record("ORD_BUDGET_001")])

        placer, *_ = _build_placer(ss, dk, dm, ntf, budget_amount=1000)
        placer._mappings = mapping_repo
        placer._orders = repo

        stats = placer.run()
        assert stats["deferred"] == 1
        assert stats["ordered"] == 0
        assert repo.find("ORD_BUDGET_001")["status"] == "PENDING"
        assert ntf.has_subject_containing("예산 부족")


def scenario_B3_domaekkuk_order_success():
    """B3: 도매꾹 발주 성공 + SS 발주확인"""
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo, supplier="domaekkuk")
        dk.order_results["DK_PROD_001"] = "DK_ORDER_001"
        repo.add_many([_order_record("ORD_DK_001")])

        placer, *_ = _build_placer(ss, dk, dm, ntf, budget_amount=500000)
        placer._mappings = mapping_repo
        placer._orders = repo

        stats = placer.run()
        assert stats["ordered"] == 1
        assert stats["ss_confirmed"] == 1
        assert repo.find("ORD_DK_001")["status"] == "ORDERED"
        assert "ORD_DK_001" in ss.confirmed

        with get_session() as s:
            so = s.query(SupplierOrder).filter_by(ss_order_id="ORD_DK_001").first()
            assert so is not None
            assert so.supplier_order_no == "DK_ORDER_001"


def scenario_B4_domaemae_order_success():
    """B4: 도매매 발주 성공 + SS 발주확인"""
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo, supplier="domaemae", supplier_product_id="DM_PROD_001")
        dm.order_results["DM_PROD_001"] = "DM_ORDER_001"
        repo.add_many([_order_record("ORD_DM_001")])

        placer, *_ = _build_placer(ss, dk, dm, ntf, budget_amount=500000)
        placer._mappings = mapping_repo
        placer._orders = repo

        stats = placer.run()
        assert stats["ordered"] == 1
        assert "ORD_DM_001" in ss.confirmed

        with get_session() as s:
            so = s.query(SupplierOrder).filter_by(ss_order_id="ORD_DM_001").first()
            assert so.supplier_order_no == "DM_ORDER_001"


def scenario_B5_order_api_failure():
    """B5: 발주 API 실패 → ERROR + 이메일"""
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)
        dk.order_fail = True
        repo.add_many([_order_record("ORD_FAIL_001")])

        placer, *_ = _build_placer(ss, dk, dm, ntf)
        placer._mappings = mapping_repo
        placer._orders = repo

        stats = placer.run()
        assert stats["error"] == 1
        assert repo.find("ORD_FAIL_001")["status"] == "ERROR"
        assert ntf.has_subject_containing("발주 실패")


def scenario_B6_ss_confirm_failure():
    """B6: SS 발주확인 실패 → 이메일"""
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)
        ss.confirm_fail = True
        repo.add_many([_order_record("ORD_CONF_001")])

        placer, *_ = _build_placer(ss, dk, dm, ntf, budget_amount=500000)
        placer._mappings = mapping_repo
        placer._orders = repo

        stats = placer.run()
        assert stats["ordered"] == 1
        assert stats["ss_confirmed"] == 0
        assert stats["ss_confirm_failed"] == 1
        assert ntf.has_subject_containing("발주확인 실패")


def scenario_B7_no_mapping_error():
    """B7: 매핑 없음 → ERROR + 이메일"""
    from src.core.order_repository import OrderRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        repo.add_many([_order_record("ORD_NOMAP_001", product_id="NO_MATCH_PROD")])

        placer, *_ = _build_placer(ss, dk, dm, ntf)
        placer._orders = repo

        stats = placer.run()
        assert stats["error"] == 1
        assert repo.find("ORD_NOMAP_001")["status"] == "ERROR"
        assert ntf.has_subject_containing("발주 실패")


def scenario_B8_confirm_batch_31():
    """B8: 31건 발주 → 30+1 배치 발주확인"""
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)
        repo.add_many([_order_record(f"ORD_BATCH_{i:03d}") for i in range(31)])

        placer, *_ = _build_placer(ss, dk, dm, ntf, budget_amount=10_000_000)
        placer._mappings = mapping_repo
        placer._orders = repo

        stats = placer.run()
        assert stats["ordered"] == 31
        assert stats["ss_confirmed"] == 31
        assert len(ss.confirmed) == 31


# ── C: 송장 동기화 ────────────────────────────────────────────────────────────

def _ordered_record(order_id, supplier, supplier_order_no):
    r = _order_record(order_id, status="ORDERED")
    r["supplier"] = supplier
    r["supplier_order_no"] = supplier_order_no
    return r


def scenario_C1_domaekkuk_invoice():
    """C1: 도매꾹 송장 → SS 등록"""
    from src.core.invoice_manager import InvoiceManager
    from src.core.order_repository import OrderRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        repo.add_many([_ordered_record("ORD_INV_DK_001", "domaekkuk", "DK_INV_001")])
        _insert_sup_order("ORD_INV_DK_001", "domaekkuk", "DK_INV_001")
        dk.tracking["DK_INV_001"] = {
            "order_no": "DK_INV_001", "delivery_company": "CJ대한통운", "tracking_number": "123456789",
        }

        InvoiceManager(ss, dk, dm, order_repo=repo, notifier=ntf).run()

        assert len(ss.dispatched) == 1
        assert ss.dispatched[0]["tracking"] == "123456789"
        assert repo.find("ORD_INV_DK_001")["status"] == "INVOICED"


def scenario_C2_domaemae_invoice():
    """C2: 도매매 송장 → SS 등록"""
    from src.core.invoice_manager import InvoiceManager
    from src.core.order_repository import OrderRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        repo.add_many([_ordered_record("ORD_INV_DM_001", "domaemae", "DM_INV_001")])
        _insert_sup_order("ORD_INV_DM_001", "domaemae", "DM_INV_001")
        dm.tracking["DM_INV_001"] = {
            "order_no": "DM_INV_001", "delivery_company": "롯데택배", "tracking_number": "987654321",
        }

        InvoiceManager(ss, dk, dm, order_repo=repo, notifier=ntf).run()

        assert ss.dispatched[0]["company"] == "LOTTE"
        assert ss.dispatched[0]["tracking"] == "987654321"


def scenario_C3_invoice_pending():
    """C3: 미발송 → pending, 상태 유지"""
    from src.core.invoice_manager import InvoiceManager
    from src.core.order_repository import OrderRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        repo.add_many([_ordered_record("ORD_PEND_001", "domaekkuk", "DK_PEND_001")])
        _insert_sup_order("ORD_PEND_001", "domaekkuk", "DK_PEND_001")
        # tracking 없음 → 미발송

        stats = InvoiceManager(ss, dk, dm, order_repo=repo, notifier=ntf).run()

        assert stats["pending"] == 1
        assert stats["invoiced"] == 0
        assert repo.find("ORD_PEND_001")["status"] == "ORDERED"


def scenario_C4_ss_dispatch_failure():
    """C4: SS dispatch 실패 → ERROR + 이메일"""
    from src.core.invoice_manager import InvoiceManager
    from src.core.order_repository import OrderRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        repo.add_many([_ordered_record("ORD_DISP_FAIL", "domaekkuk", "DK_DISP_FAIL")])
        _insert_sup_order("ORD_DISP_FAIL", "domaekkuk", "DK_DISP_FAIL")
        dk.tracking["DK_DISP_FAIL"] = {
            "order_no": "DK_DISP_FAIL", "delivery_company": "CJ대한통운", "tracking_number": "111",
        }
        ss.dispatch_fail = True

        stats = InvoiceManager(ss, dk, dm, order_repo=repo, notifier=ntf).run()

        assert stats["error"] == 1
        assert repo.find("ORD_DISP_FAIL")["status"] == "ERROR"
        assert ntf.has_subject_containing("송장 등록 실패")


# ── D: 재고 동기화 ────────────────────────────────────────────────────────────

def scenario_D1_out_of_stock():
    """D1: 품절 감지 → SS 판매중지 + 이메일"""
    from src.core.inventory_sync import InventorySync
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)
        dk.products["DK_PROD_001"] = {"title": "테스트", "price": 10000, "stock": 0, "seller_id": "s1"}

        stats = InventorySync(ss, dk, dm, mapping_repo=mapping_repo, notifier=ntf).run()

        assert stats["paused"] == 1
        assert ss.sale_status_changes[0]["on_sale"] is False
        assert ntf.has_subject_containing("품절")


def scenario_D2_restock_resumes():
    """D2: 재입고 → 판매재개 + 재고부족 대기 주문 재발주"""
    from src.core.inventory_sync import InventorySync
    from src.core.mapping_repository import MappingRepository
    from src.core.stock_pending_repository import StockPendingRepository
    from src.core.order_repository import OrderRepository
    from src.core.order_placer import OrderPlacer

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)

        cache_path = Path("data/stock_cache.json")
        cache_path.write_text(json.dumps({
            "status": {"domaekkuk:DK_PROD_001": False}, "low_stock_alerted_at": {}
        }), encoding="utf-8")

        order_repo = OrderRepository()
        order_repo.add_many([_order_record("ORD_RESTOCK_001", status="STOCK_PENDING")])
        stock_p = StockPendingRepository()
        stock_p.add({
            "order_id": "ORD_RESTOCK_001", "supplier": "domaekkuk",
            "supplier_product_id": "DK_PROD_001", "ss_product_id": "SS_PROD_001",
            "order": order_repo.find("ORD_RESTOCK_001"),
        })

        dk.products["DK_PROD_001"] = {"title": "테스트", "price": 10000, "stock": 100, "seller_id": "s1"}
        dk.order_results["DK_PROD_001"] = "DK_RESTOCK_ORDER"

        placer = OrderPlacer(dk, dm, ss_api=ss, stock_pending=stock_p, notifier=ntf)
        placer._orders = order_repo
        placer._mappings = mapping_repo

        stats = InventorySync(ss, dk, dm, mapping_repo=mapping_repo,
                              notifier=ntf, cache_path=cache_path, order_placer=placer).run()

        assert stats["resumed"] == 1
        assert any(c["on_sale"] is True for c in ss.sale_status_changes)
        assert order_repo.find("ORD_RESTOCK_001")["status"] == "ORDERED"


def scenario_D3_sale_status_api_fail():
    """D3: SS 판매상태 API 실패 → error + 이메일"""
    from src.core.inventory_sync import InventorySync
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)
        dk.products["DK_PROD_001"] = {"title": "테스트", "price": 10000, "stock": 0, "seller_id": "s1"}
        ss.sale_status_fail = True

        stats = InventorySync(ss, dk, dm, mapping_repo=mapping_repo, notifier=ntf).run()

        assert stats["error"] == 1
        assert ntf.has_subject_containing("재고 동기화 오류")


def scenario_D4_low_stock_cooldown():
    """D4: 재고 부족 알림 + 60분 쿨다운"""
    from src.core.inventory_sync import InventorySync, LOW_STOCK_THRESHOLD
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)
        dk.products["DK_PROD_001"] = {
            "title": "테스트", "price": 10000,
            "stock": LOW_STOCK_THRESHOLD - 1, "seller_id": "s1",
        }

        sync = InventorySync(ss, dk, dm, mapping_repo=mapping_repo, notifier=ntf)
        sync.run()
        assert ntf.has_subject_containing("재고부족")

        ntf.clear()
        sync.run()
        assert not ntf.has_subject_containing("재고부족"), "쿨다운 내 이메일 재발송"


# ── E: 가격 모니터링 ──────────────────────────────────────────────────────────

def scenario_E1_price_change_no_email():
    """E1: 가격 변동 → price_alerts.json 기록, 이메일 미발송"""
    from src.core.price_monitor import PriceMonitor
    from src.core.mapping_repository import MappingRepository
    from src.core.price_alert_repository import PriceAlertRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)
        alert_repo = PriceAlertRepository()
        dk.products["DK_PROD_001"] = {"title": "테스트", "price": 10000, "stock": 100, "seller_id": "s1"}

        monitor = PriceMonitor(dk, dm, ntf, mapping_repo=mapping_repo, alert_repo=alert_repo)
        monitor.run()  # 1차: None→10000 (changed=1로 기록됨)
        ntf.clear()    # 1차 이메일 무시

        # 동일 가격 → unchanged, 이메일 없음
        stats_same = monitor.run()
        assert stats_same["unchanged"] == 1
        assert not ntf.has_subject_containing("가격")

        # 가격 변동
        dk.products["DK_PROD_001"]["price"] = 12000
        ntf.clear()
        stats2 = monitor.run()

        assert stats2["changed"] == 1
        # 가격 변동 시 이메일 미발송 (설계 의도)
        assert not ntf.has_subject_containing("가격변동")
        assert not any("12000" in s for s in ntf.subjects()), "가격 변동 이메일 발송됨"

        alerts = alert_repo.all()
        latest = next(a for a in alerts if a["new_price"] == 12000)
        assert latest["old_price"] == 10000


def scenario_E2_price_api_error():
    """E2: 가격 조회 오류 → 이메일"""
    from src.core.price_monitor import PriceMonitor
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)

        orig = dk.get_product
        dk.get_product = lambda pid: (_ for _ in ()).throw(RuntimeError("API 오류"))

        stats = PriceMonitor(dk, dm, ntf, mapping_repo=mapping_repo).run()

        assert stats["error"] == 1
        assert ntf.has_subject_containing("가격 모니터링 오류")
        dk.get_product = orig


# ── F: 반품 감지 ──────────────────────────────────────────────────────────────

def scenario_F1_return_request():
    """F1: RETURN_REQUEST → 이메일(전체 필드) + returns.json 저장 + 중복 방지"""
    from src.core.return_monitor import ReturnMonitor

    with _env() as (ss, dk, dm, ntf, _):
        ss.returns_to_return = [
            _make_return_raw("ORD_RET_001", "불량"),
            _make_return_raw("ORD_RET_002", "변심"),
        ]
        monitor = ReturnMonitor(ss, ntf)
        stats = monitor.run()

        assert stats["new"] == 2
        assert len(ntf.sent) == 2
        assert all("반품" in s["subject"] for s in ntf.sent)

        # returns.json 저장 확인
        data = json.loads(Path("data/returns.json").read_text(encoding="utf-8"))
        assert len(data["returns"]) == 2

        # 새로 추가된 필드 검증
        entry = data["returns"][0]
        assert entry["product_order_id"] == "ORD_RET_001"
        assert entry["ss_order_id"] == "ORD_ORD_RET_001"
        assert entry["product_id"] == "SS_PROD_001"
        assert entry["option_code"] == "OPT_001"
        assert entry["option_name"] == "빨간색"
        assert entry["quantity"] == 2
        assert entry["unit_price"] == 15000
        assert entry["total_payment_amount"] == 30000
        assert entry["buyer_name"] == "홍길동"
        assert entry["buyer_phone"] == "010-1111-2222"
        assert entry["receiver_name"] == "이순신"
        assert entry["receiver_phone"] == "010-3333-4444"
        assert entry["receiver_address"] == "서울시 강남구 테헤란로 123 456동 789호"
        assert entry["receiver_road_address"] == "서울시 강남구 테헤란로 123"
        assert entry["receiver_detail_address"] == "456동 789호"
        assert entry["receiver_zipcode"] == "06234"
        assert entry["delivery_memo"] == "문 앞에 놔주세요"
        assert entry["invoice_number"] == "1234567890"
        assert entry["delivery_company"] == "CJ대한통운"
        assert entry["return_reason"] == "불량"
        assert entry["return_reason_type"] == "DEFECT"
        assert entry["claim_status"] == "RETURN_REQUEST"
        assert entry["return_delivery_company"] == "롯데택배"
        assert entry["return_tracking_number"] == "9876543210"
        assert entry["refund_amount"] == 28000
        assert entry["refund_expected_date"] == "2026-06-03"
        assert entry["return_delivery_fee_payer"] == "SELLER"

        # 이메일 본문에 주요 필드 포함 여부
        body = ntf.sent[0]["body"]
        assert "이순신" in body          # 수령인명
        assert "홍길동" in body          # 구매자명
        assert "1234567890" in body      # 기존 송장번호
        assert "28,000" in body          # 환불금액
        assert "롯데택배" in body         # 반품 택배사
        assert "9876543210" in body      # 반품 송장번호

        # 중복 방지
        ntf.clear()
        assert monitor.run()["new"] == 0


# ── G: 취소 처리 ──────────────────────────────────────────────────────────────

def _build_cancel_mon(ss, dk, dm, ntf):
    from src.core.cancel_monitor import CancelMonitor
    return CancelMonitor(ss, ntf, dk_api=dk, dm_cli=dm)


def _load_cancel_data():
    return json.loads(Path("data/cancellations.json").read_text(encoding="utf-8"))


def _find_cancel_entry(order_id):
    data = _load_cancel_data()
    return next((e for e in data["cancellations"] if e["product_order_id"] == order_id), None)


def scenario_G1_ss_auto_no_db_order():
    """G1: DB 발주 없음 → SS_AUTO"""
    with _env() as (ss, dk, dm, ntf, _):
        ss.cancellations_to_return = [_make_cancel_raw("ORD_G1")]
        _build_cancel_mon(ss, dk, dm, ntf).run()

        entry = _find_cancel_entry("ORD_G1")
        assert entry is not None
        assert entry["cancel_state"] == "SS_AUTO"


def scenario_G2_deny_failed_no_order_no():
    """G2: 발주번호 없음 → DENY_FAILED"""
    with _env() as (ss, dk, dm, ntf, _):
        _insert_sup_order("ORD_G2", "domaekkuk", "", status="ORDERED")
        ss.cancellations_to_return = [_make_cancel_raw("ORD_G2")]
        _build_cancel_mon(ss, dk, dm, ntf).run()

        entry = _find_cancel_entry("ORD_G2")
        assert entry["cancel_state"] == "DENY_FAILED"
        assert len(ntf.sent) >= 1


def scenario_G3_deny_failed_api_error():
    """G3: setOrdDeny API 실패 → DENY_FAILED + 이메일"""
    with _env() as (ss, dk, dm, ntf, _):
        _insert_sup_order("ORD_G3", "domaekkuk", "DK_G3_ORDER", status="ORDERED")

        orig = dk.cancel_order
        dk.cancel_order = lambda ono: (_ for _ in ()).throw(RuntimeError("취소 API 실패"))

        ss.cancellations_to_return = [_make_cancel_raw("ORD_G3")]
        _build_cancel_mon(ss, dk, dm, ntf).run()
        dk.cancel_order = orig

        entry = _find_cancel_entry("ORD_G3")
        assert entry["cancel_state"] == "DENY_FAILED"
        assert ntf.has_subject_containing("수동 처리") or ntf.has_subject_containing("긴급")


def scenario_G4_approved():
    """G4: setOrdDeny → 즉시 폴링 → APPROVED → SS 취소승인 (같은 실행 사이클)"""
    with _env() as (ss, dk, dm, ntf, _):
        _insert_sup_order("ORD_G4", "domaekkuk", "DK_G4_ORDER", status="ORDERED")
        dk.cancel_results["DK_G4_ORDER"] = "APPROVED"
        ss.cancellations_to_return = [_make_cancel_raw("ORD_G4")]
        mon = _build_cancel_mon(ss, dk, dm, ntf)

        # cancel_monitor: 감지 → DENY_SENT → 즉시 폴링 → APPROVED (같은 run 사이클)
        mon.run()

        entry = _find_cancel_entry("ORD_G4")
        assert entry["cancel_state"] == "APPROVED"
        assert "ORD_G4" in ss.approved_cancels


def scenario_G5_approved_ss_fail_manual_required():
    """G5: 도매처 승인 + SS approve_cancel 실패 → MANUAL_REQUIRED (같은 사이클)"""
    with _env() as (ss, dk, dm, ntf, _):
        _insert_sup_order("ORD_G5", "domaekkuk", "DK_G5_ORDER", status="ORDERED")
        dk.cancel_results["DK_G5_ORDER"] = "APPROVED"
        ss.approve_cancel_fail = True
        ss.cancellations_to_return = [_make_cancel_raw("ORD_G5")]
        mon = _build_cancel_mon(ss, dk, dm, ntf)

        # 감지 → DENY_SENT → 즉시 폴링 → APPROVED → SS approve 실패 → MANUAL_REQUIRED
        mon.run()

        assert _find_cancel_entry("ORD_G5")["cancel_state"] == "MANUAL_REQUIRED"
        assert ntf.has_subject_containing("수동 처리") or ntf.has_subject_containing("긴급")


def scenario_G6_rejected_with_tracking():
    """G6: 거부 + 송장 있음 → 즉시 CANCEL_REJECT (같은 사이클)"""
    with _env() as (ss, dk, dm, ntf, _):
        _insert_sup_order("ORD_G6", "domaekkuk", "DK_G6_ORDER",
                          status="ORDERED", tracking_number="TRK_G6", delivery_company="CJ대한통운")
        dk.cancel_results["DK_G6_ORDER"] = "REJECTED"
        ss.cancellations_to_return = [_make_cancel_raw("ORD_G6")]
        mon = _build_cancel_mon(ss, dk, dm, ntf)

        # 감지 → DENY_SENT → 즉시 폴링 → REJECTED + 송장 있음 → dispatch → REJECTED
        mon.run()

        entry = _find_cancel_entry("ORD_G6")
        assert entry["cancel_state"] in ("REJECTED", "SHIPPED_REJECT")
        assert len(ss.dispatched) >= 1


def scenario_G7_rejected_wait_ship():
    """G7: 거부 + 미발송 → REJECTED_WAIT_SHIP → 송장 후 dispatch"""
    with _env() as (ss, dk, dm, ntf, _):
        _insert_sup_order("ORD_G7", "domaekkuk", "DK_G7_ORDER", status="ORDERED")
        dk.cancel_results["DK_G7_ORDER"] = "REJECTED"
        ss.cancellations_to_return = [_make_cancel_raw("ORD_G7")]
        mon = _build_cancel_mon(ss, dk, dm, ntf)

        mon.run()
        ss.cancellations_to_return = []
        mon.run()
        assert _find_cancel_entry("ORD_G7")["cancel_state"] == "REJECTED_WAIT_SHIP"

        # 송장 DB 업데이트
        with get_session() as s:
            row = s.query(SupplierOrder).filter_by(ss_order_id="ORD_G7").first()
            if row:
                row.tracking_number = "TRK_G7"
                row.delivery_company = "CJ대한통운"

        mon.run()
        assert _find_cancel_entry("ORD_G7")["cancel_state"] == "REJECTED"
        assert len(ss.dispatched) >= 1


def scenario_G8_urgent_3day():
    """G8: PENDING → 3영업일 → URGENT_3DAY"""
    with _env() as (ss, dk, dm, ntf, _):
        _insert_sup_order("ORD_G8", "domaekkuk", "DK_G8_ORDER", status="ORDERED")
        dk.cancel_results["DK_G8_ORDER"] = "PENDING"
        ss.cancellations_to_return = [_make_cancel_raw("ORD_G8")]
        mon = _build_cancel_mon(ss, dk, dm, ntf)

        mon.run()
        ss.cancellations_to_return = []

        data = _load_cancel_data()
        for e in data["cancellations"]:
            if e["product_order_id"] == "ORD_G8":
                e["deny_sent_at"] = _business_days_ago(3)
        Path("data/cancellations.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        ntf.clear()
        mon.run()

        entry = _find_cancel_entry("ORD_G8")
        assert entry["cancel_state"] == "URGENT_3DAY"
        assert ntf.has_subject_containing("긴급")


def scenario_G9_manual_4day():
    """G9: PENDING → 4영업일 → MANUAL_4DAY"""
    with _env() as (ss, dk, dm, ntf, _):
        _insert_sup_order("ORD_G9", "domaekkuk", "DK_G9_ORDER", status="ORDERED")
        dk.cancel_results["DK_G9_ORDER"] = "PENDING"
        ss.cancellations_to_return = [_make_cancel_raw("ORD_G9")]
        mon = _build_cancel_mon(ss, dk, dm, ntf)

        mon.run()
        ss.cancellations_to_return = []

        data = _load_cancel_data()
        for e in data["cancellations"]:
            if e["product_order_id"] == "ORD_G9":
                e["deny_sent_at"] = _business_days_ago(4)
        Path("data/cancellations.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        ntf.clear()
        mon.run()

        entry = _find_cancel_entry("ORD_G9")
        assert entry["cancel_state"] == "MANUAL_4DAY"
        assert len(ntf.sent) >= 1


def scenario_G10_shipped_reject():
    """G10: DB SHIPPED + 송장 → SHIPPED_REJECT"""
    with _env() as (ss, dk, dm, ntf, _):
        _insert_sup_order("ORD_G10", "domaekkuk", "DK_G10_ORDER",
                          status="SHIPPED", tracking_number="TRK_G10", delivery_company="CJ대한통운")
        ss.cancellations_to_return = [_make_cancel_raw("ORD_G10")]
        _build_cancel_mon(ss, dk, dm, ntf).run()

        entry = _find_cancel_entry("ORD_G10")
        assert entry["cancel_state"] == "SHIPPED_REJECT"
        assert len(ss.dispatched) >= 1


def scenario_G11_race_condition():
    """G11: 발주확인(1시간 내) + 취소요청(24시간 내, 1시간 초과) → RACE_CONDITION

    _detect_race_conditions 작동 조건:
    - confirm_log.json에 최근 1시간 내 발주확인 기록 있음
    - get_cancellations(hours=24)에 해당 주문 있음
    - get_cancellations(hours=1)에는 없음 (1시간 이전 취소 → 현재 사이클 미감지)
    - cancellations.json에도 없음 (이전 run에서 처리 안 됨)
    """
    with _env() as (ss, dk, dm, ntf, _):
        _write_confirm_log(["ORD_RACE_001"])

        # 1시간 이내 취소: 없음 (취소가 1-24시간 전에 발생)
        ss.cancellations_to_return = []
        # 24시간 이내 취소: 포함
        ss.cancellations_24h = [_make_cancel_raw("ORD_RACE_001")]

        _build_cancel_mon(ss, dk, dm, ntf).run()

        data = _load_cancel_data()
        race = [e for e in data["cancellations"] if e.get("cancel_state") == "RACE_CONDITION"]
        assert len(race) >= 1, "RACE_CONDITION 항목이 감지되지 않았습니다"
        assert ntf.has_subject_containing("발주확인") or ntf.has_subject_containing("예외")


def scenario_G12_dispatch_fail_manual_required():
    """G12: SS dispatch 실패 → MANUAL_REQUIRED (같은 사이클)"""
    with _env() as (ss, dk, dm, ntf, _):
        _insert_sup_order("ORD_G12", "domaekkuk", "DK_G12_ORDER",
                          status="ORDERED", tracking_number="TRK_G12", delivery_company="CJ대한통운")
        dk.cancel_results["DK_G12_ORDER"] = "REJECTED"
        ss.dispatch_fail = True
        ss.cancellations_to_return = [_make_cancel_raw("ORD_G12")]
        mon = _build_cancel_mon(ss, dk, dm, ntf)

        # 감지 → DENY_SENT → 즉시 폴링 → REJECTED + 송장 → dispatch 실패 → MANUAL_REQUIRED
        mon.run()

        entry = _find_cancel_entry("ORD_G12")
        assert entry["cancel_state"] == "MANUAL_REQUIRED"
        assert ntf.has_subject_containing("수동 처리")


# ── H: 예산 관리 ──────────────────────────────────────────────────────────────

def scenario_H1_balance_check():
    """H1: 잔액 조회 + 차감 정상 작동"""
    from src.core.budget_manager import BudgetManager

    with _env() as (ss, dk, dm, ntf, _):
        bm = BudgetManager(initial_balance=300_000)
        assert bm.get_balance() == 300_000
        bm.deduct(50_000, "테스트 차감", "ORD_001")
        assert bm.get_balance() == 250_000


def scenario_H2_charge_resumes_pending():
    """H2: 예산 충전 → 대기 주문 자동 재개"""
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository
    from src.core.budget_manager import BudgetManager
    from src.core.pending_order_repository import PendingOrderRepository
    from src.core.order_placer import OrderPlacer

    with _env() as (ss, dk, dm, ntf, _):
        order_repo = OrderRepository()
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)

        order_repo.add_many([_order_record("ORD_PEND_H2", status="PENDING")])
        bm = BudgetManager(initial_balance=0)
        pending = PendingOrderRepository()
        pending.add_many([{"order": order_repo.find("ORD_PEND_H2"), "estimated_cost": 13_000}])

        placer = OrderPlacer(dk, dm, notifier=ntf, budget=bm, pending=pending)
        placer._mappings = mapping_repo
        placer._orders = order_repo

        dk.order_results["DK_PROD_001"] = "DK_RESUME_001"
        bm.charge(100_000, "테스트 충전")
        stats = placer.resume_pending()

        assert stats["ordered"] == 1
        assert order_repo.find("ORD_PEND_H2")["status"] == "ORDERED"


# ── I: 상품 자동 등록 ────────────────────────────────────────────────────────

class _MockResp:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = json.dumps(json_data)
        self.content = self.text.encode()

    def json(self):
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise Exception(f"HTTP {self.status_code}")


def _product_info(supplier="domaekkuk", product_id="DK_REG_001"):
    return {
        "supplier": supplier, "product_id": product_id,
        "supplier_url": f"https://domeggook.com/{product_id}",
        "title": "테스트 등록 상품", "supply_price": 10000, "stock": 100,
        "min_qty": 1, "origin": "국내산", "model": "M01", "brand": "B01",
        "manufacturer": "제조사", "kc_cert_no": "", "kc_cert_type": "", "kc_cert_agency": "",
        "main_image": "https://domeggook.com/img/main.jpg",
        "sub_images": ["https://domeggook.com/img/sub1.jpg"],
        "detail_images": [], "detail_html": "",
        "options": [], "category_id": "CAT_001",
        "category_name": "디지털 > 전자제품", "category_code": "17_05",
        "naver_category_name": "디지털 > 전자제품",
    }


_FAKE_IMG = (b"\xff\xd8\xff\xe0" + b"\x00" * 100, "image/jpeg", "main.jpg")


def scenario_I1_domaekkuk_register():
    """I1: 도매꾹 URL → SS 등록 성공 → mappings.json 저장"""
    from src.core.product_register import register_product
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        info = _product_info("domaekkuk")

        with patch("src.core.product_register.fetch_product_info", return_value=info), \
             patch("src.core.product_register._make_image_session", return_value=Mock()), \
             patch("src.core.product_register._fetch_image_bytes", return_value=_FAKE_IMG), \
             patch("src.core.product_register.requests.post",
                   return_value=_MockResp({"originProductNo": "SS_NEW_001"})):
            result = register_product(
                url="https://domeggook.com/DK_REG_001",
                selling_price=15000, smartstore_api=ss,
                supplier_client=dm, settings={},
                mapping_repo=mapping_repo, category_id="CAT_001",
            )

        assert result["success"] is True, result.get("error")
        assert result["product_id"] == "SS_NEW_001"
        assert len(mapping_repo.all()) == 1
        assert mapping_repo.all()[0]["supplier"] == "domaekkuk"


def scenario_I2_domaemae_register():
    """I2: 도매매 URL → SS 등록 성공"""
    from src.core.product_register import register_product
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        info = _product_info("domaemae", "DM_REG_001")

        with patch("src.core.product_register.fetch_product_info", return_value=info), \
             patch("src.core.product_register._make_image_session", return_value=Mock()), \
             patch("src.core.product_register._fetch_image_bytes", return_value=_FAKE_IMG), \
             patch("src.core.product_register.requests.post",
                   return_value=_MockResp({"originProductNo": "SS_NEW_002"})):
            result = register_product(
                url="https://domeme.domeggook.com/s/DM_REG_001",
                selling_price=20000, smartstore_api=ss,
                supplier_client=dm, settings={},
                mapping_repo=mapping_repo, category_id="CAT_002",
            )

        assert result["success"] is True
        assert mapping_repo.all()[0]["supplier"] == "domaemae"


def scenario_I3_kc_cert_required_blocks():
    """I3: KC 필수 + 인증 없음 → 등록 중단"""
    from src.core.product_register import register_product
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        ss.kc_cert_status = (True, 12345)
        info = _product_info()
        info["kc_cert_no"] = ""
        info["kc_cert_agency"] = ""

        with patch("src.core.product_register.fetch_product_info", return_value=info):
            result = register_product(
                url="https://domeggook.com/DK_KC_001",
                selling_price=15000, smartstore_api=ss,
                supplier_client=dm, settings={},
                mapping_repo=mapping_repo, category_id="CAT_KC",
            )

        assert result["success"] is False
        assert len(mapping_repo.all()) == 0


def scenario_I4_main_image_fail():
    """I4: 대표이미지 업로드 실패 → 등록 중단"""
    from src.core.product_register import register_product
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        ss.image_upload_fail = True
        info = _product_info()

        with patch("src.core.product_register.fetch_product_info", return_value=info), \
             patch("src.core.product_register._make_image_session", return_value=Mock()), \
             patch("src.core.product_register._fetch_image_bytes",
                   return_value=(b"fake", "image/jpeg", "img.jpg")):
            result = register_product(
                url="https://domeggook.com/DK_IMG_FAIL",
                selling_price=15000, smartstore_api=ss,
                supplier_client=dm, settings={},
                mapping_repo=mapping_repo, category_id="CAT_001",
            )

        assert result["success"] is False
        assert "이미지" in result.get("error", "")


def scenario_I5_sub_image_fail_continues():
    """I5: 서브이미지 실패 → 경고 후 등록 계속"""
    from src.core.product_register import register_product
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        info = _product_info()
        info["sub_images"] = ["https://domeggook.com/img/sub1.jpg"]

        call_n = [0]
        def mock_upload(data, ct, fname):
            call_n[0] += 1
            if call_n[0] == 1:
                return "https://pstatic.net/main.jpg"
            raise RuntimeError("서브 이미지 실패")
        ss.upload_image_data = mock_upload

        with patch("src.core.product_register.fetch_product_info", return_value=info), \
             patch("src.core.product_register._make_image_session", return_value=Mock()), \
             patch("src.core.product_register._fetch_image_bytes",
                   return_value=(b"fake", "image/jpeg", "img.jpg")), \
             patch("src.core.product_register.requests.post",
                   return_value=_MockResp({"originProductNo": "SS_SUB_OK"})):
            result = register_product(
                url="https://domeggook.com/DK_SUB_FAIL",
                selling_price=15000, smartstore_api=ss,
                supplier_client=dm, settings={},
                mapping_repo=mapping_repo, category_id="CAT_001",
            )

        assert result["success"] is True


def scenario_I6_category_cache_hit():
    """I6: 카테고리 캐시 히트 → find_leaf_category 호출 없음"""
    from src.core.product_register import register_product, _CATEGORY_CACHE_PATH
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        _CATEGORY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CATEGORY_CACHE_PATH.write_text(json.dumps({
            "디지털 > 전자제품": {"category_id": "CACHED_001", "category_name": "디지털 > 전자제품"}
        }, ensure_ascii=False), encoding="utf-8")

        info = _product_info()
        info["category_id"] = ""

        calls = [0]
        orig = ss.find_leaf_category
        def counting(keyword, whole_cat=""):
            calls[0] += 1
            return orig(keyword, whole_cat)
        ss.find_leaf_category = counting

        with patch("src.core.product_register.fetch_product_info", return_value=info), \
             patch("src.core.product_register._make_image_session", return_value=Mock()), \
             patch("src.core.product_register._fetch_image_bytes", return_value=_FAKE_IMG), \
             patch("src.core.product_register.requests.post",
                   return_value=_MockResp({"originProductNo": "SS_CACHE_001"})):
            result = register_product(
                url="https://domeggook.com/DK_CACHE_001",
                selling_price=15000, smartstore_api=ss,
                supplier_client=dm, settings={},
                mapping_repo=mapping_repo, category_id="",
            )

        assert calls[0] == 0, "캐시 히트 시 API 호출 불필요"
        assert result["success"] is True


def scenario_I7_category_cache_miss():
    """I7: 캐시 미스 → SS API 매칭 후 캐시 저장"""
    from src.core.product_register import register_product, _CATEGORY_CACHE_PATH
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        if _CATEGORY_CACHE_PATH.exists():
            _CATEGORY_CACHE_PATH.unlink()

        ss.category_result = ("AUTO_CAT_001", "자동매칭카테고리")
        info = _product_info()
        info["category_name"] = "생활/건강"
        info["naver_category_name"] = ""
        info["category_id"] = ""

        with patch("src.core.product_register.fetch_product_info", return_value=info), \
             patch("src.core.product_register._make_image_session", return_value=Mock()), \
             patch("src.core.product_register._fetch_image_bytes", return_value=_FAKE_IMG), \
             patch("src.core.product_register.requests.post",
                   return_value=_MockResp({"originProductNo": "SS_MISS_001"})):
            result = register_product(
                url="https://domeggook.com/DK_MISS_001",
                selling_price=15000, smartstore_api=ss,
                supplier_client=dm, settings={},
                mapping_repo=mapping_repo, category_id="",
            )

        assert result["success"] is True
        assert result["category_id"] == "AUTO_CAT_001"
        assert _CATEGORY_CACHE_PATH.exists()


def scenario_I8_no_title_fails():
    """I8: 상품명 없음 → 등록 불가"""
    from src.core.product_register import register_product
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        mapping_repo = MappingRepository()
        info = _product_info()
        info["title"] = ""

        with patch("src.core.product_register.fetch_product_info", return_value=info):
            result = register_product(
                url="https://domeggook.com/DK_NOTITLE",
                selling_price=15000, smartstore_api=ss,
                supplier_client=dm, settings={}, mapping_repo=mapping_repo,
            )

        assert result["success"] is False
        assert "상품명" in result.get("error", "")


# ── J: 이메일 내용 검증 ──────────────────────────────────────────────────────

def scenario_J1_order_fail_email_fields():
    """J1: 발주 실패 이메일에 주문ID·상품명·구매자명·사유·필요조치 포함"""
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)
        dk.order_fail = True
        repo.add_many([_order_record("ORD_EMAIL_001", product_name="이메일검증상품")])

        placer, *_ = _build_placer(ss, dk, dm, ntf)
        placer._mappings = mapping_repo
        placer._orders = repo
        placer.run()

        assert len(ntf.sent) >= 1
        email = ntf.last()
        body = email["body"]
        assert "ORD_EMAIL_001" in body,    "주문ID 누락"
        assert "이메일검증상품" in body,     "상품명 누락"
        assert "홍길동" in body,            "구매자명 누락"
        assert "발주 실패" in email["subject"]
        assert "필요한 조치" in body,        "필요한 조치 섹션 누락"
        assert "현재 상태" in body,          "현재 상태 섹션 누락"


def scenario_J2_cancel_email_fields():
    """J2: 취소 이메일에 SS 주문번호·도매처·수령인·수량·송장번호 포함

    DENY_SENT → 즉시 폴링 → APPROVED → 이메일 (같은 run 사이클).
    _make_cancel_raw에 order·shippingAddress 포함되어 있어 수령인 추출 가능.
    """
    with _env() as (ss, dk, dm, ntf, _):
        _insert_sup_order("ORD_CEL_EMAIL", "domaekkuk", "DK_EMAIL_ORDER", status="ORDERED")
        dk.cancel_results["DK_EMAIL_ORDER"] = "APPROVED"
        ss.cancellations_to_return = [_make_cancel_raw("ORD_CEL_EMAIL")]
        mon = _build_cancel_mon(ss, dk, dm, ntf)

        mon.run()  # DENY_SENT → 즉시 폴링 → APPROVED → 이메일 발송

        assert len(ntf.sent) >= 1, "이메일이 발송되지 않았습니다"
        approved_email = next(
            (e for e in ntf.sent if "취소완료" in e["subject"] or "취소 승인" in e["subject"]),
            ntf.last()
        )
        body = approved_email["body"]
        assert "ORD_CEL_EMAIL" in body,  "SS 주문번호 누락"
        assert "domaekkuk" in body,      "도매처 누락"
        assert "이순신" in body,          "수령인명 누락"
        assert "2개" in body,            "수량 누락"
        assert "INVNUM_001" in body,     "기존 송장번호 누락"
        assert "필요한 조치" in body,     "필요한 조치 섹션 누락"


def scenario_J3_budget_email_amount():
    """J3: 예산 부족 이메일에 금액 정보 포함"""
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository

    with _env() as (ss, dk, dm, ntf, _):
        repo = OrderRepository()
        mapping_repo = MappingRepository()
        _add_mapping(mapping_repo)
        dk.products["DK_PROD_001"] = {"title": "고가", "price": 100000, "stock": 10, "seller_id": "s1"}
        repo.add_many([_order_record("ORD_BUDGET_EMAIL")])

        placer, *_ = _build_placer(ss, dk, dm, ntf, budget_amount=500)
        placer._mappings = mapping_repo
        placer._orders = repo
        placer.run()

        assert ntf.has_subject_containing("예산 부족")
        email = next(e for e in ntf.sent if "예산 부족" in e["subject"])
        assert "원" in email["body"]


# ═══════════════════════════════════════════════════════════════════════════════
# §5  시나리오 목록
# ═══════════════════════════════════════════════════════════════════════════════

ALL_SCENARIOS: list[tuple[str, object]] = [
    # A: 주문 수집
    ("A1 주문수집 정상", scenario_A1_order_collection),
    # B: 자동 발주
    ("B1 취소요청→발주차단", scenario_B1_cancel_request_blocks_order),
    ("B2 예산부족→대기", scenario_B2_budget_insufficient),
    ("B3 도매꾹 발주성공", scenario_B3_domaekkuk_order_success),
    ("B4 도매매 발주성공", scenario_B4_domaemae_order_success),
    ("B5 발주API실패", scenario_B5_order_api_failure),
    ("B6 발주확인실패", scenario_B6_ss_confirm_failure),
    ("B7 매핑없음→ERROR", scenario_B7_no_mapping_error),
    ("B8 31건 배치분리", scenario_B8_confirm_batch_31),
    # C: 송장 동기화
    ("C1 도매꾹 송장등록", scenario_C1_domaekkuk_invoice),
    ("C2 도매매 송장등록", scenario_C2_domaemae_invoice),
    ("C3 미발송→pending", scenario_C3_invoice_pending),
    ("C4 SS dispatch실패", scenario_C4_ss_dispatch_failure),
    # D: 재고 동기화
    ("D1 품절→판매중지", scenario_D1_out_of_stock),
    ("D2 재입고→재발주", scenario_D2_restock_resumes),
    ("D3 SS API실패", scenario_D3_sale_status_api_fail),
    ("D4 재고부족+쿨다운", scenario_D4_low_stock_cooldown),
    # E: 가격 모니터링
    ("E1 가격변동→알림(이메일X)", scenario_E1_price_change_no_email),
    ("E2 가격조회오류→이메일", scenario_E2_price_api_error),
    # F: 반품
    ("F1 반품신청→이메일", scenario_F1_return_request),
    # G: 취소 처리 전 분기
    ("G1 DB없음→SS_AUTO", scenario_G1_ss_auto_no_db_order),
    ("G2 발주번호없음→DENY_FAILED", scenario_G2_deny_failed_no_order_no),
    ("G3 setOrdDeny실패→DENY_FAILED", scenario_G3_deny_failed_api_error),
    ("G4 DENY_SENT→APPROVED", scenario_G4_approved),
    ("G5 APPROVED→SS실패→MANUAL_REQUIRED", scenario_G5_approved_ss_fail_manual_required),
    ("G6 REJECTED+송장→CANCEL_REJECT", scenario_G6_rejected_with_tracking),
    ("G7 REJECTED+미발송→WAIT_SHIP", scenario_G7_rejected_wait_ship),
    ("G8 PENDING→3일→URGENT_3DAY", scenario_G8_urgent_3day),
    ("G9 PENDING→4일→MANUAL_4DAY", scenario_G9_manual_4day),
    ("G10 SHIPPED→SHIPPED_REJECT", scenario_G10_shipped_reject),
    ("G11 발주확인후취소→RACE_CONDITION", scenario_G11_race_condition),
    ("G12 SS dispatch실패→MANUAL_REQUIRED", scenario_G12_dispatch_fail_manual_required),
    # H: 예산
    ("H1 잔액조회", scenario_H1_balance_check),
    ("H2 충전→대기재개", scenario_H2_charge_resumes_pending),
    # I: 상품 등록
    ("I1 도매꾹 등록성공", scenario_I1_domaekkuk_register),
    ("I2 도매매 등록성공", scenario_I2_domaemae_register),
    ("I3 KC필수+없음→차단", scenario_I3_kc_cert_required_blocks),
    ("I4 대표이미지실패→차단", scenario_I4_main_image_fail),
    ("I5 서브이미지실패→계속", scenario_I5_sub_image_fail_continues),
    ("I6 카테고리캐시히트", scenario_I6_category_cache_hit),
    ("I7 카테고리캐시미스→API", scenario_I7_category_cache_miss),
    ("I8 상품명없음→실패", scenario_I8_no_title_fails),
    # J: 이메일 내용
    ("J1 발주실패이메일필드", scenario_J1_order_fail_email_fields),
    ("J2 취소이메일필드", scenario_J2_cancel_email_fields),
    ("J3 예산이메일금액", scenario_J3_budget_email_amount),
]


# ═══════════════════════════════════════════════════════════════════════════════
# §6  100회 반복 실행기
# ═══════════════════════════════════════════════════════════════════════════════

def run_all_once() -> list[dict]:
    """모든 시나리오 1회 실행. 실패 목록 반환."""
    failures = []
    for name, fn in ALL_SCENARIOS:
        try:
            fn()
        except Exception as exc:
            failures.append({
                "scenario": name,
                "exception": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            })
    return failures


def run_100_iterations(verbose: bool = True) -> tuple[bool, list[dict]]:
    all_failures = []
    for i in range(1, 101):
        failures = run_all_once()
        if failures:
            for f in failures:
                all_failures.append({"iteration": i, **f})
            if verbose:
                names = ", ".join(f["scenario"] for f in failures)
                print(f"  ✗ 반복 {i:>3}/100  실패: {names}")
        else:
            if verbose and i % 10 == 0:
                print(f"  ✓ 반복 {i:>3}/100  전부 통과")
    return len(all_failures) == 0, all_failures


# ═══════════════════════════════════════════════════════════════════════════════
# §7  pytest 함수
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name,fn", ALL_SCENARIOS, ids=[s[0] for s in ALL_SCENARIOS])
def test_scenario(name, fn):
    """모든 시나리오 개별 pytest 테스트."""
    fn()


def test_100_iterations_all_pass():
    """100회 반복 — 1건도 실패 없어야 통과."""
    passed, failures = run_100_iterations(verbose=False)
    if not passed:
        unique = sorted({f["scenario"] for f in failures})
        samples = []
        for f in failures[:5]:
            tb_tail = "\n".join(f["traceback"].strip().splitlines()[-5:])
            samples.append(f"\n[반복 {f['iteration']}] {f['scenario']}\n"
                           f"  {f['exception']}: {f['message']}\n{tb_tail}")
        pytest.fail(
            f"100회 중 {len(failures)}건 실패 / "
            f"영향 시나리오 {len(unique)}개: {unique}\n"
            + "\n".join(samples)
        )


# ═══════════════════════════════════════════════════════════════════════════════
# §8  스탠드얼론 실행
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    MAX_ROUNDS = 10

    print("=" * 72)
    print("E2E 시뮬레이션 테스트 — 스탠드얼론 모드")
    print(f"시나리오: {len(ALL_SCENARIOS)}개  /  반복: 100회")
    print("=" * 72)

    for rnd in range(1, MAX_ROUNDS + 1):
        print(f"\n▶ 라운드 {rnd}: 100회 시작")
        passed, failures = run_100_iterations(verbose=True)

        if passed:
            print(f"\n{'='*72}")
            print(f"✅  100회 전부 통과! (라운드 {rnd})")
            print(f"    {len(ALL_SCENARIOS)}개 시나리오 × 100회 = {len(ALL_SCENARIOS)*100}회 검증")
            print(f"{'='*72}")
            sys.exit(0)

        unique = sorted({f["scenario"] for f in failures})
        print(f"\n❌  {len(failures)}건 실패  /  영향 시나리오: {unique}")
        for f in failures[:10]:
            print(f"\n  ── 반복 {f['iteration']:>3} / {f['scenario']} ──")
            print(f"  {f['exception']}: {f['message']}")
            for line in f["traceback"].strip().splitlines()[-3:]:
                print(f"    {line}")

        if rnd < MAX_ROUNDS:
            print(f"\n→ 라운드 {rnd + 1} 재시도...")
        else:
            print(f"\n최대 {MAX_ROUNDS}라운드 초과 — 종료")
            sys.exit(1)
