"""스마트스토어 API 실제 연결 테스트"""
import sys
import io
import json
from datetime import datetime, timedelta, timezone

# Windows 콘솔 UTF-8 출력 강제
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from src.utils.config_loader import load_config
from src.api.smartstore import SmartstoreAPI


def separator(title: str):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"{'='*50}")


def test_token(api: SmartstoreAPI):
    separator("1. 인증 토큰 발급 테스트")
    import time, base64, bcrypt, requests

    timestamp = str(int(time.time() * 1000))
    password  = f"{api.client_id}_{timestamp}"
    hashed    = bcrypt.hashpw(password.encode("utf-8"), api.client_secret.encode("utf-8"))
    signature = base64.b64encode(hashed).decode("utf-8")

    print(f"  timestamp : {timestamp}")
    print(f"  password  : {password}")
    print(f"  signature : {signature}")

    payload = {
        "client_id":          api.client_id,
        "timestamp":          timestamp,
        "client_secret_sign": signature,
        "grant_type":         "client_credentials",
        "type":               "SELF",
    }
    print(f"  요청 payload: {payload}")

    try:
        resp = requests.post(
            f"{api.BASE_URL}/v1/oauth2/token",
            data=payload,
        )
        print(f"  HTTP 상태코드: {resp.status_code}")
        print(f"  응답 본문: {resp.text}")
        resp.raise_for_status()

        data = resp.json()
        api._token = data["access_token"]
        api._token_expires_at = time.time() + data["expires_in"]

        print(f"[OK] 토큰 발급 성공")
        print(f"     토큰 앞 20자: {api._token[:20]}...")
        return True
    except Exception as e:
        print(f"[FAIL] 토큰 발급 실패: {e}")
        _print_hint(e)
        return False


def test_orders(api: SmartstoreAPI, days: int = 30):
    separator(f"2. 주문 목록 조회 테스트 (최근 {days}일, 상태: PAYED)")
    try:
        orders = api.get_orders(status="PAYED", days=days)
        print(f"[OK] 조회 성공 - 결제완료 주문 {len(orders)}건")
        if orders:
            print("\n  [최근 주문 3건 미리보기]")
            for o in orders[:3]:
                print(f"  - 주문번호: {o.get('orderId') or o.get('orderSeq', 'N/A')}")
                print(f"    상품명:   {_first(o, 'productName','productTitle','name','N/A')}")
                print(f"    수량:     {_first(o, 'quantity','qty', 1)}")
                print(f"    수령인:   {_first(o, 'receiverName','receiver', 'N/A')}")
                print()
        else:
            print("  (최근 주문 없음 - 정상 응답)")
        return True
    except Exception as e:
        print(f"[FAIL] 주문 조회 실패: {e}")
        _print_hint(e)
        return False


def test_orders_all_statuses(api: SmartstoreAPI, days: int = 30):
    separator(f"3. 전체 주문 상태별 건수 조회 (최근 {days}일)")
    statuses = [
        ("PAYED",       "결제완료"),
        ("DELIVERING",  "배송중"),
        ("DELIVERED",   "배송완료"),
        ("CANCELED",    "취소"),
    ]
    any_ok = False
    for code, label in statuses:
        try:
            orders = api.get_orders(status=code, days=days)
            print(f"  [{label:6}] {len(orders):4}건")
            any_ok = True
        except Exception as e:
            print(f"  [{label:6}] 조회 실패: {e}")
    return any_ok


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d:
            return d[k]
    return default


def _print_hint(e):
    msg = str(e)
    if "401" in msg:
        print("  힌트: client_id 또는 client_secret 값을 확인해 주세요.")
    elif "403" in msg:
        print("  힌트: API 권한(scope)이 없거나 seller_id가 잘못되었습니다.")
    elif "404" in msg:
        print("  힌트: API 엔드포인트 경로를 확인해 주세요.")
    elif "ConnectionError" in msg or "Failed to establish" in msg:
        print("  힌트: 네트워크 연결을 확인해 주세요.")


def main():
    print("\n[스마트스토어 API 연결 테스트]")
    print(f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    cfg = load_config("config/settings.yaml")
    ss_cfg = cfg["smartstore"]

    print(f"\n사용 설정:")
    print(f"  client_id  : {ss_cfg['client_id']}")
    print(f"  client_secret: {ss_cfg['client_secret'][:6]}{'*' * (len(ss_cfg['client_secret']) - 6)}")
    print(f"  seller_id  : {ss_cfg['seller_id']}")

    api = SmartstoreAPI(
        client_id=ss_cfg["client_id"],
        client_secret=ss_cfg["client_secret"],
    )

    results = []
    results.append(("토큰 발급",    test_token(api)))

    if results[0][1]:  # 토큰 성공 시에만 진행
        results.append(("주문 조회",   test_orders(api, days=3)))
        results.append(("상태별 조회", test_orders_all_statuses(api, days=3)))

    separator("테스트 결과 요약")
    all_pass = True
    for name, ok in results:
        status = "OK  " if ok else "FAIL"
        print(f"  [{status}] {name}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print("모든 테스트 통과 - API 연결 정상입니다.")
    else:
        print("일부 테스트 실패 - 위 힌트를 참고해 설정을 확인해 주세요.")
    print()


if __name__ == "__main__":
    main()
