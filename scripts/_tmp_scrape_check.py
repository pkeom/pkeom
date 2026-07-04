"""상품 58091305 스크랩 가능 필드 전체 확인 (읽기 전용)."""
import io, sys, json, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

import requests
from bs4 import BeautifulSoup

from src.utils.config_loader import load_config
from src.api.domaemae import DomaemaeClient
from src.core.product_register import _make_session, _retry_get, _domaemae_api

PRODUCT_ID = "58091305"
BASE_URL   = f"https://domeme.domeggook.com/s/{PRODUCT_ID}"
SEP = "=" * 60

out = []
def p(*args): out.append(" ".join(str(a) for a in args))

# ── 페이지 fetch ──────────────────────────────────────────────────
session = _make_session(referer="https://domeme.domeggook.com/")
resp = _retry_get(session, BASE_URL)
resp.encoding = "euc-kr"
soup = BeautifulSoup(resp.text, "lxml")

# ════════════════════════════════════════════════════════════════
# 1. KC 인증 전부 (select_all)
# ════════════════════════════════════════════════════════════════
p(SEP)
p("[1] KC 인증 블록 전체")
cert_blocks = soup.select("div.lCert.lHasImg")
p(f"  총 {len(cert_blocks)}개\n")

for i, blk in enumerate(cert_blocks):
    title_el = blk.select_one("div.lCertTitle")
    num_el   = blk.select_one("div.lCertNum")
    link_el  = blk.select_one("div.lCertNum a[href]")

    raw_title = title_el.get_text(strip=True) if title_el else ""
    m = re.search(r"\[(.+?)\]", raw_title)
    cert_type = m.group(1).strip() if m else raw_title

    raw_num = num_el.get_text(strip=True) if num_el else ""
    cert_no = re.split(r"자세히", raw_num)[0].strip()

    link_url = link_el["href"].strip() if link_el else ""
    # 내부 상대경로 → 절대경로
    if link_url and link_url.startswith("/"):
        link_url = "https://domeme.domeggook.com" + link_url

    p(f"  [{i}] 유형={cert_type!r}  번호={cert_no!r}")
    p(f"       링크={link_url!r}")

    # safetykorea.kr 상세 파싱
    if "safetykorea.kr" in link_url:
        try:
            sk_sess = _make_session(referer="https://www.safetykorea.kr/")
            sk_resp = _retry_get(sk_sess, link_url)
            sk_soup = BeautifulSoup(sk_resp.text, "lxml")
            agency = cert_type_detail = comp_name = ""
            for cell in sk_soup.find_all(["th", "td"]):
                label = cell.get_text(strip=True)
                nxt = cell.find_next_sibling("td") or cell.find_next("td")
                val = nxt.get_text(strip=True) if nxt else ""
                if label == "인증기관" and not agency:
                    agency = val
                elif label == "인증구분" and not cert_type_detail:
                    cert_type_detail = val.split(">")[-1].strip() if ">" in val else val
                elif label in ("신청자상호", "상호명") and not comp_name:
                    comp_name = val
            p(f"       [safetykorea 파싱]")
            p(f"         인증기관(agency)   = {agency!r}")
            p(f"         인증구분(type hint) = {cert_type_detail!r}")
            p(f"         신청자상호          = {comp_name!r}")
        except Exception as e:
            p(f"       [safetykorea 파싱 실패] {e}")

    # 내부 rraCert 페이지 파싱
    elif "rraCert" in link_url or "item_rra" in link_url:
        try:
            rra_sess = _make_session(referer=BASE_URL)
            rra_resp = _retry_get(rra_sess, link_url)
            ct_header = rra_resp.headers.get("Content-Type", "")
            p(f"       [rraCert HTTP {rra_resp.status_code}  Content-Type: {ct_header}]")

            # cp949 우선 (EUC-KR 상위호환, Windows 한국어 표준)
            for enc in ("cp949", "euc-kr", "utf-8"):
                try:
                    decoded = rra_resp.content.decode(enc)
                    break
                except Exception:
                    continue
            else:
                decoded = rra_resp.content.decode("utf-8", errors="replace")

            rra_soup = BeautifulSoup(decoded, "lxml")
            charset_meta = rra_soup.find("meta", {"charset": True}) or rra_soup.find("meta", {"http-equiv": re.compile("content-type", re.I)})
            p(f"       charset meta: {charset_meta}")

            # th/td 테이블 전체 출력
            rows = []
            for th in rra_soup.find_all("th"):
                label = th.get_text(strip=True)
                td = th.find_next_sibling("td") or th.find_next("td")
                val = td.get_text(strip=True) if td else ""
                if label:
                    rows.append(f"{label}: {val}")
            if rows:
                for r in rows:
                    p(f"         {r}")
            else:
                body_text = rra_soup.get_text(" ", strip=True)[:800]
                p(f"         본문(텍스트): {body_text!r}")
        except Exception as e:
            p(f"       [rraCert 파싱 실패] {e}")
    else:
        p(f"       [미분류 링크 — 미처리]")
    p("")

# ════════════════════════════════════════════════════════════════
# 2. 모델명 / 제조사 / 제조일자 (HTML 셀렉터)
# ════════════════════════════════════════════════════════════════
p(SEP)
p("[2] 모델명 / 제조사 / 제조일자 (HTML 직접 탐색)")

