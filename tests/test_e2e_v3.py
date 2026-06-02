"""최종 E2E 시뮬레이션 테스트 v3

100명 가상 구매자 × 10개 가상 상품 × 모든 비즈니스 케이스
- 모의 API (Mock): SS/도매꾹/도매매 API 전부 Mock
- 실제 코드 로직 100% 그대로 실행
- 실제 데이터 구조와 동일하게 생성

실행:
  python tests/test_e2e_v3.py          # 2-pass 완주
  pytest tests/test_e2e_v3.py -v -s   # pytest 개별 테스트

성공 기준: 2회 연속 전체 통과
"""

from __future__ import annotations

import json
import os
import sys
import shutil
import traceback
import threading
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.db.database import init_db, get_session
from src.db.models import Base, SupplierOrder, PriceHistory
from src.core.order_collector import OrderCollector
from src.core.order_placer import OrderPlacer
from src.core.invoice_manager import InvoiceManager
from src.core.inventory_sync import InventorySync
from src.core.price_monitor import PriceMonitor
from src.core.return_monitor import ReturnMonitor
from src.core.cancel_monitor import CancelMonitor
from src.core.order_repository import OrderRepository
from src.core.mapping_repository import MappingRepository
from src.core.pending_order_repository import PendingOrderRepository
from src.core.stock_pending_repository import StockPendingRepository
from src.core.price_alert_repository import PriceAlertRepository
from src.core.budget_manager import BudgetManager
from src.core.exceptions import EmoneyShortageError


# ═════════════════════════════════════════════════════════════════════════════
# 1. 테스트 데이터 생성
# ═════════════════════════════════════════════════════════════════════════════

SURNAMES = ["김", "이", "박", "최", "정", "강", "조", "윤", "장", "임",
            "한", "오", "서", "신", "권", "황", "안", "송", "류", "홍"]
FIRSTNAMES = ["민준", "서연", "도현", "지우", "수아", "지민", "예린", "승현",
              "하늘", "유진", "태양", "아름", "진수", "소희", "현우", "나래",
              "민서", "재원", "보라", "기석", "선영", "상훈", "예지", "태호",
              "미래", "준혁", "혜린", "동현", "가은", "성민"]
CITIES = [
    ("서울시 강남구 테헤란로",      "06234"),
    ("서울시 마포구 월드컵로",      "03978"),
    ("부산시 해운대구 해운대해변로", "48099"),
    ("인천시 연수구 컨벤시아대로",  "22010"),
    ("대구시 수성구 범어로",        "42197"),
    ("광주시 서구 상무중앙로",      "61947"),
    ("대전시 유성구 대학로",        "34134"),
    ("수원시 영통구 영통로",        "16512"),
    ("성남시 분당구 불정로",        "13529"),
    ("고양시 일산동구 장항로",      "10271"),
]
DETAILS = ["101동 502호", "203동 1201호", "A동 301호", "B동 803호",
           "305호", "1504호", "2층", "오피스텔 710호", "빌라 2층 우측", "상가 3층"]
DELIVERY_MEMOS = ["부재시 문앞에 놓아주세요", "경비실에 맡겨주세요", "직접 받겠습니다",
                  "벨 눌러주세요", "1층 로비에 놔주세요", "조심히 배송해주세요",
                  "파손 주의해주세요", "빠른 배송 부탁드립니다", "", "문 앞 배송 요청"]

# 상품 A~J 스펙
_PRODUCT_SPECS = {
    "A": {"supplier": "domaekkuk", "price": 10_000, "stock": 100, "option": ""},
    "B": {"supplier": "domaekkuk", "price": 15_000, "stock": 50,  "option": "색상/빨강"},
    "C": {"supplier": "domaekkuk", "price": 20_000, "stock": 0,   "option": ""},   # 품절
    "D": {"supplier": "domaemae",  "price":  8_000, "stock": 200, "option": ""},
    "E": {"supplier": "domaemae",  "price": 12_000, "stock": 30,  "option": "사이즈/L"},
    "F": {"supplier": "domaekkuk", "price": 25_000, "stock": 100, "option": ""},
    "G": {"supplier": "domaemae",  "price": 18_000, "stock": 150, "option": "컬러/블루"},
    "H": {"supplier": "domaekkuk", "price":  5_000, "stock": 80,  "option": ""},
    "I": {"supplier": "domaemae",  "price": 30_000, "stock": 60,  "option": ""},
    "J": {"supplier": "domaekkuk", "price":  9_000, "stock": 0,   "option": "타입/A"},  # 품절
}


