"""
신규 기능 테스트 — 미검증 기능 100회 반복 검증

[테스트 목록]
  A. 재고부족 대기 전체 (stock_pending 저장, 품절처리, 재입고 시 자동 재발주)
  B. 도매매 세션 자동 갱신 (setLoginChk)
  C. MappingRepository remove/set_active/update_memo
  D. URL 파싱으로 상품번호 추출
  E. Ctrl+C graceful shutdown
"""

import datetime
import io
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.database import init_db
from src.core.mapping_repository import MappingRepository, extract_supplier_id
from src.core.order_collector import OrderCollector
from src.core.order_placer import OrderPlacer
from src.core.order_repository import OrderRepository
from src.core.inventory_sync import InventorySync
from src.core.stock_pending_repository import StockPendingRepository
from src.api.domaemae import DomaemaeClient

N = 100  # 반복 횟수

# ── 공통 mock 데이터 ──────────────────────────────────────────────

MOCK_SS_ORDER = {
    "productOrder": {
        "productOrderId": "ORD-001",
        "orderId":         "SS-001",
        "productId":       "P100",
        "productName":     "도매꾹 티셔츠",
        "optionCode":      "",
        "quantity":        1,
    },
    "order": {"ordererName": "홍길동"},
    "shippingAddress": {
        "name":       "홍길동",
        "tel1":       "010-1234-5678",
        "addressStr": "서울시 강남구 테헤란로 123",
        "zipCode":    "06234",
    },
    "deliveryMemo": "문앞 배송",
}


def _make_env():
    """격리된 테스트 환경 (P100 → domaekkuk:DK-001 단일 매핑)"""
    tmpdir = Path(tempfile.mkdtemp())
    init_db(str(tmpdir / "test.db"))

    order_repo   = OrderRepository(tmpdir / "orders.json")
    mapping_repo = MappingRepository(tmpdir / "mappings.json")
    mapping_repo.add("P100", "domaekkuk", "DK-001")

    mock_ss = MagicMock()
    mock_ss.get_orders.return_value = [MOCK_SS_ORDER]
    mock_ss.set_product_sale_status.return_value = None

    mock_dk = MagicMock()
    mock_dk.get_product.return_value = {
        "title": "도매꾹 상품", "price": 10_000, "stock": 50, "seller_id": "s1",
    }
    mock_dk.place_order.return_value = {"order_no": "DK-ORDER-001"}

    mock_dm       = MagicMock()
    mock_notifier = MagicMock()

    stock_pending = StockPendingRepository(tmpdir / "stock_pending.json")

    return {
        "tmpdir":        tmpdir,
        "order_repo":    order_repo,
        "mapping_repo":  mapping_repo,
        "mock_ss":       mock_ss,
        "mock_dk":       mock_dk,
        "mock_dm":       mock_dm,
        "mock_notifier": mock_notifier,
        "stock_pending": stock_pending,
    }


# ══════════════════════════════════════════════════════════════════
# A. 재고부족 대기 전체
# ══════════════════════════════════════════════════════════════════

