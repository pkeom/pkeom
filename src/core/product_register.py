"""도매매/도매꾹 상품 → 스마트스토어 자동 등록"""
import datetime
import itertools
import json
import math
import re
import logging
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_CATEGORY_CACHE_PATH = Path(__file__).parents[2] / "data" / "category_mapping_cache.json"


def _load_category_cache() -> dict:
    try:
        if _CATEGORY_CACHE_PATH.exists():
            return json.loads(_CATEGORY_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_category_cache(cache: dict) -> None:
    try:
        _CATEGORY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CATEGORY_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("카테고리 캐시 저장 실패: %s", e)

# ── 상수 ───────────────────────────────────────────────────────────

_DOME_API_URL = "https://domeggook.com/ssl/api/"
_TIMEOUT      = 30
_MAX_RETRIES  = 3

# 브라우저 공통 헤더
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent":      _BROWSER_UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}
# API 전용 헤더 (JSON 응답)
_API_HEADERS = {
    "User-Agent":      _BROWSER_UA,
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 카테고리 키워드 → 스마트스토어 leafCategoryId 간이 매핑
_CATEGORY_MAP = {
    "패션의류": "50000000", "패션잡화": "50000001",
    "화장품":   "50000002", "미용":     "50000002",
    "디지털":   "50000003", "가전":     "50000003",
    "가구":     "50000004", "인테리어": "50000004",
    "출산":     "50000005", "육아":     "50000005",
    "식품":     "50000006",
    "스포츠":   "50000007", "레저":     "50000007",
    "생활":     "50000008", "건강":     "50000008",
    "완구":     "50000011", "취미":     "50000011",
    "문구":     "50000012", "오피스":   "50000012",
    "반려동물": "50000013", "자동차":   "50000014",
}


def _parse_price(raw) -> int:
    """가격 파싱: 단순 정수 또는 수량별 가격 형식(예: '1+2700|10+2650|...')을 처리.

    수량별 가격이면 1개 단위 가격(첫 번째 항목)을 사용한다.
    """
    if not raw:
        return 0
    s = str(raw).strip()
    if not s:
        return 0
    # 수량별 가격 형식: "1+2700|10+2650|50+2630" → 2700
    if "+" in s or "|" in s:
        first = s.split("|")[0]          # "1+2700"
        part  = first.split("+")[-1]     # "2700"
        try:
            return int(re.sub(r"[^\d]", "", part))
        except ValueError:
            pass
    try:
        return int(re.sub(r"[^\d]", "", s))
    except ValueError:
        return 0


# ── 세션 팩토리 ────────────────────────────────────────────────────

def _make_session(referer: str = "") -> requests.Session:
    """브라우저 헤더 + 재시도 어댑터가 달린 Session 생성."""
    session = requests.Session()

    # 5xx, 429 에 대해 최대 3회 재시도 (지수 백오프)
    retry = Retry(
        total=_MAX_RETRIES,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)

    session.headers.update(_BROWSER_HEADERS)
    if referer:
        session.headers["Referer"] = referer
    return session


def _retry_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """403 등 일시적 차단에 대해 수동 재시도 (1 s 간격)."""
    kwargs.setdefault("timeout", _TIMEOUT)
    last_exc: Exception = RuntimeError("no attempt")
    for attempt in range(_MAX_RETRIES):
        try:
            resp = session.get(url, **kwargs)
            if resp.status_code == 403 and attempt < _MAX_RETRIES - 1:
                logger.debug("403 수신 (%s), %d초 후 재시도", url, attempt + 1)
                time.sleep(attempt + 1)
                continue
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt < _MAX_RETRIES - 1:
                time.sleep(attempt + 1)
    raise last_exc


def _retry_post(session: requests.Session, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", _TIMEOUT)
    last_exc: Exception = RuntimeError("no attempt")
    for attempt in range(_MAX_RETRIES):
        try:
            resp = session.post(url, **kwargs)
            if resp.status_code == 403 and attempt < _MAX_RETRIES - 1:
                time.sleep(attempt + 1)
                continue
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt < _MAX_RETRIES - 1:
                time.sleep(attempt + 1)
    raise last_exc


# ── 공급처 인증 이미지 다운로드 ─────────────────────────────────────────

_IMG_EXT_MAP = {
    "image/jpeg": ".jpg", "image/png": ".png",
    "image/gif": ".gif",  "image/bmp": ".bmp", "image/webp": ".webp",
}


def _detect_image_type(data: bytes) -> str | None:
    """바이트 시그니처(magic bytes)로 이미지 MIME 타입 감지. 알 수 없으면 None."""
    if not data:
        return None
    # JPEG: SOI 마커 FF D8 (이후 바이트 무관)
    if data[0] == 0xFF and data[1] == 0xD8:
        return "image/jpeg"
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return "image/png"
    if data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
        return "image/gif"
    if data.startswith(b'RIFF') and len(data) >= 12 and data[8:12] == b'WEBP':
        return "image/webp"
    if data.startswith(b'BM'):
        return "image/bmp"
    return None


def _make_image_session(supplier: str, product_id: str, supplier_client) -> requests.Session:
    """이미지 다운로드용 세션 생성.

    공급처 상품 페이지를 먼저 방문해 CloudFront 등 CDN의 세션 쿠키를 취득하고,
    이후 모든 이미지 다운로드에 이 세션을 재사용한다.
    """
    if supplier == "domaemae":
        supplier_client._ensure_session()
        page_url = f"https://domeme.domeggook.com/s/{product_id}"
        referer  = "https://domeme.domeggook.com/"
    else:
        page_url = f"https://www.domeggook.com/{product_id}"
        referer  = "https://www.domeggook.com/"

    session = _make_session(referer=referer)
    session.headers.update({"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"})

    if supplier == "domaemae":
        sid = getattr(supplier_client, "_sid", "")
        if sid:
            session.cookies.set("sId", sid, domain=".domeggook.com")

    try:
        resp = session.get(page_url, timeout=15)
        logger.debug("이미지 세션 페이지 방문: %s → %s (cookies: %s)",
                     page_url, resp.status_code, list(session.cookies.keys()))
    except Exception as e:
        logger.debug("이미지 세션 페이지 방문 실패 (계속 진행): %s", e)

    return session


def _fetch_image_bytes(url: str, session: requests.Session) -> tuple[bytes, str, str]:
    """주어진 세션으로 이미지 URL에서 바이트 데이터 다운로드.

    Returns: (raw_bytes, content_type, filename)
    """
    resp = _retry_get(session, url)
    raw_ct   = resp.headers.get("Content-Type", "(없음)")
    first4   = resp.content[:4] if resp.content else b""
    logger.info(
        "이미지 다운로드: status=%s  Content-Type=%s  첫4바이트=%s  size=%d  url=%s",
        resp.status_code, raw_ct, first4.hex(), len(resp.content), url[:100],
    )

    if not resp.ok:
        raise requests.HTTPError(
            f"이미지 다운로드 실패 {resp.status_code}: {url}", response=resp
        )

    content_type = raw_ct.split(";")[0].strip()
    if not content_type.startswith("image/"):
        # application/octet-stream 등 비이미지 Content-Type → magic bytes로 실제 타입 감지
        detected = _detect_image_type(resp.content)
        if detected:
            logger.info("magic bytes 감지: %s → %s", content_type, detected)
            content_type = detected
        else:
            raise ValueError(
                f"이미지 URL이 유효한 이미지를 반환하지 않음 "
                f"(Content-Type: {content_type}, 첫4바이트: {first4.hex()}): {url}"
            )

    ext = _IMG_EXT_MAP.get(content_type, ".jpg")
    filename = url.split("/")[-1].split("?")[0] or f"image{ext}"
    if not any(filename.lower().endswith(e) for e in _IMG_EXT_MAP.values()):
        filename += ext

    return resp.content, content_type, filename


def _is_ui_image(src: str) -> bool:
    """도매꾹/도매매 사이트 UI 아이콘·배경 이미지 여부 판별."""
    fname = src.split("/")[-1].split("?")[0].lower()
    if any(fname.startswith(p) for p in ("ico_", "bg_", "btn_", "icon_")):
        return True
    if "/image/item/" in src or "/image/common/" in src:
        return True
    return False


# ── URL 파싱 ───────────────────────────────────────────────────────

def extract_product_id(url: str) -> tuple[str, str]:
    """URL 또는 숫자 ID → (supplier, product_id).

    지원 형식:
      domeggook.com/{no}              → domaekkuk
      domeme.domeggook.com/s/{no}     → domaemae
      domaemae.co.kr/...?no={no}      → domaemae
      숫자                            → domaekkuk (기본)
    """
    url = url.strip()

    # 도매매: domeme.domeggook.com/s/{no}
    m = re.search(r"domeme\.domeggook\.com/s/(\d+)", url)
    if m:
        return "domaemae", m.group(1)

    # 도매꾹: www.domeggook.com/{no}  (domeme 제외)
    m = re.search(r"(?<!domeme\.)domeggook\.com/(\d+)", url)
    if m:
        return "domaekkuk", m.group(1)

    # 구 도매매: domaemae.co.kr
    m = re.search(r"domaemae\.co\.kr.*?[?&]no=(\d+)", url)
    if m:
        return "domaemae", m.group(1)
    m = re.search(r"domaemae\.co\.kr/(\d+)", url)
    if m:
        return "domaemae", m.group(1)

    # 숫자만
    if re.fullmatch(r"\d+", url):
        return "domaekkuk", url

    raise ValueError(f"지원하지 않는 URL 형식: {url}")


# ── 도우미 ──────────────────────────────────────────────────────────

def _fix_url(src: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/") and not src.startswith("//"):
        return "https://www.domeggook.com" + src
    return src


def _parse_options(select_opt) -> list[dict]:
    """selectOpt → 옵션 그룹 목록: [{"name": str, "values": [str, ...]}]

    신규 API(2024+): selectOpt는 JSON 문자열이며
    {"type":"combination","set":[{"name":str,"opts":[str,...]},...]} 형태.
    """
    if not select_opt:
        return []
    if isinstance(select_opt, str):
        try:
            data = json.loads(select_opt)
        except Exception:
            return []
    else:
        data = select_opt
    if not isinstance(data, dict):
        return []

    if "set" in data:
        result = []
        for grp in data["set"]:
            if not isinstance(grp, dict):
                continue
            name   = str(grp.get("name", "옵션")).strip() or "옵션"
            values = [str(v).strip() for v in grp.get("opts", []) if str(v).strip()]
            if values:
                result.append({"name": name, "values": values})
        return result

    # 구형 형식: {"CODE": {"name": str, "hid": int}}
    flat: list[str] = []
    seen: set[str]  = set()
    for code, info in data.items():
        if not isinstance(info, dict):
            continue
        if int(info.get("hid", 0)) == 2:
            continue
        n = str(info.get("name", "")).strip()
        if n and code not in seen:
            flat.append(n)
            seen.add(code)
    return [{"name": "옵션", "values": flat}] if flat else []


def _normalize_opt(s: str) -> str:
    """옵션명 정규화 (매칭용): 소문자, 공백·슬래시·괄호 제거."""
    return re.sub(r"[\s/()（）]", "", s).lower()


def _parse_option_code_map(select_opt) -> dict[str, str]:
    """selectOpt.data → {normalized_full_name: combo_code} 딕셔너리.

    도매매 selectOpt.data 예:
      {"00_00": {"name": "BM-106 아이스롤러/올핑크", "hid": "0", ...}, ...}
    → {"bm-106아이스롤러올핑크": "00_00", ...}
    """
    if not select_opt:
        return {}
    if isinstance(select_opt, str):
        try:
            root = json.loads(select_opt)
        except Exception:
            return {}
    else:
        root = select_opt
    if not isinstance(root, dict):
        return {}
    data = root.get("data") or {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, str] = {}
    for code, info in data.items():
        if not isinstance(info, dict):
            continue
        try:
            hid = int(info.get("hid", 0))
        except (TypeError, ValueError):
            hid = 0
        if hid == 2:
            continue
        name = str(info.get("name", "")).strip()
        if name:
            result[_normalize_opt(name)] = str(code)
    return result


def _match_supplier_option(option_code_map: dict[str, str], combo_vals: list[str]) -> str:
    """SS 옵션 콤보 값 목록 → 도매처 옵션코드.

    매칭 순서:
      1. 정규화 후 직접 일치
      2. 도매처 이름 끝이 정규화 콤보로 끝나는지 (접두사 "상품명/" 형태 대응)
    없으면 "" 반환.
    """
    if not option_code_map or not combo_vals:
        return ""
    normalized = "".join(_normalize_opt(v) for v in combo_vals)
    if normalized in option_code_map:
        return option_code_map[normalized]
    for key, code in option_code_map.items():
        if key.endswith(normalized):
            return code
    return ""


def _parse_category(cat_d) -> str:
    if isinstance(cat_d, dict):
        # 신규 API 형식: {"parents": {"elem": [{"name":..}]}, "current": {"name":..}}
        if "parents" in cat_d or "current" in cat_d:
            parts = []
            for p in cat_d.get("parents", {}).get("elem", []):
                if isinstance(p, dict) and p.get("name"):
                    parts.append(p["name"])
            cur = cat_d.get("current", {})
            if isinstance(cur, dict) and cur.get("name"):
                parts.append(cur["name"])
            return " > ".join(parts) if parts else ""
        return (cat_d.get("name") or cat_d.get("cateName") or
                cat_d.get("categoryName") or "")
    if isinstance(cat_d, list) and cat_d:
        return str(cat_d[-1])
    return ""


def _parse_images(img_d: dict) -> tuple[str, list[str], str]:
    """(main_image, sub_images, detail_html)"""
    if not isinstance(img_d, dict):
        return "", [], ""

    raw_main = img_d.get("main") or img_d.get("big") or img_d.get("url") or ""
    if isinstance(raw_main, list):
        raw_main = raw_main[0] if raw_main else ""
    main = _fix_url(str(raw_main)) if raw_main else ""

    raw_sub = img_d.get("sub") or img_d.get("list") or []
    subs = [_fix_url(str(s)) for s in raw_sub if s][:9] if isinstance(raw_sub, list) else []

    raw_detail = img_d.get("detail") or ""
    detail_html = raw_detail if isinstance(raw_detail, str) and raw_detail.startswith("<") else ""

    return main, subs, detail_html


# ── safetykorea.kr KC 인증 상세 수집 ──────────────────────────────

def _fetch_kc_cert_detail(cert_url: str) -> tuple[str, str]:
    """safetykorea.kr 인증 팝업에서 인증기관명과 인증구분을 파싱.

    Returns: (kc_cert_agency, cert_type_leaf)
    cert_type_leaf: 인증구분 '>' 뒤 마지막 항목 원문 (예: "안전확인대상 전기용품")
                    → certificationInfoId 조회 힌트로 사용
    """
    try:
        session = _make_session(referer="https://www.safetykorea.kr/")
        resp = _retry_get(session, cert_url)
        if not resp.ok:
            logger.warning("safetykorea.kr 요청 실패: HTTP %s (%s)", resp.status_code, cert_url)
            return "", ""

        soup = BeautifulSoup(resp.text, "lxml")
        agency = ""
        cert_type_detail = ""

        for cell in soup.find_all(["th", "td"]):
            label = cell.get_text(strip=True)
            if label == "인증기관" and not agency:
                nxt = cell.find_next_sibling("td") or cell.find_next("td")
                if nxt:
                    agency = nxt.get_text(strip=True)
            elif label == "인증구분" and not cert_type_detail:
                nxt = cell.find_next_sibling("td") or cell.find_next("td")
                if nxt:
                    raw = nxt.get_text(strip=True)
                    # "법률명>인증유형" → 마지막 항목 (certificationInfoId 조회 힌트)
                    cert_type_detail = raw.split(">")[-1].strip() if ">" in raw else raw

        if agency:
            logger.info("KC 인증 상세 수집: agency=%r, cert_type=%r", agency, cert_type_detail)
        else:
            logger.warning("KC 인증기관 파싱 실패 — 페이지 구조 변경 가능성: %s", cert_url)

        return agency, cert_type_detail

    except Exception as e:
        logger.warning("KC 인증 상세 수집 실패 (%s): %s", cert_url, e)
        return "", ""


# ── 도매꾹 ─────────────────────────────────────────────────────────

def _domaekkuk_api(product_id: str, api_key: str) -> dict:
    """도매꾹 getItemView API 호출 (브라우저 헤더 포함)."""
    referer = f"https://www.domeggook.com/{product_id}"
    session = _make_session(referer)
    session.headers.update(_API_HEADERS)
    session.headers["Referer"] = referer
    session.headers["Origin"]  = "https://www.domeggook.com"

    params = {
        "mode": "getItemView",
        "ver":  "4.5",
        "no":   product_id,
        "aid":  api_key,
        "om":   "json",
    }
    resp = _retry_get(session, _DOME_API_URL, params=params)
    resp.raise_for_status()
    return resp.json().get("domeggook", resp.json())


def _scrape_domaekkuk(product_id: str) -> dict:
    """도매꾹 상품 페이지 스크래핑 (실제 HTML 구조 기반)."""
    page_url = f"https://www.domeggook.com/{product_id}"
    session  = _make_session(referer="https://www.domeggook.com/")

    try:
        resp          = _retry_get(session, page_url)
        resp.encoding = "euc-kr"  # 도매꾹 EUC-KR 인코딩 명시 설정 (필수)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("도매꾹 스크래핑 실패(%s): %s", product_id, e)
        return {}

    soup   = BeautifulSoup(resp.text, "lxml")
    result: dict = {}

    # ── 1. 상품명 ─────────────────────────────────────────────────
    og_title = soup.find("meta", {"property": "og:title"})
    if og_title:
        title = re.sub(r"^\[도매꾹\]\s*", "", og_title.get("content", "")).strip()
        if title:
            result["title"] = title
    if not result.get("title"):
        h1 = soup.select_one("h1#lInfoItemTitle, h1.lInfoRow")
        if h1:
            result["title"] = h1.get_text(strip=True)

    # ── 2. 공급가 ─────────────────────────────────────────────────
    price_el = soup.select_one("div.lItemPrice")
    if price_el:
        digits = re.sub(r"[^\d]", "", price_el.get_text())
        if digits:
            result["supply_price"] = int(digits)
    if not result.get("supply_price"):
        og_desc = soup.find("meta", {"property": "og:description"})
        if og_desc:
            m = re.search(r"([\d,]+)원", og_desc.get("content", ""))
            if m:
                result["supply_price"] = int(m.group(1).replace(",", ""))

    # ── 3. 재고수량 ───────────────────────────────────────────────
    qty_el = soup.select_one("tr.lInfoQty td.lInfoItemContent")
    if not qty_el:
        qty_el = soup.select_one("tr.lInfoQty td")
    if qty_el:
        m = re.search(r"[\d,]+", qty_el.get_text())
        if m:
            result["stock"] = int(m.group(0).replace(",", ""))

    # ── 4. 대표이미지 ─────────────────────────────────────────────
    og_img = soup.find("meta", {"property": "og:image"})
    if og_img:
        result["main_image"] = og_img.get("content", "")

    # ── 5. 추가이미지 (#lThumbImgWrap a.thumbLightbox) ────────────
    sub_imgs: list[str] = []
    for a in soup.select("#lThumbImgWrap a.thumbLightbox"):
        img = a.find("img")
        if img:
            src = _fix_url(img.get("src") or img.get("data-src", ""))
            if src and src not in sub_imgs:
                sub_imgs.append(src)
    result["sub_images"] = sub_imgs[:9]

    # ── 6. 원산지 ─────────────────────────────────────────────────
    origin_el = soup.select_one("tr.lInfoItemCountry td.lInfoItemCountryContent")
    if origin_el:
        result["origin"] = origin_el.get_text(strip=True)

    # ── 7. 최소구매수량 ───────────────────────────────────────────
    minqty_el = soup.select_one("tr.lInfoPurchase td.lInfoItemContent")
    if minqty_el:
        digits = re.sub(r"[^\d]", "", minqty_el.get_text())
        result["min_qty"] = int(digits) if digits else 1
    else:
        result["min_qty"] = 1

    # ── 8. KC 인증 ────────────────────────────────────────────────
    # lCertTitle 형식: "[인증유형] 제품분류" (예: "[안전인증] 선풍기")
    # 인증기관명은 "자세히보기" 링크(safetykorea.kr)에서 수집
    cert_el = soup.select_one("div.lCert.lHasImg")
    if cert_el:
        cert_title = cert_el.select_one("div.lCertTitle")
        if cert_title:
            m = re.search(r"\[(.+?)\]", cert_title.get_text(strip=True))
            if m:
                cert_type = m.group(1).strip()
                if cert_type:
                    result["kc_cert_type"] = cert_type
        cert_num = cert_el.select_one("div.lCertNum")
        if cert_num:
            raw_num = cert_num.get_text(strip=True)
            raw_num = re.split(r"자세히", raw_num)[0].strip()
            if raw_num:
                result["kc_cert_no"] = raw_num
            # safetykorea.kr "자세히보기" 링크에서 인증기관명·인증구분 수집
            cert_link = cert_num.select_one("a[href]")
            if cert_link:
                detail_url = cert_link.get("href", "").strip()
                if detail_url and "safetykorea.kr" in detail_url:
                    agency, type_detail = _fetch_kc_cert_detail(detail_url)
                    if agency:
                        result["kc_cert_agency"] = agency
                    if type_detail:
                        result["kc_cert_type"] = type_detail

    # ── 9. 카테고리 (breadcrumb #lPath) ──────────────────────────
    cat_parts: list[str] = []
    cat_code = ""
    lcat2 = soup.find(id="lPathCat2")
    if lcat2:
        a2 = lcat2.find("a")
        txt = a2.get_text(strip=True) if a2 else lcat2.get_text(strip=True)
        if txt:
            cat_parts.append(txt)
    for n in range(3, 8):
        cat_el = soup.find(id=f"lPathCat{n}")
        if not cat_el:
            break
        first_a = cat_el.find("a")
        if not first_a:
            break
        href = first_a.get("href", "")
        txt  = first_a.get_text(strip=True)
        m    = re.search(r"ca=([\w_]+)", href)
        if m:
            cat_code = m.group(1)
        if txt:
            cat_parts.append(txt)
    result["category_name"] = " > ".join(cat_parts)
    result["category_code"] = cat_code  # 예: "17_05_07_06_00"

    # ── 10. 상세설명 이미지 (supportListFrame iframe) ─────────────
    try:
        iframe_url = (
            f"https://www.domeggook.com"
            f"/main/item/itemView/supportListFrame.php?no={product_id}"
        )
        det_resp          = _retry_get(session, iframe_url)
        det_resp.encoding = "euc-kr"
        if det_resp.ok:
            det_soup = BeautifulSoup(det_resp.text, "lxml")
            # 문의게시판·스크립트·폼 등 불필요 요소 제거
            for el in det_soup.select("#lSupportList, script, form, style, iframe"):
                el.decompose()
            det_imgs: list[str] = []
            for img in det_soup.find_all("img"):
                src = _fix_url(img.get("src") or img.get("data-src", ""))
                if src and ("cdn" in src or "upload" in src) and src not in det_imgs:
                    det_imgs.append(src)
            result["detail_images"] = det_imgs
            body = det_soup.find("body")
            result["detail_html"]   = str(body) if body else ""
    except Exception as e:
        logger.debug("도매꾹 상세이미지 수집 실패: %s", e)

    return result


# ── 도매매 ─────────────────────────────────────────────────────────

def _domaemae_api(product_id: str, client) -> dict:
    """도매매 getItemView API 호출 (DomaemaeClient 세션 재활용)."""
    # 세션(sId) 보장
    client._ensure_session()

    referer = f"https://domeme.domeggook.com/s/{product_id}"
    session = _make_session(referer)
    session.headers.update(_API_HEADERS)
    session.headers["Referer"] = referer
    session.headers["Origin"]  = "https://domeme.domeggook.com"

    params = {
        "mode": "getItemView",
        "ver":  "4.5",
        "no":   product_id,
        "aid":  client.api_key,
        "sId":  client._sid,
        "om":   "json",
    }
    resp = _retry_get(session, _DOME_API_URL, params=params)
    resp.raise_for_status()
    return resp.json().get("domeggook", resp.json())


def _scrape_domaemae(product_id: str) -> dict:
    """도매매 상품 페이지 스크래핑 (domeme.domeggook.com — 실제 HTML 구조 기반).

    도매매는 domeme.domeggook.com 에서 서비스하며 도매꾹과 동일한 HTML 구조를 사용한다.
    단, 비로그인 상태에서 가격·옵션이 숨겨지므로 og:description으로 가격을 보완한다.
    """
    page_url = f"https://domeme.domeggook.com/s/{product_id}"
    session  = _make_session(referer="https://domeme.domeggook.com/")

    try:
        resp          = _retry_get(session, page_url)
        resp.encoding = "euc-kr"  # 도매매도 EUC-KR 인코딩 명시 설정 (필수)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("도매매 스크래핑 실패(%s): %s", product_id, e)
        return {}

    soup   = BeautifulSoup(resp.text, "lxml")
    result: dict = {}

    # ── 1. 상품명 ─────────────────────────────────────────────────
    og_title = soup.find("meta", {"property": "og:title"})
    if og_title:
        title = re.sub(r"^\[도매꾹\]\s*", "", og_title.get("content", "")).strip()
        if title:
            result["title"] = title
    if not result.get("title"):
        h1 = soup.select_one("h1#lInfoItemTitle, h1.lInfoRow")
        if h1:
            result["title"] = h1.get_text(strip=True)

    # ── 2. 공급가 (비로그인 시 lItemPrice 숨김 → og:description 필수) ──
    price_el = soup.select_one("div.lItemPrice")
    if price_el:
        digits = re.sub(r"[^\d]", "", price_el.get_text())
        if digits:
            result["supply_price"] = int(digits)
    if not result.get("supply_price"):
        og_desc = soup.find("meta", {"property": "og:description"})
        if og_desc:
            m = re.search(r"([\d,]+)\s*원", og_desc.get("content", ""))
            if m:
                result["supply_price"] = int(m.group(1).replace(",", ""))

    # ── 3. 재고수량 ───────────────────────────────────────────────
    qty_el = soup.select_one("tr.lInfoQty td.lInfoItemContent")
    if not qty_el:
        qty_el = soup.select_one("tr.lInfoQty td")
    if qty_el:
        m = re.search(r"[\d,]+", qty_el.get_text())
        if m:
            result["stock"] = int(m.group(0).replace(",", ""))

    # ── 4. 대표이미지 ─────────────────────────────────────────────
    og_img = soup.find("meta", {"property": "og:image"})
    if og_img:
        result["main_image"] = og_img.get("content", "")

    # ── 5. 추가이미지 (#lThumbImgWrap a.thumbLightbox) ────────────
    sub_imgs: list[str] = []
    for a in soup.select("#lThumbImgWrap a.thumbLightbox"):
        img = a.find("img")
        if img:
            src = _fix_url(img.get("src") or img.get("data-src", ""))
            if src and src not in sub_imgs:
                sub_imgs.append(src)
    result["sub_images"] = sub_imgs[:9]

    # ── 6. 원산지 ─────────────────────────────────────────────────
    origin_el = (soup.select_one("tr.lInfoItemCountry td.lInfoItemCountryContent")
                 or soup.select_one("tr.lInfoItemCountry td"))
    if origin_el:
        result["origin"] = origin_el.get_text(strip=True)

    # ── 7. 최소구매수량 ───────────────────────────────────────────
    # 1순위: tr.lInfoPurchase (로그인 시) / 2순위: og:description "최소 N개"
    min_qty = 1
    minqty_el = soup.select_one("tr.lInfoPurchase td.lInfoItemContent")
    if minqty_el:
        digits = re.sub(r"[^\d]", "", minqty_el.get_text())
        if digits:
            min_qty = int(digits)
    else:
        og_desc = soup.find("meta", {"property": "og:description"})
        if og_desc:
            m = re.search(r"여 ([\d,]+)개", og_desc.get("content", ""))
            if not m:
                # fallback: second number in "2,300원 / 최소 3개"
                nums = re.findall(r"[\d,]+", og_desc.get("content", ""))
                if len(nums) >= 2:
                    try:
                        min_qty = int(nums[1].replace(",", ""))
                    except ValueError:
                        pass
    result["min_qty"] = min_qty

    # ── 8. KC 인증 (동일 구조) ────────────────────────────────────
    # lCertTitle 형식: "[인증유형] 제품분류" — 인증기관명은 safetykorea.kr에서 수집
    cert_el = soup.select_one("div.lCert.lHasImg")
    if cert_el:
        cert_title = cert_el.select_one("div.lCertTitle")
        if cert_title:
            m = re.search(r"\[(.+?)\]", cert_title.get_text(strip=True))
            if m:
                cert_type = m.group(1).strip()
                if cert_type:
                    result["kc_cert_type"] = cert_type
        cert_num = cert_el.select_one("div.lCertNum")
        if cert_num:
            raw_num = cert_num.get_text(strip=True)
            raw_num = re.split(r"자세히", raw_num)[0].strip()
            if raw_num:
                result["kc_cert_no"] = raw_num
            # safetykorea.kr "자세히보기" 링크에서 인증기관명·인증구분 수집
            cert_link = cert_num.select_one("a[href]")
            if cert_link:
                detail_url = cert_link.get("href", "").strip()
                if detail_url and "safetykorea.kr" in detail_url:
                    agency, type_detail = _fetch_kc_cert_detail(detail_url)
                    if agency:
                        result["kc_cert_agency"] = agency
                    if type_detail:
                        result["kc_cert_type"] = type_detail

    # ── 9. 카테고리 (동일 breadcrumb #lPath 구조) ─────────────────
    cat_parts: list[str] = []
    cat_code = ""
    lcat2 = soup.find(id="lPathCat2")
    if lcat2:
        a2 = lcat2.find("a")
        txt = a2.get_text(strip=True) if a2 else lcat2.get_text(strip=True)
        if txt:
            cat_parts.append(txt)
    for n in range(3, 8):
        cat_el = soup.find(id=f"lPathCat{n}")
        if not cat_el:
            break
        first_a = cat_el.find("a")
        if not first_a:
            break
        href = first_a.get("href", "")
        txt  = first_a.get_text(strip=True)
        m    = re.search(r"ca=([\w_]+)", href)
        if m:
            cat_code = m.group(1)
        if txt:
            cat_parts.append(txt)
    result["category_name"] = " > ".join(cat_parts)
    result["category_code"] = cat_code

    # ── 10. 상세설명 이미지 (supportListFrame iframe) ─────────────
    try:
        iframe_url = (
            f"https://domeme.domeggook.com"
            f"/main/item/itemView/supportListFrame.php?no={product_id}"
        )
        det_resp          = _retry_get(session, iframe_url)
        det_resp.encoding = "euc-kr"
        if det_resp.ok:
            det_soup = BeautifulSoup(det_resp.text, "lxml")
            # 문의게시판·스크립트·폼 등 불필요 요소 제거
            for el in det_soup.select("#lSupportList, script, form, style, iframe"):
                el.decompose()
            det_imgs: list[str] = []
            for img in det_soup.find_all("img"):
                src = _fix_url(img.get("src") or img.get("data-src", ""))
                if src and ("cdn" in src or "upload" in src) and src not in det_imgs:
                    det_imgs.append(src)
            result["detail_images"] = det_imgs
            body = det_soup.find("body")
            result["detail_html"]   = str(body) if body else ""
    except Exception as e:
        logger.debug("도매매 상세이미지 수집 실패: %s", e)

    return result


# ── 상품 정보 수집 (메인) ──────────────────────────────────────────

def fetch_product_info(url: str, client) -> dict:
    """도매꾹/도매매 URL에서 상품 정보 수집.

    Args:
        url:    상품 URL 또는 상품번호
        client: DomaemaeClient (세션/sId 관리 포함)

    Returns dict keys:
        supplier, product_id, supplier_url,
        title, supply_price, stock,
        origin, model, kc_cert_no,
        main_image, sub_images, detail_images, detail_html,
        options, category_id, category_name
    """
    supplier, product_id = extract_product_id(url)
    logger.info("상품 수집 시작: supplier=%s, product_id=%s", supplier, product_id)

    # ── API 호출 ────────────────────────────────────────────────
    raw: dict = {}
    if supplier == "domaekkuk":
        try:
            raw = _domaekkuk_api(product_id, client.api_key)
            logger.info("도매꾹 API 성공")
        except Exception as e:
            logger.warning("도매꾹 API 실패, 스크래핑으로 fallback: %s", e)
    else:
        try:
            raw = _domaemae_api(product_id, client)
            logger.info("도매매 API 성공")
        except Exception as e:
            logger.warning("도매매 API 실패, 스크래핑으로 fallback: %s", e)

    # ── API 결과 파싱 ────────────────────────────────────────────
    basis   = raw.get("basis", {})
    price_d = raw.get("price", {})
    qty     = raw.get("qty", {})

    title        = basis.get("title", "")
    supply_price = _parse_price(price_d.get("supply") or price_d.get("dome"))
    stock        = int(qty.get("inventory", 0))
    origin       = basis.get("origin") or basis.get("madeIn") or ""
    model        = basis.get("model")  or basis.get("modelNo") or ""
    brand        = basis.get("brand")  or basis.get("brandName") or ""
    manufacturer = basis.get("manufacturer") or basis.get("maker") or ""
    kc_cert_no     = (basis.get("kc_cert_no") or basis.get("kcCertNo")
                      or basis.get("certNo") or "")
    kc_cert_type   = basis.get("kc_cert_type") or basis.get("kcCertType") or ""
    kc_cert_agency = (basis.get("kc_cert_agency") or basis.get("kcCertAgency")
                      or basis.get("certAgency") or basis.get("certOrgName") or "")
    min_qty      = int(basis.get("minQty") or basis.get("min_qty") or 1)
    cat_name     = _parse_category(raw.get("category", {}))
    options          = _parse_options(raw.get("selectOpt", {}))
    option_code_map  = _parse_option_code_map(raw.get("selectOpt", {}))
    img_src = raw.get("img") or raw.get("images") or {}
    if not img_src and raw.get("thumb"):
        # 신규 API: thumb.original / thumb.large / thumb.small 순으로 대표이미지 설정
        thumb = raw["thumb"]
        img_src = {
            "main": (thumb.get("original") or thumb.get("large")
                     or thumb.get("small") or ""),
        }
    main_image, sub_images, detail_html = _parse_images(img_src)
    detail_images: list[str] = []
    category_code = ""

    # ── 스크래핑으로 부족한 필드 보완 ────────────────────────────
    need_scrape = not title or not supply_price or not main_image
    if need_scrape or not detail_html:
        logger.info("스크래핑 실행 (title=%r, price=%s, img=%r)",
                    bool(title), supply_price, bool(main_image))
        scraped = (_scrape_domaekkuk if supplier == "domaekkuk"
                   else _scrape_domaemae)(product_id)

        if not title:
            title = scraped.get("title", "")
        if not supply_price:
            supply_price = scraped.get("supply_price", 0)
        if not main_image:
            main_image = scraped.get("main_image", "")
        if not sub_images:
            sub_images = scraped.get("sub_images", [])
        if not detail_images:
            detail_images = scraped.get("detail_images", [])
        if not detail_html:
            detail_html = scraped.get("detail_html", "")
        if not kc_cert_no:
            kc_cert_no = scraped.get("kc_cert_no", "")
        if not kc_cert_type:
            kc_cert_type = scraped.get("kc_cert_type", "")
        if not kc_cert_agency:
            kc_cert_agency = scraped.get("kc_cert_agency", "")
        if not origin:
            origin = scraped.get("origin", "")
        if min_qty <= 1:
            min_qty = scraped.get("min_qty", 1)
        if not stock:
            stock = scraped.get("stock", 0)
        if not cat_name:
            cat_name = scraped.get("category_name", "")
        category_code = scraped.get("category_code", "")

    logger.info("수집 완료: title=%r, price=%s, options=%d개",
                title, supply_price, len(options))

    return {
        "supplier":      supplier,
        "product_id":    product_id,
        "supplier_url":  url,
        "title":         title,
        "supply_price":  supply_price,
        "stock":         stock,
        "min_qty":       min_qty,
        "origin":        origin,
        "model":         model,
        "brand":         brand,
        "manufacturer":  manufacturer,
        "kc_cert_no":     kc_cert_no,
        "kc_cert_type":   kc_cert_type,
        "kc_cert_agency": kc_cert_agency,
        "main_image":    main_image,
        "sub_images":    sub_images,
        "detail_images": detail_images,
        "detail_html":   detail_html,
        "options":          options,
        "option_code_map":  option_code_map,
        "category_id":      cat_name,
        "category_name": cat_name,
        "category_code": category_code,
    }


# ── 판매가·태그·카테고리 ──────────────────────────────────────────

def calculate_selling_price(supply_price: int, shipping: int = 3_000,
                            margin: float = 0.3) -> int:
    """판매가 계산 (100원 단위 올림).
    원가 = supply_price + shipping
    판매가 = ceil(원가 × (1 + margin) / 100) * 100
    """
    margin = max(0.0, float(margin))
    return math.ceil((supply_price + shipping) * (1 + margin) / 100) * 100


def generate_tags(product_name: str, max_tags: int = 10) -> list[str]:
    stop = {"및", "의", "이", "가", "을", "를", "은", "는", "에", "와", "과",
            "로", "으로", "도", "에서", "부터", "까지", "한", "하는", "하여",
            "세트", "포함", "배송", "무료"}
    words = re.split(r"[\s\[\](){}「」<>,/\\|·\-_]+", product_name)
    tags, seen = [], set()
    for w in words:
        w = w.strip()
        if len(w) >= 2 and w not in stop and w not in seen:
            tags.append(w)
            seen.add(w)
        if len(tags) >= max_tags:
            break
    return tags


def map_category(supplier_category: str, default_id: str = "") -> str:
    for keyword, cat_id in _CATEGORY_MAP.items():
        if keyword in supplier_category:
            return cat_id
    return default_id or "50000000"


def resolve_category(info: dict, smartstore_api, category_id: str = "") -> tuple[str, str]:
    """공급처 카테고리명으로 네이버 카테고리 ID를 결정한다.

    성공한 매핑은 data/category_mapping_cache.json에 캐싱해 재사용한다.
    매칭 실패 시 빈 문자열 반환 — 잘못된 카테고리 등록으로 인한 판매 금지 방지.

    Returns: (category_id, match_info)
    """
    if category_id:
        return category_id, f"사용자 지정 ({category_id})"

    cat_name = (info.get("category_name") or "").strip()
    if not cat_name:
        logger.warning("공급처 카테고리 정보 없음 — 카테고리 미지정")
        return "", "카테고리 정보 없음"

    # 캐시 확인
    cache = _load_category_cache()
    if cat_name in cache:
        entry = cache[cat_name]
        logger.info("카테고리 캐시 히트: %r → %s (%s)",
                    cat_name[:30], entry["category_id"], entry["category_name"])
        return entry["category_id"], entry["category_name"]

    # 공급처 카테고리를 레벨별로 분리 (세부 → 대분류 순으로 시도)
    parts = [p.strip() for p in re.split(r"[>|/\\]", cat_name) if len(p.strip()) >= 2]
    for part in reversed(parts):
        found_id, found_name = smartstore_api.find_leaf_category(part, whole_cat=cat_name)
        if found_id:
            logger.info("카테고리 자동매칭: %r → %s (%s)", part, found_id, found_name)
            cache[cat_name] = {"category_id": found_id, "category_name": found_name}
            _save_category_cache(cache)
            return found_id, found_name

    logger.warning("카테고리 자동매핑 실패 - 직접 입력 필요 (cat=%r)", cat_name[:40])
    return "", "미매칭 (수동 설정 필요)"


# ── 상품정보제공고시 헬퍼 ──────────────────────────────────────────

_PHONE_FALLBACK = "02-0000-0000"


def _get_notice_type(naver_category: str) -> str:
    """Naver wholeCategoryName으로 상품정보제공고시 타입을 결정한다."""
    cat = naver_category
    if cat.startswith("디지털/가전"):
        return "HOME_APPLIANCES"
    if any(k in cat for k in ["가방", "지갑", "백팩", "클러치", "숄더백", "토트백", "크로스백",
                               "파우치", "크로스백", "힙색", "웨이스트백"]):
        return "BAG"
    if any(k in cat for k in ["가구", "소파", "침대", "책상", "의자", "선반", "서랍장", "수납장",
                               "책장", "행거", "붙박이", "옷장"]):
        return "FURNITURE"
    return "ETC"


def _build_notice(notice_type: str, info: dict, seller_phone: str) -> dict:
    """상품정보제공고시 productInfoProvidedNotice 객체를 반환한다."""
    _SEP = "상세정보 별도표기"
    phone = (seller_phone or "").strip() or _PHONE_FALLBACK
    title = (info.get("title") or _SEP)[:50]
    model = info.get("model") or _SEP
    maker = info.get("manufacturer") or _SEP
    common = {
        "itemName":             title,
        "modelName":            model,
        "manufacturer":         maker,
        "afterServiceDirector": "톡톡문의로 문의부탁드립니다",
    }

    if notice_type == "HOME_APPLIANCES":
        # releaseDate: "YYYY-MM" 형식 필수 (API 실증)
        release_date = datetime.date.today().replace(day=1).strftime("%Y-%m")
        return {
            "productInfoProvidedNoticeType": "HOME_APPLIANCES",
            "homeAppliances": {
                **common,
                "certificationType":          _SEP,
                "size":                       info.get("size") or _SEP,
                "releaseDate":                release_date,
                "warrantyPolicy":             _SEP,
                "additionalCost":             _SEP,
                "qualityAssuranceStandard":   "품질보증기준에 준함",
                "compensationProcedure":      "소비자분쟁해결기준에 준함",
                "defectiveGoodsRefundPolicy": _SEP,
            },
        }
    if notice_type == "BAG":
        return {
            "productInfoProvidedNoticeType": "BAG",
            "bag": {
                **common,
                "material":                   info.get("material") or _SEP,
                "size":                       info.get("size") or _SEP,
                "color":                      info.get("color") or _SEP,
                "type":                       _SEP,
                "caution":                    _SEP,
                "warrantyPolicy":             _SEP,
                "qualityAssuranceStandard":   "품질보증기준에 준함",
                "compensationProcedure":      "소비자분쟁해결기준에 준함",
                "defectiveGoodsRefundPolicy": _SEP,
            },
        }
    if notice_type == "FURNITURE":
        return {
            "productInfoProvidedNoticeType": "FURNITURE",
            "furniture": {
                **common,
                "material":                   info.get("material") or _SEP,
                "color":                      info.get("color") or _SEP,
                "size":                       info.get("size") or _SEP,
                "installedCharge":            _SEP,
                "components":                 _SEP,
                "certificationType":          _SEP,
                "producer":                   _SEP,
                "warrantyPolicy":             _SEP,
                "qualityAssuranceStandard":   "품질보증기준에 준함",
                "compensationProcedure":      "소비자분쟁해결기준에 준함",
                "defectiveGoodsRefundPolicy": _SEP,
            },
        }
    if notice_type == "WEAR":
        return {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                **common,
                "material":                   info.get("material") or _SEP,
                "color":                      info.get("color") or _SEP,
                "size":                       info.get("size") or _SEP,
                "laundryMethod":              _SEP,
                "caution":                    _SEP,
                "warrantyPolicy":             _SEP,
                "qualityAssuranceStandard":   "품질보증기준에 준함",
                "compensationProcedure":      "소비자분쟁해결기준에 준함",
                "defectiveGoodsRefundPolicy": _SEP,
            },
        }
    # ETC (default — 가전/디지털 포함 미매핑 카테고리)
    return {
        "productInfoProvidedNoticeType": "ETC",
        "etc": {
            **common,
            "returnCostReason":         _SEP,
            "noRefundReason":           _SEP,
            "qualityAssuranceStandard": _SEP,
            "compensationProcedure":    _SEP,
            "troubleShootingContents":  _SEP,
        },
    }


_ORIGIN_COUNTRY_MAP = {
    "중국": "중국산", "일본": "일본산", "미국": "미국산", "베트남": "베트남산",
    "인도": "인도산", "인도네시아": "인도네시아산", "태국": "태국산",
    "대만": "대만산", "홍콩": "홍콩산", "캄보디아": "캄보디아산",
    "방글라데시": "방글라데시산", "이탈리아": "이탈리아산", "독일": "독일산",
    "프랑스": "프랑스산", "스페인": "스페인산", "영국": "영국산",
    "포르투갈": "포르투갈산", "터키": "터키산", "미얀마": "미얀마산",
    "필리핀": "필리핀산", "말레이시아": "말레이시아산", "싱가포르": "싱가포르산",
    "호주": "호주산", "뉴질랜드": "뉴질랜드산",
}
_DOMESTIC_KEYWORDS = ["국내", "대한민국", "한국산", "국산"]

# 대륙 → 대표 국가 키워드 (2단계 대륙 텍스트만 있고 3단계 국가가 없을 때 폴백)
_CONTINENT_KEYWORDS = ["아시아", "유럽", "북아메리카", "남아메리카", "아프리카", "오세아니아", "중동"]


def _parse_origin_parts(text: str) -> tuple[str, str, str]:
    """'수입산/아시아/중국' 형태의 원산지 텍스트를 3단계로 분리.

    Returns: (1단계: 국산/수입산, 2단계: 대륙, 3단계: 국가)
    """
    parts = [p.strip() for p in re.split(r"[/\\|]", text)]
    return (
        parts[0] if len(parts) > 0 else "",
        parts[1] if len(parts) > 1 else "",
        parts[2] if len(parts) > 2 else "",
    )


def _build_origin_area(origin_text: str) -> dict:
    """도매꾹/도매매 원산지 텍스트 → Naver originAreaInfo 변환 (3단계 파싱).

    '수입산/아시아/중국' 형태를 파싱해 국가명을 content에 담는다.
    Naver API 코드 "02" (수입산)는 수입국 드롭다운 필드가 비공개라 사용 불가.
    코드 "04" (기타 직접입력)으로 파싱된 국가 정보를 저장한다.
    국내산은 "국내산", 원산지 미확인은 코드 "03" (원산지표시면제).
    """
    text = (origin_text or "").strip()
    if not text:
        return {"originAreaCode": "03", "content": "", "directProductionYn": "N"}

    level1, level2, level3 = _parse_origin_parts(text)

    # ── 국내산 체크 ───────────────────────────────────────────────
    for kw in _DOMESTIC_KEYWORDS:
        if kw in level1 or kw in text:
            return {"originAreaCode": "04", "content": "국내산", "directProductionYn": "N"}

    # ── 국가명 파악 (3단계 → 2단계 → 전체 텍스트 순서로 탐색) ────
    country_label = ""
    for search in [level3, level2, level1, text]:
        for keyword, label in _ORIGIN_COUNTRY_MAP.items():
            if keyword in search:
                country_label = label
                break
        if country_label:
            break

    # ── 수입산 또는 국가 확인된 경우 → 코드 "04" + 파싱 정보 ────
    is_import = "수입" in level1 or any(kw in text for kw in _CONTINENT_KEYWORDS)

    if is_import or country_label:
        # content: 국가명(3단계) 우선, 없으면 원문 텍스트
        content = country_label or level3 or level2 or text[:50]
        return {"originAreaCode": "04", "content": content, "directProductionYn": "N"}

    return {"originAreaCode": "03", "content": "", "directProductionYn": "N"}


# ── 스마트스토어 payload 구성 ──────────────────────────────────────

def build_smartstore_payload(info: dict, selling_price: int,
                             settings: dict, category_id: str = "",
                             product_name: str = "") -> dict:
    seller_phone   = settings.get("seller_phone", "")
    leaf_cat_id    = category_id  # resolve_category()가 미리 결정한 값 (빈 문자열 가능)
    effective_name = (product_name or info["title"]).strip()
    tags           = generate_tags(effective_name)

    # product_name 직접 입력 시 상세설명을 상품명으로 고정
    if product_name:
        detail_content = f"<p>{product_name}</p>"
    # detail_html에서 Naver CDN에 업로드된 이미지 태그만 추출해 깔끔한 HTML 구성
    # (iframe 전체 body를 그대로 쓰면 네비게이션·스크립트 등 불필요한 HTML 포함됨)
    elif info.get("detail_html"):
        _naver_imgs = [
            img.get("src", "")
            for img in BeautifulSoup(info["detail_html"], "lxml").find_all("img")
            if "pstatic.net" in img.get("src", "")
        ]
        if _naver_imgs:
            detail_content = "\n".join(f'<img src="{u}"/>' for u in _naver_imgs)
        elif info.get("detail_images"):
            detail_content = "\n".join(f'<img src="{u}"/>' for u in info["detail_images"])
        else:
            detail_content = f"<p>{effective_name}</p>"
    elif info.get("detail_images"):
        detail_content = "\n".join(f'<img src="{u}"/>' for u in info["detail_images"])
    else:
        detail_content = f"<p>{effective_name}</p>"

    images: dict = {}
    if info.get("main_image"):
        images["representativeImage"] = {"url": info["main_image"]}
    if info.get("sub_images"):
        images["optionalImages"] = [{"url": u} for u in info["sub_images"][:9]]

    # Naver Commerce API v2 originAreaInfo:
    #   "03" = 원산지표시면제 (content 무시, "상세설명에 표시" 고정)
    #   "04" = 기타 직접입력 (content 자유 텍스트 저장)
    #   "02" = 수입산 — 수입국 선택 필드 구조 미공개로 사용 불가
    origin_area_info = _build_origin_area(info.get("origin", ""))

    origin_product: dict = {
        "statusType":    "SALE",
        "saleType":      "NEW",
        "name":          effective_name[:100],
        "detailContent": detail_content,
        "images":        images,
        "salePrice":     selling_price,
        "stockQuantity": 999,
        "deliveryInfo": {
            "deliveryType":          "DELIVERY",
            "deliveryAttributeType": "NORMAL",
            "deliveryCompany":       settings.get("delivery_company", "CJGLS"),
            "deliveryFee": {
                "deliveryFeeType":    "PAID",
                "baseFee":            3000,
                "deliveryFeePayType": "PREPAID",
                "deliveryFeeByArea": {
                    "deliveryAreaType": "AREA_3",
                    "area2extraFee":    5000,
                    "area3extraFee":    7000,
                },
            },
            "claimDeliveryInfo": {
                "returnDeliveryFee":   3000,
                "exchangeDeliveryFee": 6000,
            },
        },
        "detailAttribute": {
            "afterServiceInfo": {
                "afterServiceTelephoneNumber": (seller_phone or "").strip() or _PHONE_FALLBACK,
                "afterServiceGuideContent":    "A/S 미제공, 상품 불량 시 교환/환불로 처리. 문의는 네이버 톡톡으로 연락주세요.",
            },
            "originAreaInfo": origin_area_info,
            "taxType":         "TAX",
            "minorPurchasable": True,
            "productInfoProvidedNotice": _build_notice(
                _get_notice_type(info.get("naver_category_name") or info.get("category_name") or ""),
                info,
                seller_phone,
            ),
        },
    }

    # leafCategoryId: 빈 문자열이면 네이버 API 오류 방지를 위해 키 자체를 제외
    if leaf_cat_id:
        origin_product["leafCategoryId"] = leaf_cat_id

    payload: dict = {
        "originProduct": origin_product,
        "smartstoreChannelProduct": {
            "naverShoppingRegistration":       True,
            "channelProductDisplayStatusType": "ON",
        },
    }

    naver_search_info: dict = {}
    if info.get("model"):
        naver_search_info["modelName"] = info["model"]
    if info.get("brand"):
        naver_search_info["brandName"] = info["brand"]
    if info.get("manufacturer"):
        naver_search_info["manufacturerName"] = info["manufacturer"]
    if naver_search_info:
        payload["originProduct"]["detailAttribute"]["naverShoppingSearchInfo"] = naver_search_info

    # KC인증: certificationInfoId + certificationKindType + 인증번호
    # certificationInfoId는 register_product에서 get_kc_cert_status로 조회해 info에 저장
    # certificationKindType (kindType 아님!) + certificationTargetExcludeContent 필수
    kc_no      = (info.get("kc_cert_no") or "").strip()
    kc_agency  = (info.get("kc_cert_agency") or "").strip()
    kc_info_id = int(info.get("kc_cert_info_id") or 0)
    if kc_no and kc_info_id:
        cert_entry: dict = {
            "certificationInfoId":   kc_info_id,
            "certificationKindType": "KC_CERTIFICATION",
            "certificationNumber":   kc_no,
        }
        if kc_agency:
            cert_entry["name"] = kc_agency
        origin_product["detailAttribute"]["productCertificationInfos"] = [cert_entry]
        # KC인증 상품임을 명시 (kcCertifiedProductExclusionYn="N" → KC인증 제외 아님)
        # Boolean이 아닌 Enum 문자열: "Y"=제외, "N"=제외 아님(KC인증 적용)
        origin_product["detailAttribute"]["certificationTargetExcludeContent"] = {
            "kcCertifiedProductExclusionYn": "N"
        }
        logger.info("KC인증 payload 포함: certInfoId=%s, certificationKindType=KC_CERTIFICATION, no=%r, agency=%r",
                    kc_info_id, kc_no, kc_agency)
    else:
        logger.info("KC인증 payload 제외 (no=%r, certInfoId=%s)", kc_no, kc_info_id)

    if info.get("options"):
        groups = info["options"][:3]
        combos = list(itertools.product(*[g["values"] for g in groups]))
        # 신규 등록 시 id 없이 전송 (Naver가 자동 할당)
        opt_combos = [
            {f"optionName{i+1}": v for i, v in enumerate(combo)} |
            {"stockQuantity": 999, "price": 0, "usable": True}
            for combo in combos
        ]
        # optionInfo는 detailAttribute 안에 위치해야 함 (GET 응답 구조와 동일)
        payload["originProduct"]["detailAttribute"]["optionInfo"] = {
            "optionCombinationSortType": "CREATE",
            "optionCombinationGroupNames": {
                f"optionGroupName{i+1}": g["name"] for i, g in enumerate(groups)
            },
            "optionCombinations": opt_combos,
            "useStockManagement": True,
        }
        # 옵션 사용 시 상품 레벨 재고는 조합 수량 합산값으로 설정
        payload["originProduct"]["stockQuantity"] = len(opt_combos) * 999
        logger.info(
            "옵션 payload 구성: groups=%s, combinations=%d개",
            [g["name"] for g in groups], len(opt_combos),
        )

    if tags:
        payload["originProduct"]["tag"] = [{"text": t} for t in tags]

    return payload


# ── 옵션 매핑 저장 헬퍼 ───────────────────────────────────────────

def _save_option_mappings(
    confirmed: dict,
    ss_prod_id: str,
    origin_prod_id: str,
    info: dict,
    url: str,
    price_margin_rate: float,
    mapping_repo,
) -> int:
    """등록된 상품의 옵션별 매핑 저장.

    GET /v2/products/origin-products 응답의
    originProduct.detailAttribute.optionInfo.optionCombinations[].id 를
    ss_option_id 로 사용한다.
    (channelProductOptions 는 API 응답에서 항상 비어 있으므로 사용하지 않음)

    반환값: 저장된 옵션 매핑 수 (0 = optionCombinations 없음)
    """
    opt_info = (
        confirmed.get("originProduct", {})
                 .get("detailAttribute", {})
                 .get("optionInfo", {})
    )
    combinations = opt_info.get("optionCombinations", [])
    if not combinations:
        return 0

    option_code_map = info.get("option_code_map", {})
    title_short     = info.get("title", "")[:30]
    saved = 0
    for combo in combinations:
        if not combo.get("usable", True):
            continue
        ch_opt_id = str(combo.get("id") or "")
        if not ch_opt_id:
            continue

        name_parts = [
            str(combo.get(f"optionName{i}", "")).strip()
            for i in range(1, 5)
            if combo.get(f"optionName{i}", "").strip()
        ]
        option_label = "/".join(name_parts) or ch_opt_id

        supplier_opt_id = _match_supplier_option(option_code_map, name_parts)
        if supplier_opt_id:
            logger.info("옵션 매핑: SS 옵션=%r → 도매처 코드=%s", option_label, supplier_opt_id)
        else:
            logger.warning("옵션 매핑 실패 — SS 옵션=%r 도매처 코드 없음 (product=%s)",
                           option_label, ss_prod_id)

        try:
            mapping_repo.add(
                ss_product_id      = ss_prod_id,
                origin_product_no  = origin_prod_id,
                supplier           = info["supplier"],
                supplier_url_or_id = url,
                ss_option_id       = ch_opt_id,
                supplier_option_id = supplier_opt_id,
                price_margin_rate  = price_margin_rate,
                memo               = f"자동등록 {title_short} [{option_label}]",
            )
            saved += 1
        except Exception as e:
            logger.warning("옵션 매핑 저장 실패 (combo_id=%s): %s", ch_opt_id, e)

    logger.info("옵션 매핑 저장 완료: ss_product_id=%s — %d건 (코드 매칭 성공 %d건)",
                ss_prod_id, saved,
                sum(1 for c in combinations
                    if _match_supplier_option(option_code_map,
                        [str(c.get(f"optionName{i}", "")).strip()
                         for i in range(1, 5)
                         if str(c.get(f"optionName{i}", "")).strip()])))
    return saved


# ── 메인 등록 함수 ─────────────────────────────────────────────────

def register_product(url: str, selling_price: int, smartstore_api,
                     supplier_client, settings: dict, mapping_repo,
                     category_id: str = "", product_name: str = "") -> dict:
    """수집 → 스마트스토어 등록 → 매핑 저장."""
    try:
        info = fetch_product_info(url, supplier_client)
    except Exception as e:
        return {"success": False, "error": f"상품 정보 수집 실패: {e}"}

    if not info.get("title"):
        return {"success": False, "error": "상품명을 가져오지 못했습니다.", "info": info}
    if not info.get("supply_price"):
        return {"success": False, "error": "공급가를 가져오지 못했습니다.", "info": info}

    if not selling_price:
        selling_price = (info["supply_price"] or 0) + 3000

    category_id, category_match = resolve_category(info, smartstore_api, category_id)
    # notice type 결정을 위해 Naver 카테고리명 저장
    info["naver_category_name"] = category_match

    # KC인증 필수 카테고리 사전 확인 + certificationInfoId 조회
    if category_id:
        try:
            kc_cert_type_hint = (info.get("kc_cert_type") or "").strip()
            kc_required, kc_cert_info_id = smartstore_api.get_kc_cert_status(
                category_id, kc_cert_type_hint
            )
            if kc_required:
                kc_no     = (info.get("kc_cert_no") or "").strip()
                kc_agency = (info.get("kc_cert_agency") or "").strip()
                if not (kc_no and kc_agency and kc_cert_info_id):
                    logger.warning(
                        "KC인증 정보 누락 - 등록 불가 "
                        "(category=%s, no=%r, agency=%r, certInfoId=%s)",
                        category_id, kc_no, kc_agency, kc_cert_info_id,
                    )
                    return {
                        "success": False,
                        "error": "KC인증 정보 누락 - 등록 불가",
                        "detail": (
                            f"카테고리 [{category_match}]({category_id})는 KC인증 필수 카테고리입니다. "
                            f"인증번호·인증기관명·인증정보ID 확인이 필요합니다."
                        ),
                        "info": info,
                        "category_id": category_id,
                        "category_match": category_match,
                    }
                # payload 구성에 사용할 certificationInfoId 저장
                info["kc_cert_info_id"] = kc_cert_info_id
        except Exception as e:
            logger.warning("KC인증 필수 여부 확인 실패 (계속 진행): %s", e)

    # ── 모든 이미지를 Naver CDN에 업로드 (외부 URL 직접 사용 불가) ──
    supplier   = info.get("supplier", "domaekkuk")
    product_id = info.get("product_id", "")
    # 상품 페이지를 방문해 CDN 쿠키를 취득한 세션을 모든 이미지 다운로드에 재사용
    img_session = _make_image_session(supplier, product_id, supplier_client)

    def _upload(url: str) -> str:
        """공급처 인증 세션으로 이미지를 다운로드한 뒤 Naver CDN에 업로드."""
        data, ct, fname = _fetch_image_bytes(url, img_session)
        return smartstore_api.upload_image_data(data, ct, fname)

    # 대표 이미지 (필수 — 실패 시 등록 중단)
    if info.get("main_image"):
        try:
            naver_url = _upload(info["main_image"])
            logger.info("대표 이미지 업로드 완료: %s -> %s", info["main_image"][:60], naver_url)
            info["main_image"] = naver_url
        except Exception as e:
            logger.error("대표 이미지 업로드 실패: %s", e)
            return {"success": False, "error": f"대표 이미지 업로드 실패: {e}", "info": info}
    else:
        return {"success": False, "error": "대표 이미지가 없습니다. 상품을 등록할 수 없습니다.", "info": info}

    # 서브 이미지
    if info.get("sub_images"):
        uploaded = []
        for sub_url in info["sub_images"][:9]:
            try:
                uploaded.append(_upload(sub_url))
            except Exception as e:
                logger.warning("서브 이미지 업로드 실패 (%s): %s", sub_url, e)
        info["sub_images"] = uploaded

    # detail_images → Naver CDN URL로 교체 (UI 아이콘 제외)
    if info.get("detail_images"):
        uploaded_detail = []
        for d_url in info["detail_images"]:
            if _is_ui_image(d_url):
                logger.debug("UI 이미지 스킵 (detail_images): %s", d_url[:80])
                continue
            try:
                uploaded_detail.append(_upload(d_url))
            except Exception as e:
                logger.warning("상세 이미지 업로드 실패 (%s): %s", d_url, e)
                uploaded_detail.append(d_url)  # 실패 시 원본 유지
        info["detail_images"] = uploaded_detail

    # detail_html 내 <img src> URL → Naver CDN URL로 교체
    if info.get("detail_html"):
        def _replace_img(m):
            src = m.group(1)
            if src.startswith("http") and "pstatic.net" not in src:
                if _is_ui_image(src):
                    logger.debug("UI 이미지 스킵: %s", src[:80])
                    return ""  # 아이콘/배경 태그 제거
                try:
                    return m.group(0).replace(src, _upload(src))
                except Exception as e:
                    logger.warning("상세 HTML 이미지 업로드 실패 (%s): %s", src[:60], e)
            return m.group(0)
        info["detail_html"] = re.sub(
            r'<img[^>]+src=["\']([^"\']+)["\']',
            _replace_img,
            info["detail_html"],
        )

    payload = build_smartstore_payload(info, selling_price, settings, category_id,
                                       product_name=product_name)

    def _post_product(pl: dict):
        return requests.post(
            f"{smartstore_api.BASE_URL}/v2/products",
            headers=smartstore_api._headers(),
            json=pl,
            timeout=30,
        )

    _KC_KEYWORDS = ("certificationInfos", "certificationKind", "KC 인증", "kindType")

    def _is_kc_cert_error(body) -> bool:
        """Naver API가 KC인증 필수 오류를 반환했는지 확인.
        응답 구조에 무관하게 전체 JSON 문자열에서 KC 관련 키워드를 탐색한다.
        """
        import json as _json
        try:
            body_str = _json.dumps(body, ensure_ascii=False)
        except Exception:
            body_str = str(body)
        return any(kw in body_str for kw in _KC_KEYWORDS)

    try:
        import json as _json_log
        opt_info_log = payload.get("originProduct", {}).get("optionInfo")
        logger.info(
            "스마트스토어 등록 요청 — optionInfo 포함: %s, combinations: %d개",
            opt_info_log is not None,
            len((opt_info_log or {}).get("optionCombinations", [])),
        )
        logger.debug("전송 payload (optionInfo): %s",
                     _json_log.dumps(opt_info_log, ensure_ascii=False) if opt_info_log else "없음")
        resp = _post_product(payload)
        logger.info("스마트스토어 API 응답: HTTP %s", resp.status_code)
        if not resp.ok:
            try:
                err_body = resp.json()
            except Exception:
                err_body = {"raw": resp.text}

            logger.warning("스마트스토어 등록 오류 원문: %s", err_body)

            # KC인증 필수 오류: 해당 카테고리는 법적으로 KC인증 정보 필수 → 수동 처리 필요
            if _is_kc_cert_error(err_body) and category_id:
                logger.warning(
                    "KC인증 필수 카테고리 (%s, %s) — 이 카테고리는 KC인증 정보 필수 (법적 요구사항). "
                    "KC인증 정보를 직접 입력하거나 카테고리를 변경해 수동 등록하세요.",
                    category_id, category_match,
                )
                return {
                    "success": False,
                    "error": "KC인증 필수",
                    "detail": (
                        f"카테고리 [{category_match}]({category_id})는 KC인증 필수 카테고리입니다. "
                        f"KC인증 정보를 직접 입력하거나, KC인증 불필요 카테고리로 변경해 수동 등록하세요."
                    ),
                    "info": info,
                    "selling_price": selling_price,
                    "category_id": category_id,
                    "category_match": category_match,
                }

            if not resp.ok:
                logger.error(
                    "스마트스토어 상품 등록 실패 [%s]: %s",
                    resp.status_code, err_body,
                )
                return {
                    "success": False,
                    "error": f"HTTP {resp.status_code}",
                    "detail": err_body,
                    "info": info,
                    "selling_price": selling_price,
                }
        result     = resp.json()
        logger.info("스마트스토어 API 성공 응답: %s", _json_log.dumps(result, ensure_ascii=False)[:500])
        # originProductNo: API 호출용 (/v2/products/{id})
        # smartstoreChannelProductNo: 주문 응답의 productId, 스마트스토어 화면 표시 ID
        origin_prod_id  = str(result.get("originProductNo") or result.get("id") or result.get("productNo") or "")
        channel_prod_id = str(result.get("smartstoreChannelProductNo") or "")

        if not origin_prod_id:
            logger.error("등록 응답에서 상품 ID를 추출하지 못했습니다: %s", result)
            return {"success": False, "error": "등록 응답에 상품 ID 없음", "detail": result,
                    "info": info, "selling_price": selling_price}

        # 등록 직후 GET으로 두 ID + 옵션 목록 재확인 (originProductNo 기준으로 조회)
        confirmed = {}
        try:
            confirmed = smartstore_api.get_product(origin_prod_id)
            confirmed_origin = str(
                confirmed.get("originProduct", {}).get("originProductNo") or origin_prod_id
            )
            confirmed_channel = str(
                confirmed.get("smartstoreChannelProduct", {}).get("channelProductNo") or channel_prod_id
            )
            if confirmed_origin != origin_prod_id or confirmed_channel != channel_prod_id:
                logger.info("상품 ID 재확인: origin %s→%s, channel %s→%s",
                            origin_prod_id, confirmed_origin, channel_prod_id, confirmed_channel)
            origin_prod_id  = confirmed_origin
            channel_prod_id = confirmed_channel or channel_prod_id
        except Exception as e:
            logger.warning("상품 ID GET 재확인 실패, POST 응답 ID 사용: %s", e)

        # ss_product_id = channelProductNo (주문 매핑 기준 ID)
        ss_prod_id = channel_prod_id or origin_prod_id
    except Exception as e:
        logger.error("스마트스토어 상품 등록 실패: %s", e)
        return {"success": False, "error": str(e), "info": info, "selling_price": selling_price}

    try:
        cost              = (info["supply_price"] or 0) + 3000
        price_margin_rate = round(selling_price / cost, 6) if cost else 1.0
        # 옵션별 콤보 매핑 저장 (optionCombinations[].id = 주문 optionCode와 동일)
        _save_option_mappings(
            confirmed, ss_prod_id, origin_prod_id,
            info, url, price_margin_rate, mapping_repo,
        )
        # ss_option_id='' 폴백 매핑 항상 저장
        # — 콤보 매핑 없는 상품 및 예외 option_code 대비
        mapping_repo.add(
            ss_product_id      = ss_prod_id,
            origin_product_no  = origin_prod_id,
            supplier           = info["supplier"],
            supplier_url_or_id = url,
            price_margin_rate  = price_margin_rate,
            memo               = f"자동등록 {info['title'][:40]}",
        )
    except Exception as e:
        logger.warning("매핑 저장 실패 (등록은 성공): %s", e)

    return {
        "success":        True,
        "product_id":     ss_prod_id,
        "selling_price":  selling_price,
        "category_id":    category_id,
        "category_match": category_match,
        "info":           info,
    }


# ── 옵션 매핑 backfill ──────────────────────────────────────────────

def backfill_option_mappings(
    ss_product_id: str,
    supplier_product_id: str,
    smartstore_api,
    supplier_client,
    mapping_repo,
    supplier: str = "domaemae",
    price_margin_rate: float = 1.3,
) -> dict:
    """이미 등록된 상품의 옵션 매핑에 supplier_option_id 재매핑.

    Args:
        ss_product_id:       channelProductNo (예: "13625392732")
        supplier_product_id: 도매매 상품번호   (예: "59295051")

    Returns:
        {"updated": int, "failed": [option_label,...], "total": int}
    """
    # 1. origin_product_no 조회 (기존 매핑 우선, 없으면 ss_product_id 직접 시도)
    origin_prod_id = ""
    for m in mapping_repo.all():
        if m.get("ss_product_id") == ss_product_id and m.get("origin_product_no"):
            origin_prod_id = m["origin_product_no"]
            break
    if not origin_prod_id:
        origin_prod_id = ss_product_id
        logger.warning("backfill: 기존 매핑에서 origin_product_no 없음 — ss_product_id(%s)로 시도",
                       ss_product_id)

    # 2. SS 옵션 조합 목록 조회
    try:
        confirmed = smartstore_api.get_product(origin_prod_id)
    except Exception as e:
        raise RuntimeError(f"SS 상품 조회 실패 (origin={origin_prod_id}): {e}") from e

    opt_info     = (confirmed.get("originProduct", {})
                             .get("detailAttribute", {})
                             .get("optionInfo", {}))
    combinations = opt_info.get("optionCombinations", [])
    if not combinations:
        logger.info("backfill: optionCombinations 없음 — 단일 상품")
        return {"updated": 0, "failed": [], "total": 0}

    # 3. 도매매 옵션코드 맵 (get_options → selectOpt.data 형식)
    try:
        dm_opts = supplier_client.get_options(supplier_product_id)
    except Exception as e:
        raise RuntimeError(f"도매매 옵션 조회 실패 (product={supplier_product_id}): {e}") from e

    option_code_map: dict[str, str] = {
        _normalize_opt(o["name"]): o["id"] for o in dm_opts if o.get("name")
    }
    logger.info("backfill: 도매매 옵션 %d개 로드 (상품=%s)", len(option_code_map), supplier_product_id)

    # 4. 조합별 매핑 업데이트 or 신규 추가
    updated, failed = 0, []
    for combo in combinations:
        if not combo.get("usable", True):
            continue
        ch_opt_id = str(combo.get("id") or "")
        if not ch_opt_id:
            continue

        name_parts = [
            str(combo.get(f"optionName{i}", "")).strip()
            for i in range(1, 5)
            if combo.get(f"optionName{i}", "").strip()
        ]
        option_label    = "/".join(name_parts) or ch_opt_id
        supplier_opt_id = _match_supplier_option(option_code_map, name_parts)

        if supplier_opt_id:
            logger.info("backfill 매칭: SS 옵션=%r → 도매처 코드=%s", option_label, supplier_opt_id)
        else:
            logger.warning("backfill 매칭 실패: SS 옵션=%r 도매처 코드 없음", option_label)
            failed.append(option_label)

        existing = mapping_repo.find(ss_product_id, ch_opt_id)
        if existing:
            mapping_repo.set_supplier_option_id(existing["id"], supplier_opt_id)
            updated += 1
        else:
            try:
                mapping_repo.add(
                    ss_product_id      = ss_product_id,
                    origin_product_no  = origin_prod_id,
                    supplier           = supplier,
                    supplier_url_or_id = supplier_product_id,
                    ss_option_id       = ch_opt_id,
                    supplier_option_id = supplier_opt_id,
                    price_margin_rate  = price_margin_rate,
                    memo               = f"backfill [{option_label}]",
                )
                updated += 1
            except Exception as e:
                logger.warning("backfill 매핑 추가 실패: %s", e)
                if option_label not in failed:
                    failed.append(option_label)

    logger.info("backfill 완료: ss_product_id=%s — 처리 %d건 / 매칭 실패 %d건",
                ss_product_id, updated, len(failed))
    return {"updated": updated, "failed": failed, "total": len(combinations)}