# HTML에서 th/td 라벨 기반 탐색
LABELS = {
    "모델명": ["모델명", "모델", "Model"],
    "제조사": ["제조사", "제조업체", "브랜드", "Brand", "제조원"],
    "제조일자": ["제조일자", "제조일", "제조년월", "유통기한"],
}
found_html = {k: "" for k in LABELS}
selector_used = {k: "" for k in LABELS}

for th in soup.find_all("th"):
    label = th.get_text(strip=True)
    td = th.find_next_sibling("td") or th.find_next("td")
    val = td.get_text(strip=True) if td else ""
    for field, keywords in LABELS.items():
        if not found_html[field] and any(kw in label for kw in keywords):
            found_html[field] = val
            selector_used[field] = f"th[text~={label!r}] → next td"

for field in LABELS:
    v = found_html[field]
    sel = selector_used[field] or "없음 (HTML에서 미발견)"
    p(f"  {field}: {v!r}  ← {sel}")

# API 응답에서 확인
p("")
p("[2-b] getItemView API 응답의 model/manufacturer 필드")
cfg = load_config()
_dm = cfg.get("domaemae", {})
dm_cli = DomaemaeClient(
    api_key    = _dm.get("api_key") or cfg["domaekkuk"].get("api_key", ""),
    user_id    = _dm.get("user_id", ""),
    password   = _dm.get("password", ""),
    store_name = cfg.get("store_name", "엘에이(LA)"),
)
try:
    raw = _domaemae_api(PRODUCT_ID, dm_cli)
    basis = raw.get("item", raw.get("basis", raw))
    api_model = basis.get("model") or basis.get("modelNo") or ""
    api_maker = basis.get("manufacturer") or basis.get("maker") or basis.get("brand") or basis.get("brandName") or ""
    api_date  = basis.get("manufactureDate") or basis.get("mfgDate") or ""
    p(f"  API model       = {api_model!r}")
    p(f"  API manufacturer= {api_maker!r}")
    p(f"  API mfgDate     = {api_date!r}")
    # 관련 키 전체 덤프 (참고용)
    relevant_keys = {k: v for k, v in basis.items()
                     if any(kw in k.lower() for kw in ["model","maker","brand","manuf","date","mfg"])}
    p(f"  관련 키 전체: {json.dumps(relevant_keys, ensure_ascii=False)}")
except Exception as e:
    p(f"  API 호출 실패: {e}")

# ════════════════════════════════════════════════════════════════
# 3. 정리
# ════════════════════════════════════════════════════════════════
p("")
p(SEP)
p("[3] 자동 스크랩 가능 vs 수동 필요 (확인 결과 기준)")
p("")
p("  ┌─────────────────────────────┬──────────────────────────────────────────────────────┐")
p("  │ 필드                        │ 상태                                                 │")
p("  ├─────────────────────────────┼──────────────────────────────────────────────────────┤")
p("  │ KC 방송통신기자재 인증번호   │ ✓ div.lCert.lHasImg → lCertNum 텍스트               │")
p("  │ KC 방송통신기자재 모델명     │ ✓ rraCert 페이지 th[모델명] td  (JF137)              │")
p("  │ KC 방송통신기자재 신청자명   │ ✓ rraCert 페이지 th[신청자명] td (JIEMEIXUN...)       │")
p("  │ KC 방송통신기자재 인증일자   │ ✓ rraCert 페이지 th[인증일자] td (2025-04-09)        │")
p("  │ KC 방송통신기자재 인증기관   │ ✗ rraCert에 없음 — 현재 코드 수집 불가, 빈값         │")
p("  ├─────────────────────────────┼──────────────────────────────────────────────────────┤")
p("  │ KC 전기용품 인증번호         │ ✓ div.lCert.lHasImg → lCertNum 텍스트               │")
p("  │ KC 전기용품 인증기관         │ ✓ safetykorea.kr popup th[인증기관] td               │")
p("  │ KC 전기용품 인증구분         │ ✓ safetykorea.kr popup th[인증구분] td               │")
p("  │ KC 전기용품 신청자상호        │ ✗ safetykorea popup에서 빈값으로 파싱됨              │")
p("  ├─────────────────────────────┼──────────────────────────────────────────────────────┤")
p("  │ 상품명/공급가/재고/이미지    │ ✓ 기존 _scrape_domaemae 처리                         │")
p("  │ 원산지/카테고리              │ ✓ 기존 처리                                           │")
p("  ├─────────────────────────────┼──────────────────────────────────────────────────────┤")
p("  │ 모델명 (상품정보)            │ ✗ HTML/API 모두 빈값                                  │")
p("  │ 제조사 (상품정보)            │ ✗ HTML/API 모두 빈값                                  │")
p("  │ 제조일자                     │ ✗ HTML/API 모두 없음                                  │")
p("  └─────────────────────────────┴──────────────────────────────────────────────────────┘")
p("")
p("  ※ 방송통신기자재 기관(agency)은 현재 차단조건에서 제외됨 (product_register.py 수정 완료)")
p("  ※ 모델명/제조사는 rraCert에서 긁을 수 있으나 _scrape_domaemae에 아직 미통합")

result_file = "scripts/_tmp_scrape_out.txt"
text = "\n".join(out)
print(text)
with open(result_file, "w", encoding="utf-8") as f:
    f.write(text)
