"""도매매/도매꾹 상품 → 스마트스토어 자동 등록"""
import itertools
import json
import math
import re
import logging
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

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


def _parse_category(cat_d) -> str:
    if isinstance(cat_d, dict):
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
    cert_el = soup.select_one("div.lCert.lHasImg")
    if cert_el:
        cert_title = cert_el.select_one("div.lCertTitle")
        if cert_title:
            m = re.search(r"\[(.+?)\]", cert_title.get_text())
            if m:
                result["kc_cert_type"] = m.group(1).strip()
        cert_num = cert_el.select_one("div.lCertNum")
        if cert_num:
            raw_num = cert_num.get_text(strip=True)
            raw_num = re.split(r"자세히", raw_num)[0].strip()
            m = re.search(r"[A-Z0-9]{2,3}-\d{3,6}", raw_num)
            result["kc_cert_no"] = m.group(0) if m else raw_num

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
    cert_el = soup.select_one("div.lCert.lHasImg")
    if cert_el:
        cert_title = cert_el.select_one("div.lCertTitle")
        if cert_title:
            m = re.search(r"\[(.+?)\]", cert_title.get_text())
            if m:
                result["kc_cert_type"] = m.group(1).strip()
        cert_num = cert_el.select_one("div.lCertNum")
        if cert_num:
            raw_num = cert_num.get_text(strip=True)
            raw_num = re.split(r"자세히", raw_num)[0].strip()
            m = re.search(r"[A-Z0-9]{2,3}-\d{3,6}", raw_num)
            result["kc_cert_no"] = m.group(0) if m else raw_num

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
    kc_cert_no   = (basis.get("kc_cert_no") or basis.get("kcCertNo")
                    or basis.get("certNo") or "")
    kc_cert_type = basis.get("kc_cert_type") or basis.get("kcCertType") or ""
    min_qty      = int(basis.get("minQty") or basis.get("min_qty") or 1)
    cat_name     = _parse_category(raw.get("category", {}))
    options      = _parse_options(raw.get("selectOpt", {}))
    main_image, sub_images, detail_html = _parse_images(
        raw.get("img", raw.get("images", {}))
    )
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
        "kc_cert_no":    kc_cert_no,
        "kc_cert_type":  kc_cert_type,
        "main_image":    main_image,
        "sub_images":    sub_images,
        "detail_images": detail_images,
        "detail_html":   detail_html,
        "options":       options,
        "category_id":   cat_name,
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
    """카테고리 ID를 단계적으로 자동 결정한다.

    1단계: 공급처 카테고리명 각 레벨 (세부 → 대분류)
    2단계: 상품명 키워드 (2글자 이상 단어, 최대 5개)
    3단계: 빈 문자열 반환 (수동 입력 필요)

    Returns: (category_id, match_info)
    """
    if category_id:
        return category_id, f"사용자 지정 ({category_id})"

    cat_name = (info.get("category_name") or "").strip()
    title    = (info.get("title") or "").strip()

    # 1단계: 공급처 카테고리명 → 레벨별 분리 후 세부부터 검색
    if cat_name:
        parts = [p.strip() for p in re.split(r"[>|/\\]", cat_name) if p.strip()]
        for part in reversed(parts):
            found = smartstore_api.find_leaf_category(part)
            if found:
                logger.info("카테고리 자동매칭 성공 (카테고리명 '%s'): %s", part, found)
                return found, f"카테고리명 '{part}'"

    # 2단계: 상품명 키워드 (2글자 이상 단어, 최대 5개 시도)
    words = [w for w in re.split(r"[\s/\[\]()\-_,·]+", title) if len(w) >= 2]
    for word in words[:5]:
        found = smartstore_api.find_leaf_category(word)
        if found:
            logger.info("카테고리 자동매칭 성공 (상품명 키워드 '%s'): %s", word, found)
            return found, f"상품명 키워드 '{word}'"

    # 3단계: 미매칭
    logger.warning("카테고리 자동매칭 실패 — cat=%r  title=%r", cat_name[:30], title[:30])
    return "", "미매칭 (수동 설정 필요)"


# ── 스마트스토어 payload 구성 ──────────────────────────────────────

