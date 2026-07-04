"""KC 멀티인증 검증 디버그 — 읽기전용, 등록 안 함.

상품 58091305 + category_id=50002518 기준으로:
  1. 스크랩된 kc_certs 내용
  2. get_kc_cert_status 반환값 (각 인증별 cert_info_id + is_required)
  3. build_smartstore_payload 가 만들 cert_entries (name/companyName 포함)
  4. 카테고리 API 원문 certificationInfos 목록
"""
import io, sys, json, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from src.utils.config_loader import load_config
from src.api.smartstore import SmartstoreAPI
from src.api.domaemae import DomaemaeClient
from src.core.product_register import fetch_product_info

SEP = "=" * 60
PRODUCT_URL  = "https://domeme.domeggook.com/s/58091305"
CATEGORY_ID  = "50002518"
RRA_AGENCY   = "국립전파연구원"

# ── 클라이언트 초기화 ──────────────────────────────────────────
cfg = load_config()
_dm = cfg.get("domaemae", {})
dm_cli = DomaemaeClient(
    api_key  = _dm.get("api_key") or cfg["domaekkuk"].get("api_key", ""),
    user_id  = _dm.get("user_id", ""),
    password = _dm.get("password", ""),
)
ss_cfg = {k: v for k, v in cfg["smartstore"].items()
          if k in ("client_id", "client_secret", "account_type")}
ss_api = SmartstoreAPI(**ss_cfg)

# ── 1. 상품 스크랩 ─────────────────────────────────────────────
print(SEP)
print("[1] fetch_product_info → kc_certs")
info = fetch_product_info(PRODUCT_URL, dm_cli)
kc_certs = info.get("kc_certs", [])
print(f"  kc_certs 개수: {len(kc_certs)}")
for i, c in enumerate(kc_certs):
    print(f"\n  ── 인증 {i+1} ──")
    for k in ("cert_type", "cert_no", "cert_type_detail", "link_type",
               "agency", "company_name", "manufacturer_name", "model_name", "cert_date"):
        print(f"    {k:20s} = {c.get(k)!r}")

# ── 2. get_kc_cert_status 호출 ────────────────────────────────
print()
print(SEP)
print("[2] get_kc_cert_status 결과 (인증별)")
for i, cert in enumerate(kc_certs):
    hint = (cert.get("cert_type_detail") or cert.get("cert_type") or "").strip()
    is_req, info_id = ss_api.get_kc_cert_status(CATEGORY_ID, hint)
    cert["cert_info_id"] = info_id  # register_product과 동일하게 주입
    print(f"\n  인증 {i+1}  hint={hint!r}")
    print(f"    is_required   = {is_req}")
    print(f"    cert_info_id  = {info_id}  {'← 0이면 검증 실패' if not info_id else ''}")
    cert_no = (cert.get("cert_no") or "").strip()
    blocked = is_req and not (cert_no and info_id)
    print(f"    cert_no       = {cert_no!r}")
    print(f"    pre-check 차단? = {blocked}  (is_req={is_req}, cert_no={'있음' if cert_no else '없음'}, info_id={info_id})")

# ── 3. 최종 name(agency) / companyName 시뮬레이션 (새 로직) ──
print()
print(SEP)
print("[3] build_smartstore_payload 시뮬레이션 → cert_entries (new logic)")
info["rra_agency"] = RRA_AGENCY

def _is_rra_type(cert):
    combined = cert.get("cert_type_detail") or cert.get("cert_type") or ""
    return "방송통신기자재" in combined

valid_certs = [
    c for c in kc_certs
    if (c.get("cert_no") or "").strip() and int(c.get("cert_info_id") or 0)
]
kc_main_idx = next(
    (i for i, c in enumerate(valid_certs) if _is_rra_type(c)),
    0 if valid_certs else -1,
)
print(f"  kc_main_idx (KC_CERTIFICATION 담당) = {kc_main_idx}")

cert_entries = []
for idx, _cert in enumerate(valid_certs):
    _cert_info_id = int(_cert.get("cert_info_id") or 0)
    _cert_no      = (_cert.get("cert_no") or "").strip()
    is_rra        = _is_rra_type(_cert)
    kind_type     = "KC_CERTIFICATION" if idx == kc_main_idx else "ETC"

    _entry = {
        "certificationInfoId":   _cert_info_id,
        "certificationKindType": kind_type,
        "certificationNumber":   _cert_no,
    }
    _agency = (_cert.get("agency") or "").strip()
    if not _agency and is_rra:
        _agency = RRA_AGENCY or "국립전파연구원"
    if _agency:
        _entry["name"] = _agency
    if is_rra:
        _company = (_cert.get("company_name") or "").strip()
        if _company:
            _entry["companyName"] = _company

    cert_entries.append(_entry)
    print(f"\n  entry {len(cert_entries)} (is_rra={is_rra}):")
    print(json.dumps(_entry, ensure_ascii=False, indent=4))

print(f"\n  최종 cert_entries 개수: {len(cert_entries)}")

# ── 4. 카테고리 API 원문 certificationInfos ───────────────────
print()
print(SEP)
print(f"[4] GET /v1/categories/{CATEGORY_ID} 원문")
resp = requests.get(
    f"{ss_api.BASE_URL}/v1/categories/{CATEGORY_ID}",
    headers=ss_api._headers(),
    timeout=10,
)
print(f"  HTTP {resp.status_code}")
if resp.ok:
    data = resp.json()
    print(f"  exceptionalCategories : {data.get('exceptionalCategories')}")
    ci_list = data.get("certificationInfos", [])
    kc_list = [ci for ci in ci_list
               if ci.get("certificationMarkType") == "KC"
               and "KC_CERTIFICATION" in ci.get("kindTypes", [])]
    print(f"  certificationInfos 전체 수  : {len(ci_list)}")
    print(f"  KC_CERTIFICATION 해당 항목  : {len(kc_list)}")
    for ci in kc_list:
        print(f"    id={ci.get('id')}  name={ci.get('name')!r}  kindTypes={ci.get('kindTypes')}")
else:
    print(f"  응답 본문: {resp.text[:500]}")