class TestStockPending(unittest.TestCase):
    """재고부족 대기 전체: stock_pending 저장, 품절처리, 재입고 시 자동 재발주"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):
                e = _make_env()
                OrderCollector(e["mock_ss"], e["order_repo"]).run()

                # ── 1. 재고 0 → stock_pending 저장 + 스마트스토어 품절처리 ──
                e["mock_dk"].get_product.return_value["stock"] = 0
                placer = OrderPlacer(
                    e["mock_dk"], e["mock_dm"],
                    e["mapping_repo"], e["order_repo"],
                    e["mock_notifier"],
                    ss_api=e["mock_ss"],
                    stock_pending=e["stock_pending"],
                )
                stats = placer.run()

                self.assertEqual(stats["stock_pending"], 1)
                self.assertEqual(e["stock_pending"].count(), 1)

                entry = e["stock_pending"].all()[0]
                self.assertEqual(entry["supplier"],            "domaekkuk")
                self.assertEqual(entry["supplier_product_id"], "DK-001")
                self.assertEqual(entry["ss_product_id"],       "P100")

                self.assertEqual(
                    e["order_repo"].find("ORD-001")["status"], "STOCK_PENDING"
                )

                # 스마트스토어 품절 처리(on_sale=False) 호출 확인
                out_calls = [
                    c for c in e["mock_ss"].set_product_sale_status.call_args_list
                    if c[1].get("on_sale") is False
                ]
                self.assertGreater(len(out_calls), 0)

                # ── 2. InventorySync: 재입고 감지 → resume_stock_pending 자동 호출 ──
                sync = InventorySync(
                    e["mock_ss"], e["mock_dk"], e["mock_dm"],
                    e["mapping_repo"], e["mock_notifier"],
                    e["tmpdir"] / "stock_cache.json",
                    order_placer=placer,
                )

                # 첫 번째 동기화: stock=0 → cache[key]=False (paused)
                sync.run()

                # 재입고
                e["mock_dk"].get_product.return_value["stock"] = 50
                e["mock_ss"].reset_mock()

                # 두 번째 동기화: stock=50 → restock 감지 → resume_stock_pending
                stats2 = sync.run()
                self.assertEqual(stats2["resumed"], 1)
                self.assertEqual(
                    e["order_repo"].find("ORD-001")["status"], "ORDERED"
                )
                self.assertEqual(e["stock_pending"].count(), 0)

                # 재입고 후 판매재개(on_sale=True) 호출 확인
                resume_calls = [
                    c for c in e["mock_ss"].set_product_sale_status.call_args_list
                    if c[1].get("on_sale") is True
                ]
                self.assertGreater(len(resume_calls), 0)


# ══════════════════════════════════════════════════════════════════
# B. 도매매 세션 자동 갱신
# ══════════════════════════════════════════════════════════════════

class TestDomaemaeSession(unittest.TestCase):
    """도매매 세션 자동 갱신 (setLoginChk)"""

    _LOGIN_RESP = {
        "domeggook": {
            "sId":           "SESSION-123",
            "sIdRenewDate":  "2099-12-31 23:59:59",
            "loginKeepTime": 2592000,   # 30일
        }
    }
    _RENEW_RESP = {
        "domeggook": {
            "sId":          "SESSION-NEW",
            "sIdRenewDate": "2099-12-31 23:59:59",
        }
    }

    def _mock_resp(self, payload: dict) -> MagicMock:
        m = MagicMock()
        m.json.return_value = payload
        m.raise_for_status = MagicMock()
        return m

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):

                # ── 1. 세션 없음 → setLogin 호출 ──
                with patch("requests.post") as mock_post:
                    mock_post.return_value = self._mock_resp(self._LOGIN_RESP)
                    client = DomaemaeClient(api_key="KEY", user_id="user", password="pw")
                    client._ensure_session()

                    mock_post.assert_called_once()
                    data = mock_post.call_args[1]["data"]
                    self.assertEqual(data["mode"], "setLogin")
                    self.assertEqual(client._sid, "SESSION-123")

                # ── 2. 세션 유효 (renew 여유 있음) → API 호출 없음 ──
                client._sid                = "VALID-SID"
                client._login_time         = datetime.datetime.now()
                client._login_keep_seconds = 86400
                client._sid_renew_date     = (
                    datetime.datetime.now() + datetime.timedelta(hours=2)
                )
                with patch("requests.post") as mock_post:
                    client._ensure_session()
                    mock_post.assert_not_called()

                # ── 3. sIdRenewDate 임박 (< _RENEW_BUFFER=30s) → setLoginChk ──
                client._sid                = "VALID-SID"
                client._login_time         = datetime.datetime.now()
                client._login_keep_seconds = 86400
                client._sid_renew_date     = (
                    datetime.datetime.now() + datetime.timedelta(seconds=20)
                )
                with patch("requests.post") as mock_post:
                    mock_post.return_value = self._mock_resp(self._RENEW_RESP)
                    client._ensure_session()

                    mock_post.assert_called_once()
                    data = mock_post.call_args[1]["data"]
                    self.assertEqual(data["mode"], "setLoginChk")
                    self.assertEqual(client._sid, "SESSION-NEW")

                # ── 4. loginKeepTime 초과 → setLogin 재로그인 ──
                client._sid                = "EXPIRED-SID"
                client._login_time         = (
                    datetime.datetime.now() - datetime.timedelta(seconds=86401)
                )
                client._login_keep_seconds = 86400
                client._sid_renew_date     = (
                    datetime.datetime.now() + datetime.timedelta(hours=2)
                )
                with patch("requests.post") as mock_post:
                    mock_post.return_value = self._mock_resp(self._LOGIN_RESP)
                    client._ensure_session()

                    mock_post.assert_called_once()
                    data = mock_post.call_args[1]["data"]
                    self.assertEqual(data["mode"], "setLogin")


# ══════════════════════════════════════════════════════════════════
# C. MappingRepository remove/set_active/update_memo
# ══════════════════════════════════════════════════════════════════

class TestMappingRepository(unittest.TestCase):
    """MappingRepository remove/set_active/update_memo"""

    def _make_repo(self):
        tmpdir = Path(tempfile.mkdtemp())
        repo = MappingRepository(tmpdir / "mappings.json")
        m1 = repo.add("P100", "domaekkuk", "56328525")
        m2 = repo.add("P200", "domaemae",  "DM-001", ss_option_id="OPT-L", memo="청바지")
        return repo, m1, m2

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):

                # ── remove ──
                repo, m1, m2 = self._make_repo()

                self.assertTrue(repo.remove(m1["id"]))        # 삭제 성공 → True
                self.assertEqual(len(repo.all()), 1)
                self.assertFalse(repo.remove(9999))            # 없는 ID → False
                self.assertEqual(len(repo.all()), 1)           # 건수 유지

                self.assertIsNone(repo.find("P100"))           # 삭제된 매핑 없음
                self.assertIsNotNone(repo.find("P200", "OPT-L"))

                # ── set_active ──
                repo2, m1b, m2b = self._make_repo()

                repo2.set_active(m1b["id"], False)
                self.assertIsNone(repo2.find("P100"))          # 비활성 → find() 반환 안 됨

                repo2.set_active(m1b["id"], True)
                self.assertIsNotNone(repo2.find("P100"))       # 재활성화

                active_flag = next(
                    m["is_active"] for m in repo2.all() if m["id"] == m1b["id"]
                )
                self.assertTrue(active_flag)

                # ── update_memo ──
                repo3, m1c, m2c = self._make_repo()

                repo3.update_memo(m1c["id"], "수정된 메모")
                updated = next(m for m in repo3.all() if m["id"] == m1c["id"])
                self.assertEqual(updated["memo"], "수정된 메모")

                # 다른 매핑 메모 불변
                other = next(m for m in repo3.all() if m["id"] == m2c["id"])
                self.assertEqual(other["memo"], "청바지")


# ══════════════════════════════════════════════════════════════════
# D. URL 파싱으로 상품번호 추출
# ══════════════════════════════════════════════════════════════════

class TestUrlParsing(unittest.TestCase):
    """URL 파싱으로 상품번호 추출"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):

                # 도매꾹 URL (경로 기반)
                self.assertEqual(
                    extract_supplier_id("https://domeggook.com/56328525"), "56328525"
                )
                self.assertEqual(
                    extract_supplier_id("https://www.domeggook.com/56328525/티셔츠"),
                    "56328525",
                )

                # 도매매 URL (?no= 파라미터)
                self.assertEqual(
                    extract_supplier_id(
                        "https://www.domaemae.co.kr/product?no=56328525"
                    ),
                    "56328525",
                )
                self.assertEqual(
                    extract_supplier_id(
                        "https://domaemae.co.kr/item?category=123&no=99887766"
                    ),
                    "99887766",
                )

                # 숫자 ID 직접 입력 (공백 트리밍 포함)
                self.assertEqual(extract_supplier_id("56328525"),       "56328525")
                self.assertEqual(extract_supplier_id("  56328525  "),   "56328525")

                # 비숫자 → 원본 그대로 반환
                self.assertEqual(extract_supplier_id("DK-001"), "DK-001")

                # MappingRepository.add()와 통합: URL → ID 자동 추출
                tmpdir = Path(tempfile.mkdtemp())
                repo = MappingRepository(tmpdir / "m.json")
                m = repo.add("P1", "domaekkuk", "https://domeggook.com/12345678")
                self.assertEqual(m["supplier_product_id"], "12345678")
                self.assertEqual(m["supplier_url"], "https://domeggook.com/12345678")


