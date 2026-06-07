"""setOrder 실제 호출 테스트 — 요청/응답 전체 출력"""
import sys
import io
import json
import logging

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from src.api.domaemae import DomaemaeClient

client = DomaemaeClient(
    api_key="749082e66071934ef74df9d6e6511cda",
    user_id="lemoning",
    password="1qq@@34!!d",
)

PRODUCT_ID = "63641452"
OPTION_ID  = "00_00"
QUANTITY   = 1

shipping_info = {
    "name":                    "유승윤",
    "phone":                   "010-4734-8871",
    "zipcode":                 "46721",
    "address":                 "부산광역시 강서구 유통단지1로 41 (대제2동)",  # 통합주소 (domaekkuk용)
    "base_address":            "부산광역시 강서구 유통단지1로 41",
    "receiver_address_detail": "(대제2동)",
    "memo":                    "",
}

# ── 전송 data 딕셔너리 직접 구성 (place_order 내부 로직 그대로 재현) ──────
from src.api.domaemae import API_URL

client._ensure_session()

option_code = OPTION_ID
item_value  = f"supply||P||{option_code}|{QUANTITY}||||"

base_address   = shipping_info.get("base_address", "") or shipping_info.get("address", "")
detail_address = shipping_info.get("receiver_address_detail", "")
phone          = shipping_info["phone"]
email          = shipping_info.get("email", "") or "none@none.com"
name           = shipping_info["name"]
# 8개 필드(파이프 7개) 고정: 이름|이메일|우편번호|기본주소|상세주소|휴대폰|전화번호|상호명
deliinfo = f"{name}|{email}|{shipping_info['zipcode']}|{base_address}|{detail_address}|{phone}|{phone}|{name}"

data = {
    f"item[{PRODUCT_ID}]": item_value,
    "deliinfo":            deliinfo,
    "receipt":             "0",
}

print("\n" + "=" * 60)
print("▶ 전송 data 딕셔너리:")
print(json.dumps(data, ensure_ascii=False, indent=2))
print(f"\n▶ deliinfo 슬롯 분해:")
for i, v in enumerate(deliinfo.split("|"), start=1):
    labels = {1:"이름", 2:"이메일", 3:"우편번호", 4:"기본주소", 5:"상세주소", 6:"휴대폰", 7:"전화번호", 8:"상호명"}
    print(f"  [{i}] {labels.get(i, '?')}: {repr(v)}")
print("=" * 60 + "\n")

# ── 실제 API 호출 ────────────────────────────────────────────────────────────
print("▶ setOrder 호출 중...")
try:
    order_no = client.place_order(
        PRODUCT_ID, QUANTITY, shipping_info,
        option_id=OPTION_ID,
    )
    print(f"\n[성공] 발주번호: {order_no}")
except Exception as e:
    print(f"\n[실패] {e}")
    sys.exit(1)

# ── 즉시 취소 ────────────────────────────────────────────────────────────────
print("\n▶ setOrdDeny 호출 (즉시 취소)...")
try:
    result = client.cancel_order(order_no)
    print(f"[취소 완료] 응답: {result}")
except Exception as e:
    print(f"[취소 실패] {e}")
    print(f"  → 도매매 사이트에서 발주번호 {order_no} 수동 취소 필요")