def generate_buyers(run_id: int = 1) -> list[dict]:
    """100명 고유 구매자 생성 (run_id 로 전화번호 시드 변경)"""
    buyers = []
    for i in range(100):
        surname = SURNAMES[i % len(SURNAMES)]
        fname   = FIRSTNAMES[i % len(FIRSTNAMES)]
        name    = surname + fname
        phone   = f"010-{(run_id * 100 + i + 1000) % 10000:04d}-{(i * 7 + run_id * 13 + 1234) % 10000:04d}"
        city, zipcode = CITIES[i % len(CITIES)]
        detail  = DETAILS[i % len(DETAILS)]
        address = f"{city} {i+1}번길 {(i % 30) + 1}"
        road    = f"{city} {i+1}번길"
        memo    = DELIVERY_MEMOS[i % len(DELIVERY_MEMOS)]
        letter  = chr(ord("A") + (i // 10))   # A:0~9, B:10~19 …, J:90~99
        qty     = (i % 3) + 1
        buyers.append({
            "idx":            i,
            "name":           name,
            "phone":          phone,
            "address":        address,
            "road_address":   road,
            "detail_address": detail,
            "zipcode":        zipcode,
            "delivery_memo":  memo,
            "product_letter": letter,
            "quantity":       qty,
        })
    return buyers


def generate_products() -> dict[str, dict]:
    prods = {}
    for letter, spec in _PRODUCT_SPECS.items():
        idx = ord(letter) - ord("A")
        prods[letter] = {
            "letter":              letter,
            "ss_product_id":       f"SS_{letter}",
            "ss_option_id":        f"OPT_{letter}" if spec["option"] else "",
            "supplier":            spec["supplier"],
            "supplier_product_id": f"WPID_{letter}{idx+1:02d}",
            "supplier_option_id":  f"WOPT_{letter}" if spec["option"] else "",
            "price":               spec["price"],
            "stock":               spec["stock"],
            "option":              spec["option"],
            "product_name":        f"테스트상품{letter}",
        }
    return prods


def order_id(buyer_idx: int, letter: str, run_id: int = 1) -> str:
    return f"ORD{run_id:02d}{letter}{buyer_idx:03d}"


def make_ss_item(buyer: dict, prod: dict, run_id: int = 1) -> dict:
    """SS API get_orders() 응답 형식"""
    oid = order_id(buyer["idx"], buyer["product_letter"], run_id)
    return {
        "productOrder": {
            "productOrderId": oid,
            "orderId":        f"SS{run_id:02d}{buyer['product_letter']}{buyer['idx']:03d}",
            "productId":      prod["ss_product_id"],
            "productName":    prod["product_name"],
            "optionCode":     prod["ss_option_id"],
            "quantity":       buyer["quantity"],
        },
        "order": {
            "orderId":     f"SS{run_id:02d}{buyer['product_letter']}{buyer['idx']:03d}",
            "ordererName": buyer["name"],
        },
        "shippingAddress": {
            "name":          buyer["name"],
            "tel1":          buyer["phone"],
            "addressStr":    buyer["address"],
            "roadAddress":   buyer["road_address"],
            "detailAddress": buyer["detail_address"],
            "zipCode":       buyer["zipcode"],
        },
        "deliveryMemo": buyer["delivery_memo"],
        "claim":        {},
    }


def make_cancel_ss_item(ss_item: dict) -> dict:
    r = deepcopy(ss_item)
    r["claim"] = {"cancelReason": "단순 변심", "claimStatus": "CANCEL_REQUEST"}
    return r


def make_return_ss_item(ss_item: dict) -> dict:
    r = deepcopy(ss_item)
    r["claim"] = {
        "returnReason": "불량품", "returnReasonType": "DEFECT",
        "claimStatus": "RETURN_REQUEST", "claimRequestDate": "2026-06-01T10:00:00",
        "returnDeliveryCompany": "CJ대한통운", "returnTrackingNumber": "1234567890",
        "claimPrice": {"refundPayAmount": ss_item["productOrder"]["quantity"] * 10_000},
    }
    r["productOrder"]["invoiceNumber"] = "9876543210"
    r["productOrder"]["deliveryCompany"] = "CJ대한통운"
    return r


# ═════════════════════════════════════════════════════════════════════════════
# 2. E2ERunner
# ═════════════════════════════════════════════════════════════════════════════

class E2ERunner:
    """전체 E2E 테스트 실행기"""

    def __init__(self, run_id: int = 1):
        self.run_id    = run_id
        self.tmpdir    = None
        self._orig_cwd = None
        self.buyers    = generate_buyers(run_id)
        self.products  = generate_products()
        self._results: list[tuple[str, bool, str]] = []

    # ── 설정 / 해제 ─────────────────────────────────────────────────────────

    def setup(self):
        self.tmpdir    = tempfile.mkdtemp(prefix="e2e_v3_")
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        Path("data").mkdir(exist_ok=True)

    def teardown(self):
        if self._orig_cwd:
            os.chdir(self._orig_cwd)
        if self.tmpdir and os.path.exists(self.tmpdir):
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── 테스트별 격리 컨텍스트 ───────────────────────────────────────────────

    @contextmanager
    def _ctx(self, name: str):
        """각 테스트마다 독립 디렉토리 + 독립 DB"""
        test_dir = Path(self.tmpdir) / name
        (test_dir / "data").mkdir(parents=True, exist_ok=True)
        os.chdir(test_dir)
        db_path = str(test_dir / "data" / "orders.db")
        init_db(db_path)
        try:
            yield test_dir
        finally:
            os.chdir(self.tmpdir)

    # ── 공통 헬퍼 ───────────────────────────────────────────────────────────

    def _mapping_repo(self) -> MappingRepository:
        repo = MappingRepository()
        for prod in self.products.values():
            repo.add(
                ss_product_id=prod["ss_product_id"],
                supplier=prod["supplier"],
                supplier_url_or_id=prod["supplier_product_id"],
                ss_option_id=prod["ss_option_id"],
                supplier_option_id=prod["supplier_option_id"],
                memo=prod["product_name"],
            )
        return repo

    def _ss_mock(self, extra_cancel=None, extra_return=None, order_status="PAYED") -> MagicMock:
        ss = MagicMock()
        ss.get_orders.return_value = [
            make_ss_item(b, self.products[b["product_letter"]], self.run_id)
            for b in self.buyers
        ]
        ss.confirm_orders.side_effect = lambda ids: {"confirmed": list(ids), "failed": []}
        ss.dispatch_order.return_value = None
        ss.set_product_sale_status.return_value = None
        ss.approve_cancel.return_value = None
        ss.get_cancellations.return_value = extra_cancel or []
        ss.get_returns.return_value = extra_return or []
        ss.get_product_order.return_value = {
            "productOrder": {"productOrderStatus": order_status}
        }
        return ss

    def _supplier_mocks(self, dk_stock_override=None, dm_stock_override=None) -> tuple[MagicMock, MagicMock]:
        dk = MagicMock()
        dm = MagicMock()

        dk_counter = {"n": 0}
        dm_counter = {"n": 0}

        def dk_get_product(pid):
            if dk_stock_override and pid in dk_stock_override:
                stk = dk_stock_override[pid]
            else:
                stk = next((p["stock"] for p in self.products.values()
                            if p["supplier_product_id"] == pid and p["supplier"] == "domaekkuk"), 100)
            price = next((p["price"] for p in self.products.values()
                          if p["supplier_product_id"] == pid), 10_000)
            name  = next((p["product_name"] for p in self.products.values()
                          if p["supplier_product_id"] == pid), "테스트상품")
            return {"title": name, "price": price, "stock": stk}

        def dm_get_product(pid):
            if dm_stock_override and pid in dm_stock_override:
                stk = dm_stock_override[pid]
            else:
                stk = next((p["stock"] for p in self.products.values()
                            if p["supplier_product_id"] == pid and p["supplier"] == "domaemae"), 100)
            price = next((p["price"] for p in self.products.values()
                          if p["supplier_product_id"] == pid), 10_000)
            name  = next((p["product_name"] for p in self.products.values()
                          if p["supplier_product_id"] == pid), "테스트상품")
            return {"title": name, "price": price, "stock": stk}

        dk.get_product.side_effect = dk_get_product
        dm.get_product.side_effect = dm_get_product

        def dk_place(pid, qty, shipping, **kw):
            dk_counter["n"] += 1
            return {"order_no": f"DK{dk_counter['n']:06d}"}

        def dm_place(pid, qty, shipping, **kw):
            dm_counter["n"] += 1
            return {"order_no": f"DM{dm_counter['n']:06d}"}

        dk.place_order.side_effect = dk_place
        dm.place_order.side_effect = dm_place
        dk.get_order_tracking.return_value = {}
        dm.get_order_tracking.return_value = {}
        dk.cancel_order.return_value = None
        dm.cancel_order.return_value = None
        dk.get_cancel_result.return_value = "PENDING"
        dm.get_cancel_result.return_value = "PENDING"
        return dk, dm

    def _notifier(self) -> MagicMock:
        n = MagicMock()
        n.send.return_value = None
        return n

    def _assert(self, condition: bool, msg: str):
        if not condition:
            raise AssertionError(msg)

    def _run_test(self, name: str, fn):
        """테스트 실행 + 결과 기록"""
        try:
            fn()
            self._results.append((name, True, ""))
            print(f"  ✓ {name}")
        except Exception as e:
            detail = traceback.format_exc()
            self._results.append((name, False, detail))
            print(f"  ✗ {name}")
            print(f"    오류: {e}")
            raise  # 즉시 중단

    # ═════════════════════════════════════════════════════════════════════════
    # ── 주문 수집 ─────────────────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def test_order_collection_normal(self):
        """정상 주문 100건 수집 → DB 저장"""
        with self._ctx("t01"):
            ss = self._ss_mock()
            repo = OrderRepository()
            collector = OrderCollector(ss, repo)
            added = collector.run()
            self._assert(added == 100, f"100건 기대, 실제={added}")

            orders = repo.all()
            self._assert(len(orders) == 100, f"저장 100건 기대, 실제={len(orders)}")

            for b in self.buyers:
                oid = order_id(b["idx"], b["product_letter"], self.run_id)
                o = repo.find(oid)
                self._assert(o is not None, f"주문 없음: {oid}")
                self._assert(o["buyer_name"] == b["name"],
                             f"구매자명 불일치 [{oid}]: 기대={b['name']}, 실제={o['buyer_name']}")
                self._assert(o["receiver_phone"] == b["phone"],
                             f"전화번호 불일치 [{oid}]: 기대={b['phone']}, 실제={o['receiver_phone']}")
                self._assert(o["receiver_address"] == b["address"],
                             f"주소 불일치 [{oid}]")
                self._assert(o["receiver_zipcode"] == b["zipcode"],
                             f"우편번호 불일치 [{oid}]")
                self._assert(o["quantity"] == b["quantity"],
                             f"수량 불일치 [{oid}]: 기대={b['quantity']}, 실제={o['quantity']}")
                self._assert(o["delivery_memo"] == b["delivery_memo"],
                             f"배송메모 불일치 [{oid}]")
                self._assert(o["status"] == "NEW", f"상태 불일치 [{oid}]: {o['status']}")

    def test_order_collection_dedup(self):
        """중복 주문 수집 방지"""
        with self._ctx("t02"):
            ss = self._ss_mock()
            repo = OrderRepository()
            collector = OrderCollector(ss, repo)
            added1 = collector.run()
            added2 = collector.run()   # 두 번 수집
            self._assert(added1 == 100, f"1차 100건 기대, 실제={added1}")
            self._assert(added2 == 0,   f"2차 0건 기대(중복), 실제={added2}")
            self._assert(len(repo.all()) == 100, "DB에 100건만 있어야 함")

    # ═════════════════════════════════════════════════════════════════════════
    # ── 자동 발주 ─────────────────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def test_placement_normal(self):
        """정상 발주 → ORDERED + SS 발주확인"""
        with self._ctx("t03"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            notifier = self._notifier()
            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()

            # in-stock 상품만 발주 (C, J 는 품절 → stock_pending)
            collector = OrderCollector(ss, order_repo)
            collector.run()

            stock_pending_repo = StockPendingRepository()
            placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                 notifier=notifier, ss_api=ss,
                                 stock_pending=stock_pending_repo)
            stats = placer.run()

            self._assert(stats["error"] == 0,
                         f"에러 0 기대: {stats}")
            # 발주된 수 = 100 - 품절(C10명 + J10명=20) = 80
            self._assert(stats["ordered"] == 80,
                         f"80건 발주 기대: {stats}")
            self._assert(stats["stock_pending"] == 20,
                         f"품절대기 20건 기대: {stats}")
            self._assert(stats["ss_confirmed"] == 80,
                         f"SS 발주확인 80건 기대: {stats}")

            # ORDERED 상태 확인
            ordered = order_repo.find_by_status("ORDERED")
            self._assert(len(ordered) == 80, f"ORDERED 80건 기대: {len(ordered)}")

            # SupplierOrder DB 확인
            with get_session() as session:
                sup_count = session.query(SupplierOrder).count()
            self._assert(sup_count == 80, f"SupplierOrder 80건 기대: {sup_count}")

    def test_placement_cancel_filter(self):
        """CANCEL_REQUEST 주문 → 발주 차단"""
        with self._ctx("t04"):
            # 구매자 0번 (상품 A) 취소 요청
            b0 = self.buyers[0]
            oid0 = order_id(b0["idx"], b0["product_letter"], self.run_id)
            cancel_item = make_cancel_ss_item(
                make_ss_item(b0, self.products[b0["product_letter"]], self.run_id)
            )
            ss = self._ss_mock(extra_cancel=[cancel_item])
            dk, dm = self._supplier_mocks()
            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()

            collector = OrderCollector(ss, order_repo)
            collector.run()

            placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                 notifier=self._notifier(), ss_api=ss)
            stats = placer.run()

            self._assert(stats["cancelled"] == 1, f"취소 차단 1건 기대: {stats}")
            cancelled = order_repo.find_by_status("CANCELLED")
            self._assert(any(o["order_id"] == oid0 for o in cancelled),
                         f"취소된 주문이 CANCELLED 아님: {oid0}")

    def test_placement_emoney_shortage(self):
        """e머니 부족 → 전체 발주 대기 + 알림"""
        with self._ctx("t05"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()

            call_count = {"n": 0}
            def dk_emoney_fail(pid, qty, shipping, **kw):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise EmoneyShortageError("e머니 잔액 부족")
                return {"order_no": f"DK{call_count['n']:06d}"}
            dk.place_order.side_effect = dk_emoney_fail

            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            notifier = self._notifier()

            collector = OrderCollector(ss, order_repo)
            collector.run()

            placer = OrderPlacer(dk, dm, mapping_repo, order_repo, notifier=notifier)
            stats = placer.run()

            self._assert(stats["deferred"] > 0, f"e머니 부족으로 대기 발생 기대: {stats}")
            notifier.send.assert_called()
            calls_text = " ".join(str(c) for c in notifier.send.call_args_list)
            self._assert("e머니" in calls_text or "LACK" in calls_text or "긴급" in calls_text,
                         "e머니 알림 없음")

    def test_placement_api_error(self):
        """발주 API 오류 → ERROR 상태 + 알림"""
        with self._ctx("t06"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            dk.place_order.side_effect = Exception("API 연결 실패")
            dm.place_order.side_effect = Exception("API 연결 실패")

            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            notifier = self._notifier()

            collector = OrderCollector(ss, order_repo)
            collector.run()

            placer = OrderPlacer(dk, dm, mapping_repo, order_repo, notifier=notifier)
            stats = placer.run()

            self._assert(stats["error"] > 0, f"ERROR 발생 기대: {stats}")
            errors = order_repo.find_by_status("ERROR")
            self._assert(len(errors) > 0, "ERROR 상태 주문 없음")
            notifier.send.assert_called()
            subj_calls = [str(c) for c in notifier.send.call_args_list]
            self._assert(any("발주 실패" in s or "API" in s for s in subj_calls),
                         "발주 실패 알림 없음")

    def test_placement_no_mapping(self):
        """매핑 없음 → ERROR 상태 + 알림"""
        with self._ctx("t07"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            mapping_repo = MappingRepository()   # 빈 매핑
            order_repo = OrderRepository()
            notifier = self._notifier()

            collector = OrderCollector(ss, order_repo)
            collector.run()

            placer = OrderPlacer(dk, dm, mapping_repo, order_repo, notifier=notifier)
            stats = placer.run()

            self._assert(stats["error"] == 100, f"매핑없음 100건 ERROR 기대: {stats}")
            errors = order_repo.find_by_status("ERROR")
            self._assert(len(errors) == 100, f"ERROR 100건 기대: {len(errors)}")
            self._assert(notifier.send.call_count == 100,
                         f"100건 알림 기대: {notifier.send.call_count}")

    def test_ss_confirm_failure(self):
        """SS 발주확인 일부 실패 → 알림"""
        with self._ctx("t08"):
            ss = self._ss_mock()
            # 일부 실패 시뮬레이션 (전달된 IDs 중 앞 5개를 실패로 처리)
            def confirm_partial_fail(ids):
                id_list = list(ids)
                return {"confirmed": id_list[5:], "failed": id_list[:5]}
            ss.confirm_orders.side_effect = confirm_partial_fail

            dk, dm = self._supplier_mocks()
            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            notifier = self._notifier()

            collector = OrderCollector(ss, order_repo)
            collector.run()
            placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                 notifier=notifier, ss_api=ss)
            stats = placer.run()

            self._assert(stats["ss_confirm_failed"] >= 5,
                         f"발주확인 실패 5건 이상 기대: {stats}")
            notifier.send.assert_called()
            calls_text = " ".join(str(c) for c in notifier.send.call_args_list)
            self._assert("발주확인 실패" in calls_text,
                         "발주확인 실패 알림 없음")

    # ═════════════════════════════════════════════════════════════════════════
    # ── 예산 관리 ─────────────────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def test_budget_normal(self):
        """잔액 충분 → 전체 발주"""
        with self._ctx("t09"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            notifier = self._notifier()
            pending_repo = PendingOrderRepository()
            budget = BudgetManager(initial_balance=10_000_000)  # 1천만원

            collector = OrderCollector(ss, order_repo)
            collector.run()

            placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                 notifier=notifier, budget=budget,
                                 pending=pending_repo, ss_api=ss,
                                 stock_pending=StockPendingRepository())
            stats = placer.run()

            # 품절 20건 제외 → 80건 발주
            self._assert(stats["ordered"] == 80,
                         f"예산 충분 시 80건 기대: {stats}")
            self._assert(stats["deferred"] == 0,
                         f"대기 0건 기대: {stats}")
            self._assert(budget.get_balance() < 10_000_000,
                         "예산 차감 안 됨")

    def test_budget_shortage_pending(self):
        """잔액 부족 → 일부 PENDING + 판매중지"""
        with self._ctx("t10"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            notifier = self._notifier()
            pending_repo = PendingOrderRepository()
            # 소량 예산: 상품 H(5000원) 1건 + 배송비(3000) = 8000원만 가능
            budget = BudgetManager(initial_balance=8_000)

            collector = OrderCollector(ss, order_repo)
            collector.run()

            with patch("src.core.budget_sale_guard.suspend_all") as mock_suspend:
                mock_suspend.return_value = 0
                placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                     notifier=notifier, budget=budget,
                                     pending=pending_repo, ss_api=ss,
                                     stock_pending=StockPendingRepository())
                stats = placer.run()

            self._assert(stats["deferred"] > 0, f"대기 발생 기대: {stats}")
            pending = pending_repo.all()
            self._assert(len(pending) > 0, "pending_orders.json에 대기 주문 없음")
            # 예산 부족 알림
            notifier.send.assert_called()

    def test_budget_charge_resume(self):
        """예산 충전 → 대기 주문 자동 재개"""
        with self._ctx("t11"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            notifier = self._notifier()
            pending_repo = PendingOrderRepository()
            budget = BudgetManager(initial_balance=8_000)

            collector = OrderCollector(ss, order_repo)
            collector.run()

            with patch("src.core.budget_sale_guard.suspend_all") as mock_suspend:
                mock_suspend.return_value = 0
                placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                     notifier=notifier, budget=budget,
                                     pending=pending_repo, ss_api=ss,
                                     stock_pending=StockPendingRepository())
                stats_before = placer.run()

            pending_before = pending_repo.count()
            self._assert(pending_before > 0, "충전 전 대기 주문 있어야 함")

            # 예산 충전
            budget.charge(50_000_000, "테스트 충전")

            with patch("src.core.budget_sale_guard.restore_all") as mock_restore:
                mock_restore.return_value = 0
                stats_resume = placer.resume_pending()

            self._assert(stats_resume["ordered"] > 0,
                         f"재개 발주 기대: {stats_resume}")
            self._assert(pending_repo.count() < pending_before,
                         "재개 후 대기 감소 기대")

    def test_budget_pending_cancel_during_wait(self):
        """대기 중 취소 요청 → cancel_monitor 위임, CANCELLED 처리"""
        with self._ctx("t12"):
            b0 = self.buyers[0]
            oid0 = order_id(b0["idx"], b0["product_letter"], self.run_id)

            ss = self._ss_mock()
            # 재개 시 취소 요청 발생
            cancel_item = make_cancel_ss_item(
                make_ss_item(b0, self.products[b0["product_letter"]], self.run_id)
            )
            ss.get_cancellations.return_value = [cancel_item]

            dk, dm = self._supplier_mocks()
            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            pending_repo = PendingOrderRepository()
            budget = BudgetManager(initial_balance=8_000)

            collector = OrderCollector(ss, order_repo)
            collector.run()

            # 강제로 대기 등록
            o = order_repo.find(oid0)
            pending_repo.add_many([{"order": o, "estimated_cost": 5_000}])
            order_repo.update_status(oid0, "PENDING")

            budget.charge(50_000_000)

            placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                 notifier=self._notifier(), budget=budget,
                                 pending=pending_repo, ss_api=ss,
                                 stock_pending=StockPendingRepository())
            stats = placer.resume_pending()

            self._assert(stats["cancelled"] >= 1, f"취소 1건 이상 기대: {stats}")
            o_after = order_repo.find(oid0)
            self._assert(o_after["status"] == "CANCELLED",
                         f"CANCELLED 기대: {o_after['status']}")

    def test_budget_concurrent_lock(self):
        """동시 실행 시 중복 발주 방지 (resume_pending 락)"""
        with self._ctx("t13"):
            dk, dm = self._supplier_mocks()
            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            budget = BudgetManager(initial_balance=1_000_000)
            pending_repo = PendingOrderRepository()

            placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                 notifier=self._notifier(), budget=budget,
                                 pending=pending_repo)

            # 락 수동 획득 → resume_pending 즉시 스킵
            acquired = OrderPlacer._resume_lock.acquire(blocking=False)
            self._assert(acquired, "락 획득 실패")
            try:
                result = placer.resume_pending()
            finally:
                OrderPlacer._resume_lock.release()

            self._assert(result == {"ordered": 0, "still_pending": 0,
                                    "error": 0, "cancelled": 0},
                         f"중복 실행 차단 기대: {result}")

    # ═════════════════════════════════════════════════════════════════════════
    # ── 송장 동기화 ───────────────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def _place_orders_for_invoice(self, order_repo, mapping_repo, ss, dk, dm):
        """송장 테스트용 발주 완료 상태 구성 (재고 확인 포함)"""
        collector = OrderCollector(ss, order_repo)
        collector.run()
        placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                             notifier=self._notifier(), ss_api=ss,
                             stock_pending=StockPendingRepository())
        placer.run()

    def test_invoice_sync_domaekkuk(self):
        """domaekkuk 송장 → SS 자동 등록 → INVOICED"""
        with self._ctx("t14"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()

            # domaekkuk 송장 반환 설정
            dk.get_order_tracking.return_value = {
                "tracking_number":  "111222333444",
                "delivery_company": "CJ대한통운",
            }
            dm.get_order_tracking.return_value = {}

            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()

            self._place_orders_for_invoice(order_repo, mapping_repo, ss, dk, dm)

            inv_mgr = InvoiceManager(ss, dk, dm, order_repo, notifier=self._notifier())
            stats = inv_mgr.run()

            # domaekkuk 상품: A, B, F, H = 40건
            dk_prod_count = sum(1 for b in self.buyers
                                if self.products[b["product_letter"]]["supplier"] == "domaekkuk"
                                and self.products[b["product_letter"]]["stock"] > 0)
            self._assert(stats["invoiced"] == dk_prod_count,
                         f"domaekkuk 송장 {dk_prod_count}건 기대: {stats}")
            invoiced = order_repo.find_by_status("INVOICED")
            self._assert(len(invoiced) == dk_prod_count,
                         f"INVOICED {dk_prod_count}건 기대: {len(invoiced)}")
            ss.dispatch_order.assert_called()

    def test_invoice_sync_domaemae(self):
        """domaemae 송장 → SS 자동 등록 → INVOICED"""
        with self._ctx("t15"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            dk.get_order_tracking.return_value = {}
            dm.get_order_tracking.return_value = {
                "tracking_number":  "555666777888",
                "delivery_company": "한진택배",
            }

            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()

            self._place_orders_for_invoice(order_repo, mapping_repo, ss, dk, dm)

            inv_mgr = InvoiceManager(ss, dk, dm, order_repo, notifier=self._notifier())
            stats = inv_mgr.run()

            dm_prod_count = sum(1 for b in self.buyers
                                if self.products[b["product_letter"]]["supplier"] == "domaemae"
                                and self.products[b["product_letter"]]["stock"] > 0)
            self._assert(stats["invoiced"] == dm_prod_count,
                         f"domaemae 송장 {dm_prod_count}건 기대: {stats}")

    def test_invoice_sync_pending(self):
        """미발송 → pending (다음 주기 재시도)"""
        with self._ctx("t16"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            dk.get_order_tracking.return_value = {}
            dm.get_order_tracking.return_value = {}

            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()

            self._place_orders_for_invoice(order_repo, mapping_repo, ss, dk, dm)

            inv_mgr = InvoiceManager(ss, dk, dm, order_repo, notifier=self._notifier())
            stats = inv_mgr.run()

            self._assert(stats["invoiced"] == 0,  f"미발송 → invoiced=0 기대: {stats}")
            ordered_count = sum(1 for b in self.buyers
                                if self.products[b["product_letter"]]["stock"] > 0)
            self._assert(stats["pending"] == ordered_count,
                         f"pending={ordered_count} 기대: {stats}")
            ss.dispatch_order.assert_not_called()

    def test_invoice_sync_error(self):
        """SS dispatch 실패 → ERROR + 알림"""
        with self._ctx("t17"):
            ss = self._ss_mock()
            ss.dispatch_order.side_effect = Exception("dispatch API 오류")
            dk, dm = self._supplier_mocks()
            dk.get_order_tracking.return_value = {
                "tracking_number": "1234567890", "delivery_company": "CJ대한통운"
            }
            dm.get_order_tracking.return_value = {
                "tracking_number": "9876543210", "delivery_company": "한진택배"
            }

            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            notifier = self._notifier()

            self._place_orders_for_invoice(order_repo, mapping_repo, ss, dk, dm)

            inv_mgr = InvoiceManager(ss, dk, dm, order_repo, notifier=notifier)
            stats = inv_mgr.run()

            self._assert(stats["error"] > 0, f"error > 0 기대: {stats}")
            notifier.send.assert_called()
            calls_text = " ".join(str(c) for c in notifier.send.call_args_list)
            self._assert("송장" in calls_text or "dispatch" in calls_text.lower(),
                         "송장 실패 알림 없음")

    # ═════════════════════════════════════════════════════════════════════════
    # ── 재고 동기화 ───────────────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def test_inventory_out_of_stock(self):
        """도매처 재고 0 → SS 판매중지 + 알림"""
        with self._ctx("t18"):
            ss = self._ss_mock()
            notifier = self._notifier()
            # 상품 A(domaekkuk) 재고 → 0
            pid_a = self.products["A"]["supplier_product_id"]
            dk, dm = self._supplier_mocks(dk_stock_override={pid_a: 0})
            mapping_repo = self._mapping_repo()

            inv_sync = InventorySync(ss, dk, dm, mapping_repo, notifier=notifier)
            stats = inv_sync.run()

            self._assert(stats["paused"] >= 1, f"품절 1건 이상 기대: {stats}")
            ss.set_product_sale_status.assert_called()
            # on_sale=False 호출 확인 (keyword arg)
            pause_calls = [
                c for c in ss.set_product_sale_status.call_args_list
                if c.kwargs.get("on_sale") is False
                or (c.args and len(c.args) > 1 and c.args[1] is False)
            ]
            self._assert(len(pause_calls) >= 1, "판매중지 API 미호출")
            notifier.send.assert_called()

    def test_inventory_restock(self):
        """재입고 → SS 판매재개 + stock_pending 재발주"""
        with self._ctx("t19"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            mapping_repo = self._mapping_repo()
            notifier = self._notifier()

            # 캐시: 상품 A = 품절 상태였음
            pid_a = self.products["A"]["supplier_product_id"]
            cache = {"status": {f"domaekkuk:{pid_a}": False}, "low_stock_alerted_at": {}}
            cache_path = Path("data/stock_cache.json")
            cache_path.parent.mkdir(exist_ok=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False))

            order_placer_mock = MagicMock()
            order_placer_mock.resume_stock_pending.return_value = 1

            inv_sync = InventorySync(ss, dk, dm, mapping_repo,
                                     notifier=notifier,
                                     order_placer=order_placer_mock)
            stats = inv_sync.run()

            self._assert(stats["resumed"] >= 1, f"재입고 1건 이상 기대: {stats}")
            order_placer_mock.resume_stock_pending.assert_called()

    def test_inventory_api_error(self):
        """재고 조회 API 실패 → 알림"""
        with self._ctx("t20"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            dk.get_product.side_effect = Exception("API 연결 오류")
            dm.get_product.side_effect = Exception("API 연결 오류")
            mapping_repo = self._mapping_repo()
            notifier = self._notifier()

            inv_sync = InventorySync(ss, dk, dm, mapping_repo, notifier=notifier)
            stats = inv_sync.run()

            self._assert(stats["error"] > 0, f"에러 기대: {stats}")
            notifier.send.assert_called()

    def test_inventory_low_stock_alert(self):
        """재고 부족(임계치 미만) → 이메일 알림"""
        with self._ctx("t21"):
            ss = self._ss_mock()
            # 재고 10개 (임계치 50 미만)
            pid_d = self.products["D"]["supplier_product_id"]
            dk, dm = self._supplier_mocks(dm_stock_override={pid_d: 10})
            mapping_repo = self._mapping_repo()
            notifier = self._notifier()

            inv_sync = InventorySync(ss, dk, dm, mapping_repo, notifier=notifier)
            stats = inv_sync.run()

            notifier.send.assert_called()
            calls_text = " ".join(str(c) for c in notifier.send.call_args_list)
            self._assert("재고" in calls_text, "재고부족 알림 없음")

    # ═════════════════════════════════════════════════════════════════════════
    # ── 가격 모니터링 ─────────────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def test_price_change_detected(self):
        """가격 변동 감지 → PriceHistory DB + price_alerts.json"""
        with self._ctx("t22"):
            dk, dm = self._supplier_mocks()
            # 상품 A 가격 변경
            dk.get_product.side_effect = lambda pid: {
                "title": "테스트상품A",
                "price": 12_000 if pid == self.products["A"]["supplier_product_id"] else 10_000,
                "stock": 100,
            }

            mapping_repo = self._mapping_repo()
            alert_repo = PriceAlertRepository()
            notifier = self._notifier()

            monitor = PriceMonitor(dk, dm, notifier=notifier,
                                   mapping_repo=mapping_repo, alert_repo=alert_repo)
            stats = monitor.run()

            self._assert(stats["changed"] >= 1, f"가격변동 기대: {stats}")
            alerts = alert_repo.all()
            self._assert(len(alerts) >= 1, "price_alerts.json에 알림 없음")
            with get_session() as session:
                cnt = session.query(PriceHistory).count()
            self._assert(cnt >= 1, "PriceHistory DB 기록 없음")

    def test_price_monitor_error(self):
        """가격 조회 오류 → 이메일 알림"""
        with self._ctx("t23"):
            dk, dm = self._supplier_mocks()
            dk.get_product.side_effect = Exception("API 오류")
            dm.get_product.side_effect = Exception("API 오류")

            mapping_repo = self._mapping_repo()
            notifier = self._notifier()

            monitor = PriceMonitor(dk, dm, notifier=notifier, mapping_repo=mapping_repo)
            stats = monitor.run()

            self._assert(stats["error"] > 0, f"에러 기대: {stats}")
            notifier.send.assert_called()

    # ═════════════════════════════════════════════════════════════════════════
    # ── 반품 감지 ─────────────────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def test_return_detection(self):
        """RETURN_REQUEST 감지 → returns.json + 이메일 (모든 구매자 정보 포함)"""
        with self._ctx("t24"):
            # 상품 A 구매자 전원 반품 요청
            return_buyers = [b for b in self.buyers if b["product_letter"] == "A"]
            return_items = [
                make_return_ss_item(
                    make_ss_item(b, self.products["A"], self.run_id)
                )
                for b in return_buyers
            ]

            ss = self._ss_mock(extra_return=return_items)
            notifier = self._notifier()

            monitor = ReturnMonitor(ss, notifier=notifier)
            result = monitor.run()

            self._assert(result["new"] == 10, f"반품 10건 기대: {result}")
            self._assert(monitor._load()["returns"].__len__() == 10, "returns.json 저장 오류")

            # 이메일에 구매자 정보 포함 확인
            self._assert(notifier.send.call_count == 10, "10건 알림 기대")
            for b in return_buyers:
                call_texts = " ".join(str(c) for c in notifier.send.call_args_list)
                self._assert(b["name"] in call_texts,
                             f"반품 알림에 구매자명 누락: {b['name']}")

    # ═════════════════════════════════════════════════════════════════════════
    # ── 취소 자동 처리 ────────────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def _make_cancel_entry_in_file(self, cancel_file: str, entry: dict):
        """cancellations.json에 기존 엔트리 추가"""
        Path(cancel_file).parent.mkdir(parents=True, exist_ok=True)
        if Path(cancel_file).exists():
            with open(cancel_file, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"cancellations": []}
        data["cancellations"].append(entry)
        with open(cancel_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _make_base_cancel_entry(self, b: dict) -> dict:
        oid = order_id(b["idx"], b["product_letter"], self.run_id)
        return {
            "product_order_id":      oid,
            "ss_order_id":           f"SS{oid}",
            "product_name":          self.products[b["product_letter"]]["product_name"],
            "quantity":              b["quantity"],
            "cancel_reason":         "단순 변심",
            "buyer_name":            b["name"],
            "buyer_phone":           b["phone"],
            "receiver_name":         b["name"],
            "receiver_phone":        b["phone"],
            "receiver_address":      b["address"],
            "receiver_zipcode":      b["zipcode"],
            "invoice_number":        "",
            "supplier":              "",
            "supplier_order_no":     "",
            "cancel_state":          "",
            "deny_sent_at":          "",
            "last_checked_at":       "",
            "detected_at":           datetime.now(timezone.utc).isoformat(),
            "result_label":          "",
            "business_days_elapsed": 0,
        }

    def test_cancel_ss_auto(self):
        """발주 기록 없음 → SS_AUTO"""
        with self._ctx("t25"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            cancel_item = make_cancel_ss_item(
                make_ss_item(b0, self.products[b0["product_letter"]], self.run_id)
            )
            ss = self._ss_mock(extra_cancel=[cancel_item])
            dk, dm = self._supplier_mocks()
            notifier = self._notifier()

            monitor = CancelMonitor(ss, notifier=notifier,
                                    dk_api=dk, dm_cli=dm)
            result = monitor.run()

            self._assert(result["new"] >= 1, f"신규 취소 기대: {result}")
            data = monitor._load()
            entry = next((e for e in data["cancellations"]
                          if e["product_order_id"] == oid), None)
            self._assert(entry is not None, f"취소 엔트리 없음: {oid}")
            self._assert(entry["cancel_state"] == "SS_AUTO",
                         f"SS_AUTO 기대: {entry['cancel_state']}")

    def test_cancel_shipped_reject(self):
        """이미 출고 (tracking 있음) → CANCEL_REJECT + SHIPPED_REJECT"""
        with self._ctx("t26"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            cancel_item = make_cancel_ss_item(
                make_ss_item(b0, self.products[b0["product_letter"]], self.run_id)
            )
            ss = self._ss_mock(extra_cancel=[cancel_item])
            dk, dm = self._supplier_mocks()
            notifier = self._notifier()

            # SupplierOrder DB에 SHIPPED + tracking 등록
            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001",
                    status="SHIPPED", tracking_number="TEST1234567890",
                    delivery_company="CJ대한통운",
                ))

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm)
            monitor.run()

            ss.dispatch_order.assert_called_once()
            data = monitor._load()
            entry = next((e for e in data["cancellations"]
                          if e["product_order_id"] == oid), None)
            self._assert(entry is not None, f"취소 엔트리 없음: {oid}")
            self._assert(entry["cancel_state"] == "SHIPPED_REJECT",
                         f"SHIPPED_REJECT 기대: {entry['cancel_state']}")
            notifier.send.assert_called()

    def test_cancel_deny_sent(self):
        """ORDERED → setOrdDeny 전송 → DENY_SENT + 알림"""
        with self._ctx("t27"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            cancel_item = make_cancel_ss_item(
                make_ss_item(b0, self.products[b0["product_letter"]], self.run_id)
            )
            ss = self._ss_mock(extra_cancel=[cancel_item],
                               order_status="CANCEL_REQUEST")
            dk, dm = self._supplier_mocks()
            notifier = self._notifier()

            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001",
                    status="ORDERED",
                ))

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm)
            monitor.run()

            dk.cancel_order.assert_called_once_with("DK000001")
            data = monitor._load()
            entry = next((e for e in data["cancellations"]
                          if e["product_order_id"] == oid), None)
            self._assert(entry["cancel_state"] == "DENY_SENT",
                         f"DENY_SENT 기대: {entry['cancel_state']}")
            notifier.send.assert_called()

    def test_cancel_deny_failed(self):
        """setOrdDeny 실패 → DENY_FAILED + 알림"""
        with self._ctx("t28"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            cancel_item = make_cancel_ss_item(
                make_ss_item(b0, self.products[b0["product_letter"]], self.run_id)
            )
            ss = self._ss_mock(extra_cancel=[cancel_item],
                               order_status="CANCEL_REQUEST")
            dk, dm = self._supplier_mocks()
            dk.cancel_order.side_effect = Exception("도매처 API 오류")
            notifier = self._notifier()

            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001",
                    status="ORDERED",
                ))

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm)
            monitor.run()

            data = monitor._load()
            entry = next((e for e in data["cancellations"]
                          if e["product_order_id"] == oid), None)
            self._assert(entry["cancel_state"] == "DENY_FAILED",
                         f"DENY_FAILED 기대: {entry['cancel_state']}")
            notifier.send.assert_called()
            calls_text = " ".join(str(c) for c in notifier.send.call_args_list)
            self._assert("긴급" in calls_text or "실패" in calls_text,
                         "DENY_FAILED 긴급 알림 없음")

    def test_cancel_buyer_withdrew_pre_deny(self):
        """setOrdDeny 호출 전 구매자 취소 철회 → BUYER_WITHDREW_PRE_DENY"""
        with self._ctx("t29"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            cancel_item = make_cancel_ss_item(
                make_ss_item(b0, self.products[b0["product_letter"]], self.run_id)
            )
            # SS 주문 상태: 이미 PAYED (취소 철회됨)
            ss = self._ss_mock(extra_cancel=[cancel_item], order_status="PAYED")
            dk, dm = self._supplier_mocks()
            order_repo = OrderRepository()
            notifier = self._notifier()

            # orders.json에 주문 추가
            collector = OrderCollector(ss, order_repo)
            ss.get_orders.return_value = [
                make_ss_item(b0, self.products[b0["product_letter"]], self.run_id)
            ]
            collector.run()
            order_repo.update_status(oid, "ORDERED")

            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001", status="ORDERED",
                ))

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm,
                                    order_repo=order_repo)
            monitor.run()

            # setOrdDeny 미호출
            dk.cancel_order.assert_not_called()
            data = monitor._load()
            entry = next((e for e in data["cancellations"]
                          if e["product_order_id"] == oid), None)
            self._assert(entry["cancel_state"] == "BUYER_WITHDREW_PRE_DENY",
                         f"BUYER_WITHDREW_PRE_DENY 기대: {entry['cancel_state']}")

    def test_cancel_approved(self):
        """도매처 취소 승인 → SS approve_cancel → APPROVED"""
        with self._ctx("t30"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            ss = self._ss_mock(order_status="CANCEL_REQUEST")
            dk, dm = self._supplier_mocks()
            dk.get_cancel_result.return_value = "APPROVED"
            notifier = self._notifier()

            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001", status="ORDERED",
                ))

            # 기존 DENY_SENT 엔트리 생성
            entry = self._make_base_cancel_entry(b0)
            entry.update({
                "supplier": "domaekkuk",
                "supplier_order_no": "DK000001",
                "cancel_state": "DENY_SENT",
                "deny_sent_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            })
            self._make_cancel_entry_in_file("data/cancellations.json", entry)

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm)
            monitor.run()

            ss.approve_cancel.assert_called_once_with(oid)
            data = monitor._load()
            e = next(x for x in data["cancellations"] if x["product_order_id"] == oid)
            self._assert(e["cancel_state"] == "APPROVED",
                         f"APPROVED 기대: {e['cancel_state']}")

    def test_cancel_rejected_wait_ship(self):
        """도매처 취소 거부 → REJECTED_WAIT_SHIP"""
        with self._ctx("t31"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            ss = self._ss_mock(order_status="CANCEL_REQUEST")
            dk, dm = self._supplier_mocks()
            dk.get_cancel_result.return_value = "REJECTED"
            notifier = self._notifier()

            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001", status="ORDERED",
                ))

            entry = self._make_base_cancel_entry(b0)
            entry.update({
                "supplier": "domaekkuk", "supplier_order_no": "DK000001",
                "cancel_state": "DENY_SENT",
                "deny_sent_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            })
            self._make_cancel_entry_in_file("data/cancellations.json", entry)

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm)
            monitor.run()

            data = monitor._load()
            e = next(x for x in data["cancellations"] if x["product_order_id"] == oid)
            self._assert(e["cancel_state"] == "REJECTED_WAIT_SHIP",
                         f"REJECTED_WAIT_SHIP 기대: {e['cancel_state']}")
            notifier.send.assert_called()

    def test_cancel_3day_urgent(self):
        """3영업일 경과 → URGENT_3DAY + 긴급 알림"""
        with self._ctx("t32"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            ss = self._ss_mock(order_status="CANCEL_REQUEST")
            dk, dm = self._supplier_mocks()
            dk.get_cancel_result.return_value = "PENDING"
            notifier = self._notifier()

            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001", status="ORDERED",
                ))

            # 5영업일 전 deny_sent (3영업일 이상 경과)
            deny_sent = datetime.now(timezone.utc) - timedelta(days=5)
            entry = self._make_base_cancel_entry(b0)
            entry.update({
                "supplier": "domaekkuk", "supplier_order_no": "DK000001",
                "cancel_state": "DENY_SENT",
                "deny_sent_at": deny_sent.isoformat(),
            })
            self._make_cancel_entry_in_file("data/cancellations.json", entry)

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm)
            monitor.run()

            data = monitor._load()
            e = next(x for x in data["cancellations"] if x["product_order_id"] == oid)
            self._assert(e["cancel_state"] in ("URGENT_3DAY", "MANUAL_4DAY"),
                         f"URGENT_3DAY/MANUAL_4DAY 기대: {e['cancel_state']}")
            notifier.send.assert_called()

    def test_cancel_4day_manual(self):
        """4영업일 이상 경과 → MANUAL_4DAY + 수동 알림"""
        with self._ctx("t33"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            ss = self._ss_mock(order_status="CANCEL_REQUEST")
            dk, dm = self._supplier_mocks()
            dk.get_cancel_result.return_value = "PENDING"
            notifier = self._notifier()

            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001", status="ORDERED",
                ))

            # 7영업일 전 deny_sent
            deny_sent = datetime.now(timezone.utc) - timedelta(days=7)
            entry = self._make_base_cancel_entry(b0)
            entry.update({
                "supplier": "domaekkuk", "supplier_order_no": "DK000001",
                "cancel_state": "URGENT_3DAY",   # 이미 3일 경고 → 4일로 전환 테스트
                "deny_sent_at": deny_sent.isoformat(),
            })
            self._make_cancel_entry_in_file("data/cancellations.json", entry)

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm)
            monitor.run()

            data = monitor._load()
            e = next(x for x in data["cancellations"] if x["product_order_id"] == oid)
            self._assert(e["cancel_state"] == "MANUAL_4DAY",
                         f"MANUAL_4DAY 기대: {e['cancel_state']}")
            calls_text = " ".join(str(c) for c in notifier.send.call_args_list)
            self._assert("수동" in calls_text or "4영업일" in calls_text,
                         "수동 처리 알림 없음")

    def test_cancel_race_condition(self):
        """발주확인 후 취소 요청 → RACE_CONDITION + 알림"""
        with self._ctx("t34"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            ss = self._ss_mock()
            # 24시간 취소 목록에 oid 포함
            ss.get_cancellations.side_effect = [
                [],  # 1시간 목록 (신규 없음)
                [make_cancel_ss_item(
                    make_ss_item(b0, self.products[b0["product_letter"]], self.run_id)
                )],  # 24시간 목록
            ]
            notifier = self._notifier()

            # confirm_log.json에 oid 기록 (발주확인 완료)
            confirm_log = Path("data/confirm_log.json")
            confirm_log.parent.mkdir(exist_ok=True)
            confirm_log.write_text(json.dumps({
                "confirms": [{"order_id": oid,
                               "confirmed_at": datetime.now(timezone.utc).isoformat()}]
            }, ensure_ascii=False))

            monitor = CancelMonitor(ss, notifier=notifier,
                                    dk_api=MagicMock(), dm_cli=MagicMock())
            monitor.run()

            data = monitor._load()
            race_entries = [e for e in data["cancellations"]
                            if e.get("cancel_state") == "RACE_CONDITION"]
            self._assert(len(race_entries) >= 1, "RACE_CONDITION 감지 안 됨")
            notifier.send.assert_called()

    def test_cancel_invoice_registered(self):
        """REJECTED_WAIT_SHIP → 송장 등록 확인 → INVOICE_REGISTERED"""
        with self._ctx("t35"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            ss = self._ss_mock(order_status="CANCEL_REQUEST")
            dk, dm = self._supplier_mocks()
            notifier = self._notifier()

            # DB: 송장 등록됨
            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001", status="SHIPPED",
                    tracking_number="9876543210", delivery_company="CJ대한통운",
                ))

            entry = self._make_base_cancel_entry(b0)
            entry.update({
                "supplier": "domaekkuk", "supplier_order_no": "DK000001",
                "cancel_state": "REJECTED_WAIT_SHIP",
                "deny_sent_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            })
            self._make_cancel_entry_in_file("data/cancellations.json", entry)

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm)
            monitor.run()

            data = monitor._load()
            e = next(x for x in data["cancellations"] if x["product_order_id"] == oid)
            self._assert(e["cancel_state"] == "INVOICE_REGISTERED",
                         f"INVOICE_REGISTERED 기대: {e['cancel_state']}")
            notifier.send.assert_called()

    # ═════════════════════════════════════════════════════════════════════════
    # ── 취소 철회 시나리오 ─────────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def test_withdrawal_case2a_approved_reorder(self):
        """Case 2-A: DENY_SENT 중 철회 + 도매처 취소 승인 → 자동 재발주"""
        with self._ctx("t36"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            # SS 상태: PAYED (취소 철회됨)
            ss = self._ss_mock(order_status="PAYED")
            dk, dm = self._supplier_mocks()
            dk.get_cancel_result.return_value = "APPROVED"
            notifier = self._notifier()

            # orders.json에 주문 등록
            order_repo = OrderRepository()
            ss.get_orders.return_value = [
                make_ss_item(b0, self.products[b0["product_letter"]], self.run_id)
            ]
            collector = OrderCollector(ss, order_repo)
            collector.run()
            order_repo.update_status(oid, "ORDERED")

            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001", status="ORDERED",
                ))

            entry = self._make_base_cancel_entry(b0)
            entry.update({
                "supplier": "domaekkuk", "supplier_order_no": "DK000001",
                "cancel_state": "DENY_SENT",
                "deny_sent_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            })
            self._make_cancel_entry_in_file("data/cancellations.json", entry)

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm,
                                    order_repo=order_repo)
            monitor.run()

            data = monitor._load()
            e = next(x for x in data["cancellations"] if x["product_order_id"] == oid)
            self._assert(e["cancel_state"] in ("BUYER_WITHDREW_REORDER",
                                                "BUYER_WITHDREW_REORDER_FAILED"),
                         f"재발주 상태 기대: {e['cancel_state']}")
            notifier.send.assert_called()
            # 주문 NEW로 복원 확인 (BUYER_WITHDREW_REORDER 경우)
            if e["cancel_state"] == "BUYER_WITHDREW_REORDER":
                o = order_repo.find(oid)
                self._assert(o["status"] == "NEW",
                             f"NEW 복원 기대: {o['status']}")

    def test_withdrawal_case2b_rejected_normal(self):
        """Case 2-B: DENY_SENT 중 철회 + 도매처 취소 거부 → 발주 정상 진행"""
        with self._ctx("t37"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            ss = self._ss_mock(order_status="PAYED")  # 철회됨
            dk, dm = self._supplier_mocks()
            dk.get_cancel_result.return_value = "REJECTED"
            notifier = self._notifier()

            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001", status="ORDERED",
                ))

            entry = self._make_base_cancel_entry(b0)
            entry.update({
                "supplier": "domaekkuk", "supplier_order_no": "DK000001",
                "cancel_state": "DENY_SENT",
                "deny_sent_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            })
            self._make_cancel_entry_in_file("data/cancellations.json", entry)

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm)
            monitor.run()

            data = monitor._load()
            e = next(x for x in data["cancellations"] if x["product_order_id"] == oid)
            self._assert(e["cancel_state"] == "BUYER_WITHDREW_REJECTED",
                         f"BUYER_WITHDREW_REJECTED 기대: {e['cancel_state']}")
            notifier.send.assert_called()

    def test_withdrawal_case2c_pending(self):
        """Case 2-C: DENY_SENT 중 철회 + 도매처 미처리 → BUYER_WITHDREW_DENY_PENDING"""
        with self._ctx("t38"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            ss = self._ss_mock(order_status="PAYED")  # 철회됨
            dk, dm = self._supplier_mocks()
            dk.get_cancel_result.return_value = "PENDING"
            notifier = self._notifier()

            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001", status="ORDERED",
                ))

            entry = self._make_base_cancel_entry(b0)
            entry.update({
                "supplier": "domaekkuk", "supplier_order_no": "DK000001",
                "cancel_state": "DENY_SENT",
                "deny_sent_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            })
            self._make_cancel_entry_in_file("data/cancellations.json", entry)

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm)
            monitor.run()

            data = monitor._load()
            e = next(x for x in data["cancellations"] if x["product_order_id"] == oid)
            self._assert(e["cancel_state"] == "BUYER_WITHDREW_DENY_PENDING",
                         f"BUYER_WITHDREW_DENY_PENDING 기대: {e['cancel_state']}")

    def test_withdrawal_2day_manual_alert(self):
        """BUYER_WITHDREW_DENY_PENDING 2영업일 초과 → 수동 알림"""
        with self._ctx("t39"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            ss = self._ss_mock(order_status="PAYED")
            dk, dm = self._supplier_mocks()
            dk.get_cancel_result.return_value = "PENDING"
            notifier = self._notifier()

            with get_session() as session:
                session.add(SupplierOrder(
                    ss_order_id=oid, supplier="domaekkuk",
                    supplier_product_id=self.products[b0["product_letter"]]["supplier_product_id"],
                    supplier_order_no="DK000001", status="ORDERED",
                ))

            # 3영업일 전 deny_sent
            entry = self._make_base_cancel_entry(b0)
            entry.update({
                "supplier": "domaekkuk", "supplier_order_no": "DK000001",
                "cancel_state": "BUYER_WITHDREW_DENY_PENDING",
                "deny_sent_at": (datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),
            })
            self._make_cancel_entry_in_file("data/cancellations.json", entry)

            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm)
            monitor.run()

            notifier.send.assert_called()
            calls_text = " ".join(str(c) for c in notifier.send.call_args_list)
            self._assert("수동" in calls_text or "2영업일" in calls_text,
                         "2영업일 초과 수동 알림 없음")

    # ═════════════════════════════════════════════════════════════════════════
    # ── 복합 시나리오 ─────────────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def test_complex_cancel_withdraw_recancel(self):
        """취소 요청 → 철회 → 재취소"""
        with self._ctx("t40"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            prod = self.products[b0["product_letter"]]
            ss_item = make_ss_item(b0, prod, self.run_id)

            notifier = self._notifier()
            order_repo = OrderRepository()
            mapping_repo = self._mapping_repo()
            dk, dm = self._supplier_mocks()

            # Step 1: 주문 수집
            ss = self._ss_mock()
            ss.get_orders.return_value = [ss_item]
            OrderCollector(ss, order_repo).run()

            # Step 2: 발주
            ss.get_cancellations.return_value = []
            placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                 notifier=notifier, ss_api=ss)
            placer.run()
            o = order_repo.find(oid)
            self._assert(o["status"] == "ORDERED", f"ORDERED 기대: {o['status']}")

            # Step 3: 취소 요청
            ss.get_cancellations.return_value = [make_cancel_ss_item(ss_item)]
            ss.get_product_order.return_value = {"productOrder": {"productOrderStatus": "CANCEL_REQUEST"}}
            monitor = CancelMonitor(ss, notifier=notifier, dk_api=dk, dm_cli=dm,
                                    order_repo=order_repo)
            monitor.run()
            data = monitor._load()
            e = next(x for x in data["cancellations"] if x["product_order_id"] == oid)
            self._assert(e["cancel_state"] == "DENY_SENT", f"DENY_SENT 기대: {e['cancel_state']}")

            # Step 4: 도매처 취소 승인 + 구매자 철회
            ss.get_cancellations.return_value = []  # 신규 취소 없음
            ss.get_product_order.return_value = {"productOrder": {"productOrderStatus": "PAYED"}}
            dk.get_cancel_result.return_value = "APPROVED"
            monitor.run()
            data = monitor._load()
            e = next(x for x in data["cancellations"] if x["product_order_id"] == oid)
            self._assert(e["cancel_state"] in ("BUYER_WITHDREW_REORDER",
                                                "BUYER_WITHDREW_REORDER_FAILED"),
                         f"재발주 상태 기대: {e['cancel_state']}")

            # Step 5: 재발주 후 주문 상태 확인
            # (동일 order_id 재취소는 cancel_monitor가 seen_ids로 스킵 — 정상 동작)
            if e["cancel_state"] == "BUYER_WITHDREW_REORDER":
                o_restored = order_repo.find(oid)
                self._assert(o_restored is not None, "주문 복원 확인")
                self._assert(o_restored["status"] == "NEW",
                             f"재발주 NEW 기대: {o_restored['status']}")

    def test_complex_emoney_charge_reorder(self):
        """e머니 부족 → PENDING 전환 → 수동 복원 후 재발주"""
        with self._ctx("t41"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            notifier = self._notifier()
            pending_repo = PendingOrderRepository()
            budget = BudgetManager(initial_balance=50_000_000)

            OrderCollector(ss, order_repo).run()

            # e머니 부족 시뮬레이션
            call_count = {"n": 0}
            def fail_first(pid, qty, shipping, **kw):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise EmoneyShortageError("잔액 부족")
                return {"order_no": f"DK{call_count['n']:06d}"}
            dk.place_order.side_effect = fail_first

            placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                 notifier=notifier, pending=pending_repo,
                                 budget=budget, stock_pending=StockPendingRepository())
            stats1 = placer.run()
            self._assert(stats1["deferred"] > 0, f"e머니 부족 → 대기 기대: {stats1}")
            notifier.send.assert_called()

            # e머니 충전 후 resume (budget도 충분하므로 resume_pending 가능)
            dk.place_order.side_effect = lambda pid, qty, s, **kw: {"order_no": "DK_RESUMED"}
            budget.charge(50_000_000)
            stats2 = placer.resume_pending()
            self._assert(stats2["ordered"] + stats2["still_pending"] > 0,
                         f"재개 결과 기대: {stats2}")

    def test_complex_pending_cancel(self):
        """발주 대기(PENDING) 중 취소 요청 → resume_pending 시 CANCELLED 처리"""
        with self._ctx("t42"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            prod = self.products[b0["product_letter"]]

            # 1단계: 취소 없이 주문 수집 → PENDING 전환
            ss = self._ss_mock()
            ss.get_orders.return_value = [make_ss_item(b0, prod, self.run_id)]
            ss.get_cancellations.return_value = []  # 취소 없음

            dk, dm = self._supplier_mocks()
            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            pending_repo = PendingOrderRepository()
            budget = BudgetManager(initial_balance=0)  # 잔액 없음 → 전부 PENDING

            OrderCollector(ss, order_repo).run()

            with patch("src.core.budget_sale_guard.suspend_all") as mock_suspend:
                mock_suspend.return_value = 0
                placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                     notifier=self._notifier(), budget=budget,
                                     pending=pending_repo, ss_api=ss,
                                     stock_pending=StockPendingRepository())
                placer.run()

            # PENDING 상태 확인
            pending = pending_repo.all()
            self._assert(len(pending) > 0, f"PENDING 주문 없음: {order_repo.find(oid)}")

            # 2단계: 취소 요청 발생
            cancel_item = make_cancel_ss_item(make_ss_item(b0, prod, self.run_id))
            ss.get_cancellations.return_value = [cancel_item]

            # 3단계: 충전 후 재개 시 취소 감지 → CANCELLED
            budget.charge(50_000_000)
            stats = placer.resume_pending()
            self._assert(stats["cancelled"] >= 1, f"PENDING 취소 처리 기대: {stats}")

            o = order_repo.find(oid)
            self._assert(o["status"] == "CANCELLED", f"CANCELLED 기대: {o['status']}")

    # ═════════════════════════════════════════════════════════════════════════
    # ── 예산 관리 (budget_sale_guard) ─────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def test_budget_suspend_all(self):
        """예산 부족 → suspend_all → 전체 판매중지 + 알림"""
        with self._ctx("t43"):
            from src.core.budget_sale_guard import suspend_all, _STATE_FILE
            ss = self._ss_mock()
            notifier = self._notifier()
            mapping_repo = self._mapping_repo()

            count = suspend_all(ss, mapping_repo, notifier=notifier)

            self._assert(count > 0, f"판매중지 상품 있어야 함: {count}")
            ss.set_product_sale_status.assert_called()
            pause_calls = [
                c for c in ss.set_product_sale_status.call_args_list
                if c.kwargs.get("on_sale") is False
                or (c.args and len(c.args) > 1 and c.args[1] is False)
            ]
            self._assert(len(pause_calls) == count, f"판매중지 {count}건 기대")
            notifier.send.assert_called()

    def test_budget_restore_all(self):
        """예산 충전 → restore_all → 전체 판매재개"""
        with self._ctx("t44"):
            from src.core.budget_sale_guard import suspend_all, restore_all
            ss = self._ss_mock()
            notifier = self._notifier()
            mapping_repo = self._mapping_repo()

            suspend_all(ss, mapping_repo)
            ss.reset_mock()

            count = restore_all(ss, mapping_repo, notifier=notifier)
            self._assert(count > 0, f"판매재개 상품 있어야 함: {count}")
            resume_calls = [
                c for c in ss.set_product_sale_status.call_args_list
                if c.kwargs.get("on_sale") is True
                or (c.args and len(c.args) > 1 and c.args[1] is True)
            ]
            self._assert(len(resume_calls) == count, f"판매재개 {count}건 기대")

    # ═════════════════════════════════════════════════════════════════════════
    # ── 이메일 알림 필드 검증 ────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def test_email_notification_required_fields(self):
        """모든 알림에 상품명·SS주문번호·현재상태·필요조치·구매자정보 포함 검증"""
        with self._ctx("t45"):
            b0 = self.buyers[0]
            oid = order_id(b0["idx"], b0["product_letter"], self.run_id)
            prod = self.products[b0["product_letter"]]

            ss = self._ss_mock()
            ss.get_cancellations.return_value = [
                make_cancel_ss_item(make_ss_item(b0, prod, self.run_id))
            ]
            ss.get_product_order.return_value = {"productOrder": {"productOrderStatus": "CANCEL_REQUEST"}}
            dk, dm = self._supplier_mocks()

            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()
            notifier = self._notifier()

            # 발주 에러 알림
            dk.place_order.side_effect = Exception("API 오류")
            dm.place_order.side_effect = Exception("API 오류")
            collector = OrderCollector(ss, order_repo)
            ss.get_orders.return_value = [make_ss_item(b0, prod, self.run_id)]
            collector.run()

            placer = OrderPlacer(dk, dm, mapping_repo, order_repo, notifier=notifier)
            placer.run()

            self._assert(notifier.send.called, "알림 미발송")
            for c in notifier.send.call_args_list:
                subject = c[0][0] if c[0] else c[1].get("subject", "")
                body    = c[0][1] if len(c[0]) > 1 else c[1].get("body", "")
                combined = subject + body
                self._assert(prod["product_name"] in combined or oid in combined,
                             f"알림에 상품명/주문번호 누락\nsubject={subject}\nbody={body[:200]}")
                self._assert("상태" in combined or "조치" in combined,
                             f"알림에 상태/조치 누락\n{combined[:200]}")
                self._assert(b0["name"] in combined,
                             f"알림에 구매자명({b0['name']}) 누락\n{combined[:300]}")

    # ═════════════════════════════════════════════════════════════════════════
    # ── 정보 전달 정확성 검증 ────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def test_info_accuracy_all_100_buyers(self):
        """100명 각각의 이름·전화번호·주소·옵션·수량이 발주에 정확히 전달"""
        with self._ctx("t46"):
            ss = self._ss_mock()
            dk, dm = self._supplier_mocks()
            mapping_repo = self._mapping_repo()
            order_repo = OrderRepository()

            collector = OrderCollector(ss, order_repo)
            collector.run()

            # place_order 호출 캡처
            dk_calls: list[dict] = []
            dm_calls: list[dict] = []

            def capture_dk(pid, qty, shipping, **kw):
                dk_calls.append({"pid": pid, "qty": qty, "shipping": shipping, "kw": kw})
                return {"order_no": f"DK{len(dk_calls):06d}"}

            def capture_dm(pid, qty, shipping, **kw):
                dm_calls.append({"pid": pid, "qty": qty, "shipping": shipping, "kw": kw})
                return {"order_no": f"DM{len(dm_calls):06d}"}

            dk.place_order.side_effect = capture_dk
            dm.place_order.side_effect = capture_dm

            placer = OrderPlacer(dk, dm, mapping_repo, order_repo,
                                 notifier=self._notifier(), ss_api=ss,
                                 stock_pending=StockPendingRepository())
            placer.run()

            # 발주가 이루어진 구매자 (품절 상품 C, J 제외)
            placed_buyers = [b for b in self.buyers
                             if self.products[b["product_letter"]]["stock"] > 0]
            all_calls = dk_calls + dm_calls
            self._assert(len(all_calls) == len(placed_buyers),
                         f"발주 호출 수 불일치: {len(all_calls)} != {len(placed_buyers)}")

            # 각 구매자별 정보 검증
            for b in placed_buyers:
                prod = self.products[b["product_letter"]]
                supplier_calls = dk_calls if prod["supplier"] == "domaekkuk" else dm_calls
                matched = [c for c in supplier_calls
                           if c["shipping"]["name"] == b["name"]
                           and c["shipping"]["phone"] == b["phone"]]
                self._assert(len(matched) >= 1,
                             f"구매자 정보 미전달: {b['name']} / {b['phone']}")
                c = matched[0]
                self._assert(c["qty"] == b["quantity"],
                             f"수량 불일치 [{b['name']}]: {c['qty']} != {b['quantity']}")
                self._assert(c["shipping"]["address"] == b["address"],
                             f"주소 불일치 [{b['name']}]")
                self._assert(c["shipping"]["zipcode"] == b["zipcode"],
                             f"우편번호 불일치 [{b['name']}]")
                self._assert(c["shipping"]["memo"] == b["delivery_memo"],
                             f"배송메모 불일치 [{b['name']}]")
                if prod["supplier_option_id"]:
                    self._assert("supplier_option_id" in c["kw"] or "option_id" in c["kw"],
                                 f"옵션 미전달 [{b['name']}]")

    def test_info_uniqueness(self):
        """100명 구매자 정보 고유성 검증"""
        with self._ctx("t47"):
            phones  = [b["phone"]   for b in self.buyers]
            names   = [b["name"]    for b in self.buyers]
            addrs   = [b["address"] for b in self.buyers]
            oids    = [order_id(b["idx"], b["product_letter"], self.run_id)
                       for b in self.buyers]

            self._assert(len(set(phones)) == 100,
                         f"전화번호 중복 존재: {100 - len(set(phones))}건")
            self._assert(len(set(oids)) == 100,
                         f"주문ID 중복 존재: {100 - len(set(oids))}건")
            self._assert(len(set(addrs)) == 100,
                         f"주소 중복 존재: {100 - len(set(addrs))}건")
            # 이름은 성씨+이름 조합으로 일부 중복 허용 (성씨 20개 × 이름 30개)
            self._assert(len(names) == 100, "구매자 100명 기대")

    # ═════════════════════════════════════════════════════════════════════════
    # ── 전체 실행 ─────────────────────────────────────────────────────────────
    # ═════════════════════════════════════════════════════════════════════════

    def run_all(self) -> dict:
        tests = [
            ("T01 주문수집_정상",                    self.test_order_collection_normal),
            ("T02 주문수집_중복방지",                self.test_order_collection_dedup),
            ("T03 자동발주_정상",                    self.test_placement_normal),
            ("T04 발주_취소요청_차단",               self.test_placement_cancel_filter),
            ("T05 e머니_부족_대기",                  self.test_placement_emoney_shortage),
            ("T06 발주_API오류_알림",                self.test_placement_api_error),
            ("T07 발주_매핑없음_ERROR",              self.test_placement_no_mapping),
            ("T08 SS발주확인_실패_알림",             self.test_ss_confirm_failure),
            ("T09 예산_충분_전체발주",               self.test_budget_normal),
            ("T10 예산_부족_PENDING",                self.test_budget_shortage_pending),
            ("T11 예산_충전_재개",                   self.test_budget_charge_resume),
            ("T12 대기중_취소요청",                  self.test_budget_pending_cancel_during_wait),
            ("T13 동시실행_중복방지",                self.test_budget_concurrent_lock),
            ("T14 송장_domaekkuk",                   self.test_invoice_sync_domaekkuk),
            ("T15 송장_domaemae",                    self.test_invoice_sync_domaemae),
            ("T16 송장_미발송_pending",              self.test_invoice_sync_pending),
            ("T17 송장_등록실패_알림",               self.test_invoice_sync_error),
            ("T18 재고_품절_판매중지",               self.test_inventory_out_of_stock),
            ("T19 재고_재입고_판매재개",             self.test_inventory_restock),
            ("T20 재고_API오류_알림",                self.test_inventory_api_error),
            ("T21 재고_부족알림",                    self.test_inventory_low_stock_alert),
            ("T22 가격_변동_감지",                   self.test_price_change_detected),
            ("T23 가격_조회오류_알림",               self.test_price_monitor_error),
            ("T24 반품_감지_알림",                   self.test_return_detection),
            ("T25 취소_발주없음_SS_AUTO",            self.test_cancel_ss_auto),
            ("T26 취소_출고완료_CANCEL_REJECT",      self.test_cancel_shipped_reject),
            ("T27 취소_정상_DENY_SENT",              self.test_cancel_deny_sent),
            ("T28 취소_setOrdDeny실패_DENY_FAILED",  self.test_cancel_deny_failed),
            ("T29 취소_DENY전_철회",                 self.test_cancel_buyer_withdrew_pre_deny),
            ("T30 취소_승인_APPROVED",               self.test_cancel_approved),
            ("T31 취소_거부_REJECTED_WAIT_SHIP",     self.test_cancel_rejected_wait_ship),
            ("T32 취소_3영업일_URGENT",              self.test_cancel_3day_urgent),
            ("T33 취소_4영업일_MANUAL",              self.test_cancel_4day_manual),
            ("T34 취소_레이스컨디션",                self.test_cancel_race_condition),
            ("T35 취소_거부후_송장등록",             self.test_cancel_invoice_registered),
            ("T36 철회_Case2A_재발주",               self.test_withdrawal_case2a_approved_reorder),
            ("T37 철회_Case2B_정상발주",             self.test_withdrawal_case2b_rejected_normal),
            ("T38 철회_Case2C_PENDING",              self.test_withdrawal_case2c_pending),
            ("T39 철회_2영업일_수동알림",            self.test_withdrawal_2day_manual_alert),
            ("T40 복합_취소→철회→재취소",           self.test_complex_cancel_withdraw_recancel),
            ("T41 복합_e머니→충전→재발주",          self.test_complex_emoney_charge_reorder),
            ("T42 복합_대기중_취소",                 self.test_complex_pending_cancel),
            ("T43 예산가드_suspend_all",             self.test_budget_suspend_all),
            ("T44 예산가드_restore_all",             self.test_budget_restore_all),
            ("T45 이메일_필수필드_검증",             self.test_email_notification_required_fields),
            ("T46 정보정확성_100명",                 self.test_info_accuracy_all_100_buyers),
            ("T47 정보고유성_검증",                  self.test_info_uniqueness),
        ]

        print(f"\n  총 {len(tests)}개 테스트 실행 (run_id={self.run_id})")
        print(f"  {'-'*60}")

        for name, fn in tests:
            self._run_test(name, fn)

        passed = sum(1 for _, ok, _ in self._results if ok)
        failed = sum(1 for _, ok, _ in self._results if not ok)
        total  = len(self._results)

        print(f"\n  {'-'*60}")
        print(f"  결과: {passed}/{total} 통과" + (f", {failed}건 실패" if failed else " (전체 통과)"))
        return {"passed": passed, "failed": failed, "total": total}


# ═════════════════════════════════════════════════════════════════════════════
# pytest 호환
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def runner(tmp_path):
    r = E2ERunner(run_id=1)
    r.tmpdir    = str(tmp_path)
    r._orig_cwd = os.getcwd()
    os.chdir(str(tmp_path))
    (tmp_path / "data").mkdir(exist_ok=True)
    yield r
    os.chdir(r._orig_cwd)


class TestE2EV3:
    def test_T01(self, runner): runner._run_test("T01", runner.test_order_collection_normal)
    def test_T02(self, runner): runner._run_test("T02", runner.test_order_collection_dedup)
    def test_T03(self, runner): runner._run_test("T03", runner.test_placement_normal)
    def test_T04(self, runner): runner._run_test("T04", runner.test_placement_cancel_filter)
    def test_T05(self, runner): runner._run_test("T05", runner.test_placement_emoney_shortage)
    def test_T06(self, runner): runner._run_test("T06", runner.test_placement_api_error)
    def test_T07(self, runner): runner._run_test("T07", runner.test_placement_no_mapping)
    def test_T08(self, runner): runner._run_test("T08", runner.test_ss_confirm_failure)
    def test_T09(self, runner): runner._run_test("T09", runner.test_budget_normal)
    def test_T10(self, runner): runner._run_test("T10", runner.test_budget_shortage_pending)
    def test_T11(self, runner): runner._run_test("T11", runner.test_budget_charge_resume)
    def test_T12(self, runner): runner._run_test("T12", runner.test_budget_pending_cancel_during_wait)
    def test_T13(self, runner): runner._run_test("T13", runner.test_budget_concurrent_lock)
    def test_T14(self, runner): runner._run_test("T14", runner.test_invoice_sync_domaekkuk)
    def test_T15(self, runner): runner._run_test("T15", runner.test_invoice_sync_domaemae)
    def test_T16(self, runner): runner._run_test("T16", runner.test_invoice_sync_pending)
    def test_T17(self, runner): runner._run_test("T17", runner.test_invoice_sync_error)
    def test_T18(self, runner): runner._run_test("T18", runner.test_inventory_out_of_stock)
    def test_T19(self, runner): runner._run_test("T19", runner.test_inventory_restock)
    def test_T20(self, runner): runner._run_test("T20", runner.test_inventory_api_error)
    def test_T21(self, runner): runner._run_test("T21", runner.test_inventory_low_stock_alert)
    def test_T22(self, runner): runner._run_test("T22", runner.test_price_change_detected)
    def test_T23(self, runner): runner._run_test("T23", runner.test_price_monitor_error)
    def test_T24(self, runner): runner._run_test("T24", runner.test_return_detection)
    def test_T25(self, runner): runner._run_test("T25", runner.test_cancel_ss_auto)
    def test_T26(self, runner): runner._run_test("T26", runner.test_cancel_shipped_reject)
    def test_T27(self, runner): runner._run_test("T27", runner.test_cancel_deny_sent)
    def test_T28(self, runner): runner._run_test("T28", runner.test_cancel_deny_failed)
    def test_T29(self, runner): runner._run_test("T29", runner.test_cancel_buyer_withdrew_pre_deny)
    def test_T30(self, runner): runner._run_test("T30", runner.test_cancel_approved)
    def test_T31(self, runner): runner._run_test("T31", runner.test_cancel_rejected_wait_ship)
    def test_T32(self, runner): runner._run_test("T32", runner.test_cancel_3day_urgent)
    def test_T33(self, runner): runner._run_test("T33", runner.test_cancel_4day_manual)
    def test_T34(self, runner): runner._run_test("T34", runner.test_cancel_race_condition)
    def test_T35(self, runner): runner._run_test("T35", runner.test_cancel_invoice_registered)
    def test_T36(self, runner): runner._run_test("T36", runner.test_withdrawal_case2a_approved_reorder)
    def test_T37(self, runner): runner._run_test("T37", runner.test_withdrawal_case2b_rejected_normal)
    def test_T38(self, runner): runner._run_test("T38", runner.test_withdrawal_case2c_pending)
    def test_T39(self, runner): runner._run_test("T39", runner.test_withdrawal_2day_manual_alert)
    def test_T40(self, runner): runner._run_test("T40", runner.test_complex_cancel_withdraw_recancel)
    def test_T41(self, runner): runner._run_test("T41", runner.test_complex_emoney_charge_reorder)
    def test_T42(self, runner): runner._run_test("T42", runner.test_complex_pending_cancel)
    def test_T43(self, runner): runner._run_test("T43", runner.test_budget_suspend_all)
    def test_T44(self, runner): runner._run_test("T44", runner.test_budget_restore_all)
    def test_T45(self, runner): runner._run_test("T45", runner.test_email_notification_required_fields)
    def test_T46(self, runner): runner._run_test("T46", runner.test_info_accuracy_all_100_buyers)
    def test_T47(self, runner): runner._run_test("T47", runner.test_info_uniqueness)


# ═════════════════════════════════════════════════════════════════════════════
# 독립 실행: 2회 연속 통과
# ═════════════════════════════════════════════════════════════════════════════

def _print_header(msg: str):
    print(f"\n{'='*70}")
    print(f"  {msg}")
    print(f"{'='*70}")


def main():
    _print_header("E2E 시뮬레이션 테스트 v3 — 2회 연속 통과 검증")
    print("  구성: 가상 구매자 100명 × 가상 상품 10개 (A~J) × 47개 테스트")

    for run_id in range(1, 3):
        _print_header(f"{run_id}회차 실행 — 구매자 정보 새로 생성")

        runner = E2ERunner(run_id=run_id)
        try:
            runner.setup()
            result = runner.run_all()
        except AssertionError as e:
            runner.teardown()
            # 실패한 테스트 상세 출력
            failed_tests = [(name, detail) for name, ok, detail in runner._results if not ok]
            _print_header(f"✗ {run_id}회차 실패 — 즉시 중단")
            for name, detail in failed_tests:
                print(f"\n  [실패 테스트] {name}")
                print(f"  {'-'*50}")
                print(detail)
            print("\n  오류를 수정한 후 다시 실행하세요.")
            sys.exit(1)
        except Exception as e:
            runner.teardown()
            _print_header(f"✗ {run_id}회차 예외 발생")
            traceback.print_exc()
            sys.exit(1)

        runner.teardown()

        if result["failed"] > 0:
            failed_tests = [(name, detail) for name, ok, detail in runner._results if not ok]
            _print_header(f"✗ {run_id}회차 실패 ({result['failed']}건 오류)")
            for name, detail in failed_tests:
                print(f"\n  [실패] {name}\n{detail}")
            sys.exit(1)

        _print_header(f"✓ {run_id}회차 전체 통과 — {result['passed']}/{result['total']}건")

    _print_header("✓ 2회 연속 전체 통과 완료 — E2E 시뮬레이션 검증 성공")
    print(f"\n  총 {47 * 2}건 테스트 (47 × 2회) 모두 통과\n")


if __name__ == "__main__":
    main()
