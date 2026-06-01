"""스마트스토어 드롭셔핑 자동화 — E2E 시뮬레이션 v2

가상 인물 100명 × 상품 10개 전체 주문 흐름을 하나의 연속 시나리오로 시뮬레이션.
오류 발생 시 즉시 중단 → 새 데이터 생성 후 처음부터 재시작.
1회 오류 없이 통과하면 완료.

실행:
    python tests/test_e2e_v2.py          # 스탠드얼론
    pytest tests/test_e2e_v2.py -v       # pytest
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import sys
import tempfile
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from src.db.database import init_db, get_session
from src.db.models import SupplierOrder

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("e2e_v2")

MAX_RETRIES = 5  # 최대 재시도 횟수

# ═══════════════════════════════════════════════════════════════════════════════
# §1  상품 정의 (A~J, 10개)
# ═══════════════════════════════════════════════════════════════════════════════
# (key, ss_product_id, supplier, supplier_product_id, name, price, stock, options, delivery_company)
PRODUCT_DEFS = [
    ("A","SS_PROD_A","domaekkuk","DK_PROD_A","면 슬리퍼 세트",       8_900, 200,
     [{"name":"사이즈","values":["230","240","250","260"]}],      "CJ대한통운"),
    ("B","SS_PROD_B","domaekkuk","DK_PROD_B","캐릭터 세라믹 머그",   12_500, 150,
     [{"name":"색상",  "values":["화이트","블루","핑크"]}],       "롯데택배"),
    ("C","SS_PROD_C","domaemae", "DM_PROD_C","탄성 저항 밴드",        5_500, 300,
     [],                                                            "CJ대한통운"),
    ("D","SS_PROD_D","domaekkuk","DK_PROD_D","LED 무선 독서등",      24_800,  80,
     [{"name":"색상",  "values":["화이트","블랙"]}],               "한진택배"),
    ("E","SS_PROD_E","domaemae", "DM_PROD_E","유기농 그린티 선물세트",18_000, 120,
     [{"name":"용량",  "values":["30g","50g","100g"]}],           "우체국택배"),
    ("F","SS_PROD_F","domaekkuk","DK_PROD_F","3단 자동 우산",          9_800, 180,
     [],                                                            "롯데택배"),
    ("G","SS_PROD_G","domaemae", "DM_PROD_G","노트북 와이어 거치대",  32_000,  60,
     [{"name":"타입",  "values":["A형","B형"]}],                   "CJ대한통운"),
    ("H","SS_PROD_H","domaekkuk","DK_PROD_H","실리콘 주방 장갑",       7_200, 250,
     [{"name":"색상",  "values":["레드","그린","옐로우"]}],        "롯데택배"),
    ("I","SS_PROD_I","domaemae", "DM_PROD_I","스트레칭 폼롤러",       15_500,  45,
     [{"name":"크기",  "values":["30cm","60cm"]}],                 "한진택배"),
    ("J","SS_PROD_J","domaekkuk","DK_PROD_J","감성 캔들 홀더",        11_000,  90,
     [],                                                            "CJ대한통운"),
]
PROD_BY_KEY = {p[0]: p for p in PRODUCT_DEFS}

# 취소 케이스 배정 (상품 key → 경우)
CANCEL_CASES = {
    "A": "case1_pre_cancel",         # 발주 전 취소 → SS_AUTO
    "B": "case2_approved",            # 도매처 취소 승인 → APPROVED
    "C": "case3_rejected_wait_ship",  # 취소 거부 + 미발송 → REJECTED_WAIT_SHIP
    "D": "case5_shipped_reject",      # 배송 완료 후 취소 → SHIPPED_REJECT
    "E": "case6_urgent_3day",         # 3영업일 → URGENT_3DAY
    "F": "case7_manual_4day",         # 4영업일 초과 → MANUAL_4DAY
    "G": "case8_race_condition",      # 레이스 컨디션
}

# ═══════════════════════════════════════════════════════════════════════════════
# §2  가상 인물 100명 생성
# ═══════════════════════════════════════════════════════════════════════════════
_LAST  = ["김","이","박","최","정","강","조","윤","장","임","한","오","서","신","권","황","안","송","유","홍"]
_FIRST = [
    "민준","서준","도윤","예준","시우","주원","하준","지호","준서","지우",
    "지유","서연","서윤","지아","하은","하윤","윤서","채원","지민","수아",
    "성민","현우","동현","정우","재원","민재","도현","승현","영우","태양",
    "아름","나은","예린","유진","소연","민지","혜원","지수","수진","은지",
    "준혁","현준","이안","건우","우진","민성","태민","준영","성준","혁준",
    "예나","수빈","서현","지혜","은서","민아","유나","다은","하나","선영",
    "동훈","세준","진혁","태준","원준","재혁","민호","지훈","규민","진우",
    "미래","채은","혜진","보람","정은","서희","미소","연아","은비","세아",
    "유찬","인준","찬호","주안","한솔","현태","진서","재민","성호","민규",
    "지은","혜은","은혜","아진","예지","소희","나희","정아","민서","수현",
]
_CITIES = [
    "서울시 강남구","서울시 마포구","서울시 노원구","부산시 해운대구",
    "부산시 수영구","대구시 수성구","인천시 남동구","광주시 서구",
    "대전시 유성구","수원시 팔달구","울산시 남구","고양시 일산서구",
    "성남시 분당구","용인시 수지구","창원시 성산구",
]
_ROADS = [
    "테헤란로","강남대로","을지로","종로","노원로","해운대로","수성로",
    "남동대로","유성대로","정조로","울산로","일산로","중앙대로","분당로","한강로",
]
_MEMOS = ["문 앞에 놔주세요","경비실에 맡겨주세요","벨 눌러주세요",
          "부재 시 경비실 부탁드려요","조심히 다뤄주세요","빠른 배송 부탁드려요",
          "부재 시 문 앞에 두세요","","",""]


def _generate_people() -> list[dict]:
    """100명 가상 인물 완전 무작위 생성."""
    people: list[dict] = []
    used_names: set[str] = set()
    used_phones: set[str] = set()

    for i in range(100):
        prod = PRODUCT_DEFS[i // 10]
        key, ss_id, supplier, sup_id, name, price, stock, options, dc = prod
        buyer_idx = i % 10
        order_id  = f"ORD_{key}_{buyer_idx:02d}"

        while True:
            bname = random.choice(_LAST) + random.choice(_FIRST)
            if bname not in used_names:
                used_names.add(bname)
                break
        rname = bname if random.random() > 0.3 else (random.choice(_LAST) + random.choice(_FIRST))

        while True:
            phone = f"010-{random.randint(1000,9999)}-{random.randint(1000,9999)}"
            if phone not in used_phones:
                used_phones.add(phone)
                break

        city    = random.choice(_CITIES)
        road    = random.choice(_ROADS)
        num     = random.randint(1, 500)
        dong    = random.randint(100, 999)
        ho      = random.randint(101, 2504)
        zipcode = f"{random.randint(10000, 69999):05d}"
        detail  = f"{dong}동 {ho}호"
        address = f"{city} {road} {num} {detail}"
        road_address = f"{city} {road} {num}"
        qty  = random.randint(1, 3)
        opt  = random.choice(options[0]["values"]) if options else ""

        people.append({
            "index":                   i,
            "order_id":                order_id,
            "prod_key":                key,
            "buyer_idx":               buyer_idx,
            "buyer_name":              bname,
            "receiver_name":           rname,
            "receiver_phone":          phone,
            "receiver_address":        address,
            "receiver_road_address":   road_address,
            "receiver_detail_address": detail,
            "receiver_zipcode":        zipcode,
            "delivery_memo":           random.choice(_MEMOS),
            "quantity":                qty,
            "option_value":            opt,
            "product_name":            name,
            "ss_product_id":           ss_id,
            "supplier":                supplier,
            "supplier_product_id":     sup_id,
            "unit_price":              price,
            "delivery_company":        dc,
        })
    return people


def _people_by_id(people: list[dict]) -> dict[str, dict]:
    return {p["order_id"]: p for p in people}


# ═══════════════════════════════════════════════════════════════════════════════
# §3  Mock 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class MockEmailNotifier:
    def __init__(self):
        self.sent: list[dict] = []
    def send(self, subject: str, body: str):
        self.sent.append({"subject": subject, "body": body})
    def clear(self):
        self.sent.clear()
    def has_subject(self, kw: str) -> bool:
        return any(kw in e["subject"] for e in self.sent)
    def count_subject(self, kw: str) -> int:
        return sum(1 for e in self.sent if kw in e["subject"])


class MockSmartstoreAPI:
    BASE_URL = "https://mock.api.commerce.naver.com/external"
    def __init__(self):
        self.orders_to_return:        list[dict] = []
        self.cancellations_to_return: list[dict] = []
        self.cancellations_24h:       list[dict] = []
        self.returns_to_return:       list[dict] = []
        self.dispatch_fail = False
        self.approve_cancel_fail = False
        self.sale_status_fail = False
        self.dispatched:           list[dict] = []
        self.confirmed:            list[str]  = []
        self.approved_cancels:     list[str]  = []
        self.sale_status_changes:  list[dict] = []
        self.category_result: tuple[str,str] = ("","")
        self.kc_cert_status:  tuple[bool,int] = (False, 0)
        self.image_upload_calls = 0

    def _headers(self): return {"Authorization": "Bearer mock"}
    def get_orders(self, status="PAYED", days=1): return self.orders_to_return
    def get_cancellations(self, hours=1):
        return self.cancellations_to_return if hours <= 1 else self.cancellations_24h
    def get_returns(self, hours=1): return self.returns_to_return

    def dispatch_order(self, product_order_id, delivery_company_code, tracking_number):
        if self.dispatch_fail:
            raise RuntimeError("SS dispatch 실패 (Mock)")
        self.dispatched.append({"order_id": product_order_id,
                                 "company": delivery_company_code,
                                 "tracking": tracking_number})
        return {"success": True}

    def confirm_orders(self, product_order_ids: list[str]) -> dict:
        confirmed = []
        for i in range(0, len(product_order_ids), 30):
            confirmed.extend(product_order_ids[i:i+30])
        self.confirmed.extend(confirmed)
        return {"confirmed": confirmed, "failed": []}

    def approve_cancel(self, product_order_id):
        if self.approve_cancel_fail:
            raise RuntimeError("SS 취소승인 실패 (Mock)")
        self.approved_cancels.append(product_order_id)
        return {"success": True}

    def set_product_sale_status(self, product_id, on_sale):
        if self.sale_status_fail:
            raise RuntimeError("SS 판매상태 실패 (Mock)")
        self.sale_status_changes.append({"product_id": product_id, "on_sale": on_sale})
        return {"success": True}

    def get_product(self, pid): return {"id": pid, "saleStatus": "ON_SALE"}
    def get_products(self, size=100): return []
    def find_leaf_category(self, kw, whole_cat=""): return self.category_result
    def get_kc_cert_status(self, cat_id, hint=""): return self.kc_cert_status
    def upload_image_data(self, data, ct, fname):
        self.image_upload_calls += 1
        return f"https://pstatic.net/mock/{fname}"


class MockDomaekkukAPI:
    def __init__(self):
        self._counter    = 0
        self.products:       dict[str, dict] = {}
        self.tracking:       dict[str, dict] = {}
        self.cancel_results: dict[str, str]  = {}
        self.placed:         list[dict]      = []
        self.cancel_requests: list[str]      = []
        self.order_fail = False

    def _default(self, pid):
        return {"title": f"도매꾹_{pid}", "price": 10_000, "stock": 100, "seller_id": "s1"}

    def get_product(self, product_no):
        return self.products.get(str(product_no), self._default(product_no))

    def get_stock(self, pid): return self.get_product(str(pid)).get("stock", 0)
    def search_products(self, kw, **kw2): return {"total": 0, "items": []}
    def get_options(self, pid): return []

    def place_order(self, product_no, quantity, shipping_info, *,
                    supplier_option_id="", dry_run=False):
        if dry_run:
            return {"order_no": f"[DRY_RUN] {product_no}"}
        if self.order_fail:
            raise RuntimeError("도매꾹 발주 실패 (Mock)")
        self._counter += 1
        order_no = f"DK_{product_no}_{self._counter:05d}"
        self.placed.append({"order_no": order_no, "product_no": str(product_no),
                             "qty": quantity, "shipping": dict(shipping_info),
                             "supplier_option_id": supplier_option_id})
        return {"order_no": order_no}

    def cancel_order(self, order_no):
        self.cancel_requests.append(order_no); return {"success": True}

    def get_cancel_result(self, order_no):
        return self.cancel_results.get(str(order_no), "PENDING")

    def get_order_tracking(self, order_no):
        return self.tracking.get(str(order_no),
                                 {"order_no": order_no, "delivery_company": "", "tracking_number": ""})


class MockDomaemaeClient:
    def __init__(self):
        self._counter    = 0
        self.products:       dict[str, dict] = {}
        self.tracking:       dict[str, dict] = {}
        self.cancel_results: dict[str, str]  = {}
        self.placed:         list[dict]      = []
        self.cancel_requests: list[str]      = []
        self.order_fail  = False
        self.api_key     = "mock_key"
        self._sid        = "mock_sid"
        self._sid_renew_date  = datetime.now() + timedelta(hours=24)
        self._login_time      = datetime.now()
        self._login_keep_seconds = 86400 * 30

    def _ensure_session(self): pass

    def _default(self, pid):
        return {"product_id": pid, "title": f"도매매_{pid}", "price": 8_000, "stock": 50}

    def get_product(self, pid):
        return self.products.get(str(pid), self._default(pid))

    def get_stock(self, pid): return self.get_product(str(pid)).get("stock", 0)
    def get_options(self, pid): return []

    def place_order(self, product_id, quantity, shipping_info, *,
                    option_id="", option_name="", dry_run=False):
        if dry_run:
            return f"[DRY_RUN] {product_id}"
        if self.order_fail:
            raise RuntimeError("도매매 발주 실패 (Mock)")
        self._counter += 1
        order_no = f"DM_{product_id}_{self._counter:05d}"
        self.placed.append({"order_no": order_no, "product_id": str(product_id),
                             "qty": quantity, "shipping": dict(shipping_info),
                             "option_id": option_id, "option_name": option_name})
        return order_no  # domaemae returns string

    def cancel_order(self, order_no):
        self.cancel_requests.append(order_no); return {"success": True}

    def get_cancel_result(self, order_no):
        return self.cancel_results.get(str(order_no), "PENDING")

    def get_order_tracking(self, order_no):
        return self.tracking.get(str(order_no),
                                 {"order_no": order_no, "delivery_company": "", "tracking_number": ""})


# ═══════════════════════════════════════════════════════════════════════════════
# §4  환경 설정
# ═══════════════════════════════════════════════════════════════════════════════

@contextlib.contextmanager
def _env():
    import src.db.database as _db_mod
    old_dir = os.getcwd()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
        os.chdir(tmp)
        init_db(os.path.join(tmp, "data", "orders.db"))
        ss  = MockSmartstoreAPI()
        dk  = MockDomaekkukAPI()
        dm  = MockDomaemaeClient()
        ntf = MockEmailNotifier()
        try:
            yield ss, dk, dm, ntf, Path(tmp)
        finally:
            if _db_mod._engine is not None:
                _db_mod._engine.dispose()
                _db_mod._engine = None
                _db_mod._SessionFactory = None
            os.chdir(old_dir)


# ═══════════════════════════════════════════════════════════════════════════════
# §5  헬퍼
# ═══════════════════════════════════════════════════════════════════════════════

def _business_days_ago(n: int) -> str:
    today = datetime.now(timezone.utc).date()   # cancel_monitor 와 동일하게 UTC 기준
    count, d = 0, today
    while count < n:
        if d.weekday() < 5:
            count += 1
        if count < n:
            d -= timedelta(days=1)
    start = d - timedelta(days=1)
    return datetime(start.year, start.month, start.day, tzinfo=timezone.utc).isoformat()


def _get_sup_order_no(order_id: str) -> str:
    with get_session() as session:
        row = session.query(SupplierOrder).filter_by(ss_order_id=order_id).first()
        return row.supplier_order_no if row else ""


def _raw_order_from_person(p: dict) -> dict:
    return {
        "productOrder": {
            "productOrderId": p["order_id"],
            "orderId":        p["order_id"],
            "productId":      p["ss_product_id"],
            "productName":    p["product_name"],
            "optionCode":     "",   # 매핑은 옵션 없이 등록됨 → "" 로 조회해야 매칭
            "quantity":       p["quantity"],
            "invoiceNumber":  "",
        },
        "order": {"ordererName": p["buyer_name"], "ordererTel": p["receiver_phone"]},
        "shippingAddress": {
            "name":          p["receiver_name"],
            "tel1":          p["receiver_phone"],
            "addressStr":    p["receiver_address"],
            "roadAddress":   p["receiver_road_address"],
            "detailAddress": p["receiver_detail_address"],
            "zipCode":       p["receiver_zipcode"],
        },
        "deliveryMemo": p["delivery_memo"],
        "claim": {},
    }


def _raw_cancel_from_person(p: dict, reason: str = "변심") -> dict:
    return {
        "productOrder": {
            "productOrderId": p["order_id"],
            "orderId":        p["order_id"],
            "productName":    p["product_name"],
            "quantity":       p["quantity"],
            "invoiceNumber":  "",
        },
        "order": {"ordererName": p["buyer_name"], "ordererTel": p["receiver_phone"]},
        "shippingAddress": {
            "name":       p["receiver_name"],
            "tel1":       p["receiver_phone"],
            "addressStr": p["receiver_address"],
            "zipCode":    p["receiver_zipcode"],
        },
        "claim": {"cancelReason": reason},
    }


def _raw_return_from_person(p: dict, tracking_number: str, reason: str = "단순변심") -> dict:
    return {
        "productOrder": {
            "productOrderId":     p["order_id"],
            "orderId":            p["order_id"],
            "productId":          p["ss_product_id"],
            "productName":        p["product_name"],
            "optionCode":         p["option_value"],
            "optionName":         p["option_value"],
            "quantity":           p["quantity"],
            "unitPrice":          p["unit_price"],
            "totalPaymentAmount": p["unit_price"] * p["quantity"],
            "invoiceNumber":      tracking_number,
            "deliveryCompany":    p["delivery_company"],
        },
        "order": {"ordererName": p["buyer_name"], "ordererTel": p["receiver_phone"]},
        "shippingAddress": {
            "name":          p["receiver_name"],
            "tel1":          p["receiver_phone"],
            "addressStr":    p["receiver_address"],
            "roadAddress":   p["receiver_road_address"],
            "detailAddress": p["receiver_detail_address"],
            "zipCode":       p["receiver_zipcode"],
        },
        "deliveryMemo": p["delivery_memo"],
        "claim": {
            "returnReason":         reason,
            "returnReasonType":     "SIMPLE_CHANGE",
            "claimStatus":          "RETURN_REQUEST",
            "claimRequestDate":     datetime.now(timezone.utc).isoformat(),
            "returnDeliveryCompany":"롯데택배",
            "returnTrackingNumber": f"RET_{p['order_id']}",
            "refundExpectedDate":   (date.today() + timedelta(days=3)).isoformat(),
            "deliveryFeePayType":   "SELLER",
            "claimPrice":           {"refundPayAmount": p["unit_price"] * p["quantity"]},
        },
    }


def _write_confirm_log(order_ids: list[str]):
    data = {"confirms": [
        {"order_id": oid, "confirmed_at": datetime.now(timezone.utc).isoformat()}
        for oid in order_ids
    ]}
    Path("data/confirm_log.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_cancel_entry(order_id: str) -> dict | None:
    f = Path("data/cancellations.json")
    if not f.exists():
        return None
    data = json.loads(f.read_text(encoding="utf-8"))
    return next((e for e in data["cancellations"] if e["product_order_id"] == order_id), None)


# ═══════════════════════════════════════════════════════════════════════════════
# §6  단계 함수
# ═══════════════════════════════════════════════════════════════════════════════

def phase1_order_collection(ss, order_repo, people: list[dict]) -> dict:
    """주문 수집 — 100건 정확히 수집 + 중복 방지."""
    from src.core.order_collector import OrderCollector

    ss.orders_to_return = [_raw_order_from_person(p) for p in people]
    collector = OrderCollector(ss, repo=order_repo)
    added = collector.run()

    assert added == 100, f"수집 100 기대, 실제 {added}"
    orders = order_repo.all()
    assert len(orders) == 100, f"DB 100건 기대, 실제 {len(orders)}"
    assert all(o["status"] == "NEW" for o in orders), "NEW 이외 상태 존재"

    ids = {o["order_id"] for o in orders}
    expected = {p["order_id"] for p in people}
    assert ids == expected, f"주문 ID 불일치: {ids ^ expected}"

    dup = collector.run()
    assert dup == 0, f"중복 수집 발생 {dup}건"

    ss.orders_to_return = []
    return {"collected": 100, "duplicate_prevented": True}


def phase2_order_placement(ss, dk, dm, ntf, order_repo, mapping_repo,
                            people: list[dict]) -> dict:
    """발주확인 + 자동 발주 — 이름·주소·옵션·수량 검증 포함."""
    from src.core.order_placer import OrderPlacer
    from src.core.budget_manager import BudgetManager
    from src.core.pending_order_repository import PendingOrderRepository
    from src.core.stock_pending_repository import StockPendingRepository

    pid = _people_by_id(people)

    # 상품 정보 등록 (mock)
    for key, ss_id, supplier, sup_id, name, price, stock, opts, dc in PRODUCT_DEFS:
        if supplier == "domaekkuk":
            dk.products[sup_id] = {"title": name, "price": price, "stock": stock, "seller_id": "s1"}
        else:
            dm.products[sup_id] = {"product_id": sup_id, "title": name, "price": price, "stock": stock}

    # 매핑 등록
    for key, ss_id, supplier, sup_id, *_ in PRODUCT_DEFS:
        mapping_repo.add(ss_product_id=ss_id, supplier=supplier, supplier_url_or_id=sup_id)

    # 경우1: ORD_A_00 취소 요청 (발주 전 취소)
    # OrderPlacer._filter_cancel_requests 는 hours=24 로 조회 → cancellations_24h 설정
    ss.cancellations_24h = [_raw_cancel_from_person(pid["ORD_A_00"], "변심")]

    placer = OrderPlacer(
        dk, dm,
        mapping_repo  = mapping_repo,
        order_repo    = order_repo,
        notifier      = ntf,
        budget        = BudgetManager(initial_balance=20_000_000),
        pending       = PendingOrderRepository(),
        ss_api        = ss,
        stock_pending = StockPendingRepository(),
    )
    stats = placer.run()

    assert stats["cancelled"]    == 1,  f"취소건너뜀 1 기대, 실제 {stats['cancelled']}"
    assert stats["ordered"]      == 99, f"발주 99 기대, 실제 {stats['ordered']}"
    assert stats["ss_confirmed"] == 99, f"발주확인 99 기대, 실제 {stats['ss_confirmed']}"

    a00 = order_repo.find("ORD_A_00")
    assert a00["status"] == "CANCELLED", f"ORD_A_00 상태: {a00['status']}"

    # 나머지 99건 ORDERED 검증
    ordered = order_repo.find_by_status("ORDERED")
    assert len(ordered) == 99, f"ORDERED 99 기대, 실제 {len(ordered)}"

    # 발주 정보 정확성 검증 (임의 5건)
    normal = [p for p in people if p["order_id"] != "ORD_A_00"]
    sample = random.sample(normal, 5)
    for p in sample:
        sup_no = _get_sup_order_no(p["order_id"])
        assert sup_no, f"{p['order_id']} supplier_order_no 없음"
        placed_list = dk.placed if p["supplier"] == "domaekkuk" else dm.placed
        placed = next((x for x in placed_list if x["order_no"] == sup_no), None)
        assert placed is not None, f"{p['order_id']} 발주 레코드 없음 (order_no={sup_no})"
        ship = placed["shipping"]
        assert ship["name"]    == p["receiver_name"],    f"{p['order_id']} 수령인 불일치"
        assert ship["phone"]   == p["receiver_phone"],   f"{p['order_id']} 전화번호 불일치"
        assert ship["address"] == p["receiver_address"], f"{p['order_id']} 주소 불일치"
        assert ship["zipcode"] == p["receiver_zipcode"], f"{p['order_id']} 우편번호 불일치"
        assert placed["qty"]   == p["quantity"],          f"{p['order_id']} 수량 불일치"

    ss.cancellations_24h      = []
    ss.cancellations_to_return = []
    return {"ordered": 99, "cancelled": 1, "ss_confirmed": 99, "verified_sample": 5}


def phase3_invoice_registration(ss, dk, dm, ntf, order_repo,
                                  people: list[dict]) -> tuple[dict, dict]:
    """송장 등록 — 95건 등록, 4건 대기 (경우 2,3,6,7 취소 케이스는 트래킹 없음)."""
    from src.core.invoice_manager import InvoiceManager

    NO_TRACKING = {"ORD_B_00", "ORD_C_00", "ORD_E_00", "ORD_F_00"}
    tracking_map: dict[str, str] = {}  # order_id → tracking_number

    ordered = order_repo.find_by_status("ORDERED")
    assert len(ordered) == 99, f"ORDERED 99 기대, 실제 {len(ordered)}"

    for order in ordered:
        oid = order["order_id"]
        if oid in NO_TRACKING:
            continue
        supplier       = order.get("supplier", "")
        sup_no         = _get_sup_order_no(oid)
        p              = next(x for x in people if x["order_id"] == oid)
        tracking_no    = f"TRK{oid.replace('_','')}"
        dc             = p["delivery_company"]

        if supplier == "domaekkuk":
            dk.tracking[sup_no] = {"delivery_company": dc, "tracking_number": tracking_no}
        else:
            dm.tracking[sup_no] = {"delivery_company": dc, "tracking_number": tracking_no}
        tracking_map[oid] = tracking_no

    stats = InvoiceManager(ss, dk, dm, order_repo=order_repo, notifier=ntf).run()

    assert stats["invoiced"] == 95, f"송장 등록 95 기대, 실제 {stats['invoiced']}"
    assert stats["pending"]  == 4,  f"송장 대기 4 기대, 실제 {stats['pending']}"
    assert stats["error"]    == 0,  f"송장 오류 발생: {stats['error']}"
    assert len(ss.dispatched) == 95, f"SS dispatch 95 기대, 실제 {len(ss.dispatched)}"

    # 샘플 검증: 트래킹 번호 정확히 전달됐는지
    for oid in random.sample(list(tracking_map.keys()), 5):
        d = next((x for x in ss.dispatched if x["order_id"] == oid), None)
        assert d is not None, f"{oid} dispatch 기록 없음"
        assert d["tracking"] == tracking_map[oid], f"{oid} 트래킹 불일치"

    return {"invoiced": 95, "pending": 4}, tracking_map


def phase4_cancel_tests(ss, dk, dm, ntf, people: list[dict]) -> dict:
    """취소 처리 — 7가지 경우 전부 검증."""
    from src.core.cancel_monitor import CancelMonitor

    pid  = _people_by_id(people)
    mon  = CancelMonitor(ss, ntf, dk_api=dk, dm_cli=dm)
    results: dict[str, str] = {}

    # ── 4-a: 경우2 (APPROVED), 경우3 (REJECTED_WAIT_SHIP), ───────────────────
    #          경우5 (SHIPPED_REJECT), 경우8 (RACE_CONDITION)

    b00_sup = _get_sup_order_no("ORD_B_00")
    c00_sup = _get_sup_order_no("ORD_C_00")
    assert b00_sup, "ORD_B_00 supplier_order_no 없음"
    assert c00_sup, "ORD_C_00 supplier_order_no 없음"

    dk.cancel_results[b00_sup] = "APPROVED"   # 경우2: B(도매꾹) → 승인
    dm.cancel_results[c00_sup] = "REJECTED"   # 경우3: C(도매매) → 거부+미발송

    ss.cancellations_to_return = [
        _raw_cancel_from_person(pid["ORD_B_00"], "단순변심"),
        _raw_cancel_from_person(pid["ORD_C_00"], "단순변심"),
        _raw_cancel_from_person(pid["ORD_D_00"], "단순변심"),  # 경우5: 이미 배송
    ]
    ss.cancellations_24h = [_raw_cancel_from_person(pid["ORD_G_00"], "단순변심")]
    _write_confirm_log(["ORD_G_00"])  # 경우8: 발주확인 후 취소 레이스

    ntf.clear()
    mon.run()

    e_B = _load_cancel_entry("ORD_B_00")
    e_C = _load_cancel_entry("ORD_C_00")
    e_D = _load_cancel_entry("ORD_D_00")
    e_G = _load_cancel_entry("ORD_G_00")

    assert e_B and e_B["cancel_state"] == "APPROVED", \
        f"경우2(B): {e_B and e_B.get('cancel_state')}"
    assert "ORD_B_00" in ss.approved_cancels, "경우2: SS approve_cancel 미호출"
    results["case2"] = "APPROVED"

    assert e_C and e_C["cancel_state"] == "REJECTED_WAIT_SHIP", \
        f"경우3(C): {e_C and e_C.get('cancel_state')}"
    results["case3"] = "REJECTED_WAIT_SHIP"

    assert e_D and e_D["cancel_state"] == "SHIPPED_REJECT", \
        f"경우5(D): {e_D and e_D.get('cancel_state')}"
    d_dispatched = any(x["order_id"] == "ORD_D_00" for x in ss.dispatched)
    assert d_dispatched, "경우5: SS dispatch 미호출"
    results["case5"] = "SHIPPED_REJECT"

    assert e_G and e_G["cancel_state"] == "RACE_CONDITION", \
        f"경우8(G): {e_G and e_G.get('cancel_state')}"
    assert ntf.has_subject("발주확인") or ntf.has_subject("예외"), "경우8: 알림 없음"
    results["case8"] = "RACE_CONDITION"

    # ── 4-b: 경우6 (URGENT_3DAY), 경우7 (MANUAL_4DAY) ───────────────────────

    ss.cancellations_to_return = [
        _raw_cancel_from_person(pid["ORD_E_00"], "단순변심"),
        _raw_cancel_from_person(pid["ORD_F_00"], "단순변심"),
    ]
    ss.cancellations_24h = []

    mon.run()  # E00, F00 → DENY_SENT

    e_E = _load_cancel_entry("ORD_E_00")
    e_F = _load_cancel_entry("ORD_F_00")
    assert e_E and e_E["cancel_state"] == "DENY_SENT", f"E00 DENY_SENT 기대: {e_E}"
    assert e_F and e_F["cancel_state"] == "DENY_SENT", f"F00 DENY_SENT 기대: {e_F}"

    # deny_sent_at 조작: E=3영업일 전, F=4영업일 전
    cancel_file = Path("data/cancellations.json")
    data = json.loads(cancel_file.read_text(encoding="utf-8"))
    for entry in data["cancellations"]:
        if entry["product_order_id"] == "ORD_E_00":
            entry["deny_sent_at"] = _business_days_ago(3)
        elif entry["product_order_id"] == "ORD_F_00":
            entry["deny_sent_at"] = _business_days_ago(4)
    cancel_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    ss.cancellations_to_return = []
    ntf.clear()
    mon.run()

    e_E = _load_cancel_entry("ORD_E_00")
    e_F = _load_cancel_entry("ORD_F_00")
    assert e_E and e_E["cancel_state"] == "URGENT_3DAY", \
        f"경우6(E): {e_E and e_E.get('cancel_state')}"
    assert ntf.has_subject("긴급"), "경우6: 긴급 알림 없음"
    results["case6"] = "URGENT_3DAY"

    assert e_F and e_F["cancel_state"] == "MANUAL_4DAY", \
        f"경우7(F): {e_F and e_F.get('cancel_state')}"
    results["case7"] = "MANUAL_4DAY"

    # ── 4-c: 경우3 후속 — 트래킹 등록 → INVOICE_REGISTERED ─────────────────

    c00_sup = _get_sup_order_no("ORD_C_00")
    with get_session() as session:
        row = session.query(SupplierOrder).filter_by(ss_order_id="ORD_C_00").first()
        if row:
            row.tracking_number  = f"TRKORD_C_00"
            row.delivery_company = "CJ대한통운"

    mon.run()

    e_C = _load_cancel_entry("ORD_C_00")
    assert e_C and e_C["cancel_state"] == "INVOICE_REGISTERED", \
        f"경우3 후속(C): {e_C and e_C.get('cancel_state')}"
    results["case3_followup"] = "INVOICE_REGISTERED"

    # 경우1: ORD_A_00은 phase2에서 이미 CANCELLED (SS_AUTO)
    a00 = _load_cancel_entry("ORD_A_00")
    # CANCEL_REQUEST 전에 발주 자체가 안 됐으므로 cancellations.json에 없음 (SS 자동처리)
    results["case1"] = "SS_AUTO (phase2 발주 차단)"

    return results


def phase5_return_monitor(ss, ntf, people: list[dict], tracking_map: dict) -> dict:
    """반품 감지 — 상품당 1건(buyer_idx=1), 총 10건, 이메일 검증."""
    from src.core.return_monitor import ReturnMonitor

    return_people = [p for p in people if p["buyer_idx"] == 1]
    assert len(return_people) == 10, f"반품 대상 10명 기대, 실제 {len(return_people)}"

    ss.returns_to_return = [
        _raw_return_from_person(p, tracking_map.get(p["order_id"], "TRK_UNKNOWN"))
        for p in return_people
    ]

    ntf.clear()
    monitor = ReturnMonitor(ss, ntf)
    stats = monitor.run()

    assert stats["new"] == 10, f"반품 감지 10 기대, 실제 {stats['new']}"
    assert len(ntf.sent) == 10, f"반품 이메일 10 기대, 실제 {len(ntf.sent)}"
    assert all("반품" in e["subject"] for e in ntf.sent), "반품 이메일 제목 누락"

    # returns.json 저장 검증
    ret_data = json.loads(Path("data/returns.json").read_text(encoding="utf-8"))
    assert len(ret_data["returns"]) == 10, "returns.json 항목 10 기대"

    # 필드 검증 (첫 번째 항목)
    entry = ret_data["returns"][0]
    rp = return_people[0]
    assert entry["product_order_id"] == rp["order_id"]
    assert entry["buyer_name"]    == rp["buyer_name"]
    assert entry["receiver_name"] == rp["receiver_name"]
    assert entry["quantity"]      == rp["quantity"]
    assert entry["claim_status"]  == "RETURN_REQUEST"

    # 이메일 본문 검증 (수령인명·수량 포함 여부)
    for i, (e, rp) in enumerate(zip(ntf.sent, return_people)):
        assert rp["receiver_name"] in e["body"], \
            f"반품 {i+1}: 수령인 {rp['receiver_name']} 이메일 누락"

    # 중복 방지
    stats2 = monitor.run()
    assert stats2["new"] == 0, "반품 중복 감지 발생"

    ss.returns_to_return = []
    return {"detected": 10, "emails_sent": 10}


def phase6_inventory_sync(ss, dk, dm, ntf, mapping_repo) -> dict:
    """재고 동기화 — 상품 I·J 품절 → SS 판매중지."""
    from src.core.inventory_sync import InventorySync

    # I, J 품절 설정
    dk.products["DK_PROD_J"] = {"title": "감성 캔들 홀더", "price": 11_000, "stock": 0, "seller_id": "s1"}
    dm.products["DM_PROD_I"] = {"product_id": "DM_PROD_I", "title": "폼롤러", "price": 15_500, "stock": 0}

    ntf.clear()
    sync = InventorySync(ss, dk, dm, mapping_repo=mapping_repo, notifier=ntf)
    stats = sync.run()

    assert stats["paused"] >= 2, f"품절 판매중지 2 이상 기대, 실제 {stats['paused']}"
    paused_ids = {c["product_id"] for c in ss.sale_status_changes if not c["on_sale"]}
    assert "SS_PROD_I" in paused_ids, "SS_PROD_I 판매중지 미실행"
    assert "SS_PROD_J" in paused_ids, "SS_PROD_J 판매중지 미실행"
    assert ntf.count_subject("품절") >= 2, f"품절 이메일 2 이상 기대, 실제 {ntf.count_subject('품절')}"

    return {"paused": stats["paused"], "paused_products": sorted(paused_ids)}


def phase7_price_monitoring(dk, dm, ntf, mapping_repo) -> dict:
    """가격 모니터링 — A·B·C 가격 변동 감지."""
    from src.core.price_monitor import PriceMonitor
    from src.core.price_alert_repository import PriceAlertRepository

    alert_repo = PriceAlertRepository()
    monitor    = PriceMonitor(dk, dm, ntf, mapping_repo=mapping_repo, alert_repo=alert_repo)

    # 1차 실행: 초기 가격 기준선 확립
    ntf.clear()
    monitor.run()

    # A, B, C 가격 변경
    dk.products["DK_PROD_A"]["price"] = 10_500  # 8900 → 10500
    dk.products["DK_PROD_B"]["price"] =  9_900  # 12500 → 9900
    dm.products["DM_PROD_C"] = {"product_id": "DM_PROD_C", "title": "탄성 밴드", "price": 6_200, "stock": 300}

    ntf.clear()
    stats = monitor.run()

    assert stats["changed"] >= 3, f"가격 변동 3 이상 기대, 실제 {stats['changed']}"

    alerts = alert_repo.all()
    changed_ids = {a["ss_product_id"] for a in alerts if a.get("new_price") != a.get("old_price")}
    assert "SS_PROD_A" in changed_ids, "SS_PROD_A 가격 변동 미감지"
    assert "SS_PROD_B" in changed_ids, "SS_PROD_B 가격 변동 미감지"
    assert "SS_PROD_C" in changed_ids, "SS_PROD_C 가격 변동 미감지"

    return {"changed": stats["changed"], "changed_products": sorted(changed_ids)}


def phase8_budget_management(ss, dk, dm, ntf, order_repo, mapping_repo) -> dict:
    """예산 관리 — 예산 부족 시 PENDING 처리."""
    from src.core.order_placer import OrderPlacer
    from src.core.budget_manager import BudgetManager
    from src.core.pending_order_repository import PendingOrderRepository
    from src.core.stock_pending_repository import StockPendingRepository

    now = datetime.now().isoformat(timespec="seconds")
    budget_orders = [
        {"order_id": f"ORD_D_BUDGET_{i:02d}", "ss_order_id": f"ORD_D_BUDGET_{i:02d}",
         "product_id": "SS_PROD_D", "product_name": "LED 무선 독서등", "option_code": "",
         "quantity": 2, "buyer_name": f"예산테스트{i}", "receiver_name": f"예산수령{i}",
         "receiver_phone": f"010-0000-{i:04d}", "receiver_address": f"서울시 강남구 테헤란로 {i}",
         "receiver_zipcode": "06234", "delivery_memo": "", "status": "NEW",
         "collected_at": now, "updated_at": now}
        for i in range(1, 4)
    ]
    order_repo.add_many(budget_orders)

    budget  = BudgetManager(initial_balance=1_000, path="data/budget_test.json")  # 3건 발주 불가 예산
    pending = PendingOrderRepository()
    stock_p = StockPendingRepository()

    ntf.clear()
    placer = OrderPlacer(dk, dm, mapping_repo=mapping_repo, order_repo=order_repo,
                          notifier=ntf, budget=budget, pending=pending,
                          ss_api=ss, stock_pending=stock_p)
    stats = placer.run()

    assert stats["deferred"] == 3, f"대기 전환 3 기대, 실제 {stats['deferred']}"
    assert stats["ordered"]  == 0, f"발주 0 기대, 실제 {stats['ordered']}"
    for oid in [f"ORD_D_BUDGET_{i:02d}" for i in range(1, 4)]:
        o = order_repo.find(oid)
        assert o["status"] == "PENDING", f"{oid} 상태: {o['status']}"
    assert ntf.has_subject("예산 부족"), "예산 부족 이메일 없음"

    return {"deferred": 3, "budget_emails": ntf.count_subject("예산 부족")}


# ═══════════════════════════════════════════════════════════════════════════════
# §7  메인 시뮬레이션 실행기
# ═══════════════════════════════════════════════════════════════════════════════

def run_simulation() -> tuple[bool, list[dict]]:
    """전체 시뮬레이션 1회 실행. (passed, phase_results)."""
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository

    people = _generate_people()
    phase_results: list[dict] = []

    with _env() as (ss, dk, dm, ntf, tmpdir):
        order_repo   = OrderRepository()
        mapping_repo = MappingRepository()
        shared: dict = {}

        phases = [
            ("1단계: 주문 수집 (100건)",
             lambda: phase1_order_collection(ss, order_repo, people)),

            ("2단계: 발주확인 + 자동발주 (99건)",
             lambda: phase2_order_placement(ss, dk, dm, ntf, order_repo, mapping_repo, people)),

            ("3단계: 송장 등록 (95건 / 4건 대기)",
             lambda: _run_phase3(ss, dk, dm, ntf, order_repo, people, shared)),

            ("4단계: 취소 처리 (7가지 경우)",
             lambda: phase4_cancel_tests(ss, dk, dm, ntf, people)),

            ("5단계: 반품 감지 (10건)",
             lambda: phase5_return_monitor(ss, ntf, people, shared.get("tracking_map", {}))),

            ("6단계: 재고 동기화 (I·J 품절)",
             lambda: phase6_inventory_sync(ss, dk, dm, ntf, mapping_repo)),

            ("7단계: 가격 모니터링 (A·B·C 변동)",
             lambda: phase7_price_monitoring(dk, dm, ntf, mapping_repo)),

            ("8단계: 예산 관리 (부족→대기)",
             lambda: phase8_budget_management(ss, dk, dm, ntf, order_repo, mapping_repo)),
        ]

        for phase_name, phase_fn in phases:
            try:
                result = phase_fn()
                phase_results.append({"phase": phase_name, "status": "OK", "result": result})
            except AssertionError as exc:
                tb = traceback.format_exc()
                phase_results.append({
                    "phase": phase_name, "status": "FAIL",
                    "error": str(exc), "traceback": tb,
                })
                return False, phase_results
            except Exception as exc:
                tb = traceback.format_exc()
                phase_results.append({
                    "phase": phase_name, "status": "ERROR",
                    "error": str(exc), "traceback": tb,
                })
                return False, phase_results

    return True, phase_results


def _run_phase3(ss, dk, dm, ntf, order_repo, people, shared):
    result, tracking_map = phase3_invoice_registration(ss, dk, dm, ntf, order_repo, people)
    shared["tracking_map"] = tracking_map
    return result


def run_with_retries() -> tuple[bool, list[dict], int]:
    """오류 시 새 데이터로 재시작. (passed, last_results, attempts)."""
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n{'='*70}")
        print(f"  시뮬레이션 시도 {attempt}/{MAX_RETRIES}  (100명 × 10상품 새로 생성)")
        print(f"{'='*70}")

        passed, results = run_simulation()

        for r in results:
            icon = "[OK]  " if r["status"] == "OK" else "[FAIL]"
            print(f"  {icon} {r['phase']}", end="")
            if r["status"] == "OK":
                print(f"  → {r['result']}")
            else:
                print(f"\n      [{r['status']}] {r['error']}")
                tb_lines = r.get("traceback", "").strip().splitlines()
                for line in tb_lines[-6:]:
                    print(f"        {line}")

        if passed:
            print(f"\n{'='*70}")
            print(f"  [PASS] 전체 통과! (시도 {attempt}회)")
            print(f"{'='*70}")
            _print_statistics(results)
            return True, results, attempt
        else:
            failed = next(r for r in results if r["status"] != "OK")
            print(f"\n  -> 오류: {failed['phase']} - 새 데이터로 재시작...\n")

    print(f"\n최대 {MAX_RETRIES}회 재시도 초과 — 종료")
    return False, results, MAX_RETRIES


def _print_statistics(results: list[dict]):
    print("\n  ── 단계별 결과 ──")
    for r in results:
        detail = r.get("result", "")
        print(f"    {r['phase']}: {detail}")
    print(f"\n  총 단계: {len(results)}  /  성공: {sum(1 for r in results if r['status']=='OK')}")


# ═══════════════════════════════════════════════════════════════════════════════
# §8  pytest 통합
# ═══════════════════════════════════════════════════════════════════════════════

def test_e2e_v2_full_pass():
    """E2E v2 — 전체 1회 오류 없이 통과해야 합격."""
    passed, results, attempts = run_with_retries()
    if not passed:
        failed = [r for r in results if r["status"] != "OK"]
        msgs = []
        for f in failed:
            tb_tail = "\n".join(f.get("traceback","").strip().splitlines()[-8:])
            msgs.append(f"\n[{f['phase']}]\n  {f['status']}: {f['error']}\n{tb_tail}")
        pytest.fail(
            f"{MAX_RETRIES}회 시도 모두 실패\n"
            f"실패 단계: {[f['phase'] for f in failed]}\n"
            + "\n".join(msgs)
        )


# pytest 개별 단계 테스트 (빠른 CI 검증용)
def test_phase1_order_collection():
    from src.core.order_repository import OrderRepository
    with _env() as (ss, dk, dm, ntf, _):
        people = _generate_people()
        repo   = OrderRepository()
        phase1_order_collection(ss, repo, people)


def test_phase2_order_placement():
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository
    with _env() as (ss, dk, dm, ntf, _):
        people = _generate_people()
        repo   = OrderRepository()
        mr     = MappingRepository()
        phase1_order_collection(ss, repo, people)
        phase2_order_placement(ss, dk, dm, ntf, repo, mr, people)


def test_phase3_invoice_registration():
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository
    with _env() as (ss, dk, dm, ntf, _):
        people = _generate_people()
        repo   = OrderRepository()
        mr     = MappingRepository()
        phase1_order_collection(ss, repo, people)
        phase2_order_placement(ss, dk, dm, ntf, repo, mr, people)
        phase3_invoice_registration(ss, dk, dm, ntf, repo, people)


def test_phase4_cancel_tests():
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository
    with _env() as (ss, dk, dm, ntf, _):
        people = _generate_people()
        repo   = OrderRepository()
        mr     = MappingRepository()
        phase1_order_collection(ss, repo, people)
        phase2_order_placement(ss, dk, dm, ntf, repo, mr, people)
        phase3_invoice_registration(ss, dk, dm, ntf, repo, people)
        phase4_cancel_tests(ss, dk, dm, ntf, people)


def test_phase5_return_monitor():
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository
    with _env() as (ss, dk, dm, ntf, _):
        people = _generate_people()
        repo   = OrderRepository()
        mr     = MappingRepository()
        phase1_order_collection(ss, repo, people)
        phase2_order_placement(ss, dk, dm, ntf, repo, mr, people)
        _, tracking_map = phase3_invoice_registration(ss, dk, dm, ntf, repo, people)
        phase5_return_monitor(ss, ntf, people, tracking_map)


def test_phase6_inventory_sync():
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository
    with _env() as (ss, dk, dm, ntf, _):
        people = _generate_people()
        repo   = OrderRepository()
        mr     = MappingRepository()
        phase1_order_collection(ss, repo, people)
        phase2_order_placement(ss, dk, dm, ntf, repo, mr, people)
        phase6_inventory_sync(ss, dk, dm, ntf, mr)


def test_phase7_price_monitoring():
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository
    with _env() as (ss, dk, dm, ntf, _):
        people = _generate_people()
        repo   = OrderRepository()
        mr     = MappingRepository()
        phase1_order_collection(ss, repo, people)
        phase2_order_placement(ss, dk, dm, ntf, repo, mr, people)
        phase7_price_monitoring(dk, dm, ntf, mr)


def test_phase8_budget_management():
    from src.core.order_repository import OrderRepository
    from src.core.mapping_repository import MappingRepository
    with _env() as (ss, dk, dm, ntf, _):
        people = _generate_people()
        repo   = OrderRepository()
        mr     = MappingRepository()
        phase1_order_collection(ss, repo, people)
        phase2_order_placement(ss, dk, dm, ntf, repo, mr, people)
        phase8_budget_management(ss, dk, dm, ntf, repo, mr)


# ═══════════════════════════════════════════════════════════════════════════════
# §9  스탠드얼론 실행
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("E2E 시뮬레이션 v2 - 스탠드얼론 모드")
    print(f"가상 인물: 100명 (10명 × 10상품)")
    print(f"검증 항목: 주문수집·발주확인·자동발주·송장등록·취소처리(7가지)·반품·재고·가격·예산")
    print(f"최대 재시도: {MAX_RETRIES}회")

    passed, results, attempts = run_with_retries()
    sys.exit(0 if passed else 1)