def build_smartstore_payload(info: dict, selling_price: int,
                             settings: dict, category_id: str = "") -> dict:
    seller_phone = settings.get("seller_phone", "")
    leaf_cat_id  = category_id  # resolve_category()가 미리 결정한 값 (빈 문자열 가능)
    tags         = generate_tags(info["title"])

    # detail_html에서 Naver CDN에 업로드된 이미지 태그만 추출해 깔끔한 HTML 구성
    # (iframe 전체 body를 그대로 쓰면 네비게이션·스크립트 등 불필요한 HTML 포함됨)
    if info.get("detail_html"):
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
            detail_content = f"<p>{info.get('title', '')}</p>"
    elif info.get("detail_images"):
        detail_content = "\n".join(f'<img src="{u}"/>' for u in info["detail_images"])
    else:
        detail_content = f"<p>{info.get('title', '')}</p>"

    images: dict = {}
    if info.get("main_image"):
        images["representativeImage"] = {"url": info["main_image"]}
    if info.get("sub_images"):
        images["optionalImages"] = [{"url": u} for u in info["sub_images"][:9]]

    # Naver Commerce API v2 originAreaCode 스펙:
    #   "01" = 수산물/농임산물 전용 (일반 공산품에 사용 불가)
    #   "02" = 수입산 — importer(수입사) + 수입국 필드 추가 필수 (미수집)
    #   "03" = 원산지표시면제 — 일반 공산품(전자, 잡화 등)에 유효
    # 도매꾹/도매매 상품(주로 제조품)은 원산지 정보 불완전 → "03" 사용
    origin_area_code = "03"
    origin_content   = ""

    origin_product: dict = {
        "statusType":    "SALE",
        "saleType":      "NEW",
        "name":          info["title"][:100],
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
            },
            "claimDeliveryInfo": {
                "returnDeliveryFee":   3000,
                "exchangeDeliveryFee": 6000,
            },
        },
        "detailAttribute": {
            "afterServiceInfo": {
                "afterServiceTelephoneNumber": seller_phone or "00-0000-0000",
                "afterServiceGuideContent":    "구매 후 문의사항은 판매자에게 연락해주세요.",
            },
            "originAreaInfo": {
                "originAreaCode":    origin_area_code,
                "content":           origin_content,
                "directProductionYn": "N",
            },
            "taxType":         "TAX",
            "minorPurchasable": True,
            "productInfoProvidedNotice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {
                    "itemName":                 info.get("title", "상품 설명 참조")[:50],
                    "modelName":                info.get("model") or "상품 설명 참조",
                    "manufacturer":             info.get("manufacturer") or "상품 설명 참조",
                    "afterServiceDirector":     seller_phone or "상품 설명 참조",
                    "returnCostReason":         "상품 설명 참조",
                    "noRefundReason":           "상품 설명 참조",
                    "qualityAssuranceStandard": "상품 설명 참조",
                    "compensationProcedure":    "상품 설명 참조",
                    "troubleShootingContents":  "상품 설명 참조",
                },
            },
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

    if info.get("kc_cert_no"):
        logger.info("KC인증번호 있으나 인증기관명 미수집 — 인증 정보 제외 (no=%r)", info["kc_cert_no"])

    if info.get("options"):
        groups = info["options"][:3]
        grp_names = {f"optionGroupName{i+1}": g["name"] for i, g in enumerate(groups)}
        combos    = list(itertools.product(*[g["values"] for g in groups]))
        opt_combos = [
            {"id": idx + 1} |
            {f"optionName{i+1}": v for i, v in enumerate(combo)} |
            {"stockQuantity": 999, "price": 0, "usable": True}
            for idx, combo in enumerate(combos)
        ]
        payload["originProduct"]["optionInfo"] = {
            "optionCombinationGroupNames": grp_names,
            "optionCombinations":          opt_combos,
            "useStockManagement":          True,
        }

    if tags:
        payload["originProduct"]["tag"] = [{"text": t} for t in tags]

    return payload


# ── 메인 등록 함수 ─────────────────────────────────────────────────

def register_product(url: str, selling_price: int, smartstore_api,
                     supplier_client, settings: dict, mapping_repo,
                     category_id: str = "") -> dict:
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

    payload = build_smartstore_payload(info, selling_price, settings, category_id)

    try:
        resp = requests.post(
            f"{smartstore_api.BASE_URL}/v2/products",
            headers=smartstore_api._headers(),
            json=payload,
            timeout=30,
        )
        if not resp.ok:
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text
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
        ss_prod_id = str(
            result.get("originProductNo") or
            result.get("id") or
            result.get("productNo") or ""
        )
    except Exception as e:
        logger.error("스마트스토어 상품 등록 실패: %s", e)
        return {"success": False, "error": str(e), "info": info, "selling_price": selling_price}

    try:
        cost              = (info["supply_price"] or 0) + 3000
        price_margin_rate = round(selling_price / cost, 6) if cost else 1.0
        mapping_repo.add(
            ss_product_id      = ss_prod_id,
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