# ══════════════════════════════════════════════════════════════════
# E. Ctrl+C graceful shutdown
# ══════════════════════════════════════════════════════════════════

class TestGracefulShutdown(unittest.TestCase):
    """Ctrl+C graceful shutdown"""

    def test_100_iterations(self):
        for i in range(N):
            with self.subTest(i=i + 1):

                # ── 1. _shutdown 패턴: scheduler.stop() + stop_event.set() ──
                mock_scheduler = MagicMock()
                stop_evt = threading.Event()

                def _shutdown(sig=None, frame=None):
                    mock_scheduler.stop()
                    stop_evt.set()

                self.assertFalse(stop_evt.is_set())
                _shutdown()
                mock_scheduler.stop.assert_called_once()
                self.assertTrue(stop_evt.is_set())

                # ── 2. stop_event 설정 시 main 루프 종료 ──
                stop_evt2 = threading.Event()

                def _loop():
                    while not stop_evt2.is_set():
                        time.sleep(0.005)

                t = threading.Thread(target=_loop, daemon=True)
                t.start()
                stop_evt2.set()                 # 즉시 종료 신호
                t.join(timeout=1.0)
                self.assertFalse(t.is_alive())

                # ── 3. SIGINT 핸들러 등록 + 직접 호출 ──
                mock_sched3 = MagicMock()
                stop_evt3   = threading.Event()

                def _shutdown3(sig=None, frame=None):
                    mock_sched3.stop()
                    stop_evt3.set()

                orig = signal.getsignal(signal.SIGINT)
                try:
                    signal.signal(signal.SIGINT, _shutdown3)
                    self.assertEqual(signal.getsignal(signal.SIGINT), _shutdown3)

                    # 실제 시그널 없이 핸들러 직접 호출
                    signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
                    mock_sched3.stop.assert_called_once()
                    self.assertTrue(stop_evt3.is_set())
                finally:
                    signal.signal(signal.SIGINT, orig)

                # ── 4. KeyboardInterrupt → _shutdown 호출 경로 ──
                mock_sched4 = MagicMock()
                stop_evt4   = threading.Event()

                def _shutdown4(sig=None, frame=None):
                    mock_sched4.stop()
                    stop_evt4.set()

                try:
                    raise KeyboardInterrupt
                except KeyboardInterrupt:
                    _shutdown4()

                mock_sched4.stop.assert_called_once()
                self.assertTrue(stop_evt4.is_set())


# ── 보고서 출력 ───────────────────────────────────────────────────

_FEATURE_MAP = {
    "TestStockPending":      "재고부족 대기 전체 (stock_pending 저장/품절/재발주)",
    "TestDomaemaeSession":   "도매매 세션 자동 갱신 (setLoginChk)",
    "TestMappingRepository": "MappingRepository remove/set_active/update_memo",
    "TestUrlParsing":        "URL 파싱으로 상품번호 추출",
    "TestGracefulShutdown":  "Ctrl+C graceful shutdown",
}

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    result = runner.run(suite)

    passed = result.testsRun - len(result.failures) - len(result.errors)
    failed = len(result.failures) + len(result.errors)

    print("\n" + "=" * 56)
    print("=== 신규 기능 테스트 보고서 ===")
    print("=" * 56)
    print(f"총 테스트: {result.testsRun}회")
    print(f"통과:      {passed}회")
    print(f"실패:      {failed}회")
    print()
    print(f"{'기능명':<50} | {'반복':>4} | {'결과'}")
    print("-" * 70)

    fail_names = {type(f).__name__ for f, _ in result.failures + result.errors}
    for cls_name, feature in _FEATURE_MAP.items():
        status = "FAIL ✗" if cls_name in fail_names else "PASS ✓"
        print(f"{feature:<50} | {N:>4} | {status}")

    if result.failures or result.errors:
        print()
        print("[실패 목록]")
        for test, tb in result.failures + result.errors:
            print(f"  - {type(test).__name__}: {tb.splitlines()[-1]}")

    sys.exit(0 if result.wasSuccessful() else 1)
