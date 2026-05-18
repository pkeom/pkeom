"""도매매/도매꾹 상품 → 스마트스토어 자동 등록"""
import math
import re
import logging
import threading
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# 도매꾹/도매매 카테고리명 키워드 → 스마트스토어 leafCategoryId 간이 매핑
# 실제 카테고리 ID는 네이버 커머스 API /v1/product-attributes/categories 로 조회 가능
_CATEGORY_MAP = {
    "패션의류": "50000000",
    "패션잡화": "50000001",
    "화장품": "50000002",
    "미용": "50000002",
    "디지털": "50000003",
    "가전": "50000003",
    "가구": "50000004",
    "인테리어": "50000004",
    "출산": "50000005",
    "육아": "50000005",
    "식품": "50000006",
    "스포츠": "50000007",
    "레저": "50000007",
    "생활": "50000008",
    "건강": "50000008",
    "완구": "50000011",
    "취미": "50000011",
    "문구": "50000012",
    "오피스": "50000012",
    "반려동물": "50000013",
    "자동차": "50000014",
}


def extract_product_id(url: str) -> tuple[str, str]:
    """URL 또는 숫자 ID에서 (supplier, product_id) 추출.

    Returns:
        ("domaekkuk" | "domaemae", "상품번호")
    Raises:
        ValueError: 파싱 불가 형식
    """
    url = url.strip()
    # 도매꾹: domeggook.com/{no}
    m = re.search(r"domeggook\.com/(\d+)", url)
    if m:
        return "domaekkuk", m.group(1)
    # 도매매: domaemae.co.kr/...?no={no}
    m = re.search(r"domaemae\.co\.kr.*?[?&]no=(\d+)", url)
    if m:
        return "domaemae", m.group(1)
    m = re.search(r"domaemae\.co\.kr/(\d+)", url)
    if m:
        return "domaemae", m.group(1)
    # 숫자만
    if re.fullmatch(r"\d+", url):
        return "domaekkuk", url
    raise ValueError(f"지원하지 않는 URL: {url}")


def calculate_selling_price(supply_price: int, shipping: int = 3_000, margin: float = 0.3) -> int:
    """판매가 계산 (100원 단위 올림).

    원가 = supply_price + shipping
    판매가 = ceil(원가 / (1 - margin) / 100) * 100
    """
    cost = supply_price + shipping
    return math.ceil(cost / (1 - margin) / 100) * 100


def generate_tags(product_name: str, max_tags: int = 10) -> list[str]:
    """상품명 키워드로 태그 자동 생성."""
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
    """공급사 카테고리명 → 스마트스토어 leafCategoryId."""
    for keyword, cat_id in _CATEGORY_MAP.items():
        if keyword in supplier_category:
            return cat_id
    return default_id or "50000000"


# ── 웹 스크래핑 ────────────────────────────────────────────────────

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _fix_url(src: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    return src


def _scrape_domaekkuk(product_id: str) -> dict:
    """도매꾹 상품 페이지에서 이미지·KC인증 스크래핑."""
    url = f"https://www.domeggook.com/{product_id}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("도매꾹 스크래핑 실패(%s): %s", product_id, e)
        return {}

    soup = BeautifulSoup(resp.text, "lxml")
    result: dict = {}

    # 대표이미지
    for sel in ["img#productImage", ".product-image img", ".main-image img", ".big-image img"]:
        el = soup.select_one(sel)
        if el and el.get("src"):
            result["main_image"] = _fix_url(el["src"])
            break

    # 추가이미지
    subs = []
    for sel in [".thumbnail-list img", ".sub-images img", ".small-img img"]:
        imgs = soup.select(sel)
        if imgs:
            subs = [_fix_url(i["src"]) for i in imgs if i.get("src")][:9]
            break
    result["sub_images"] = subs

    # 상세설명
    for sel in [".product-detail", "#productDetail", ".detail-content", ".item-detail"]:
        el = soup.select_one(sel)
        if el:
            result["detail_html"] = str(el)
            result["detail_images"] = [_fix_url(i["src"]) for i in el.select("img") if i.get("src")]
            break

    # KC 인증번호
    m = re.search(r"[A-Z]{2,3}-\d{3,4}-\d{4,6}", resp.text)
    if m:
        result["kc_cert_no"] = m.group(0)

    return result


def _scrape_domaemae(product_id: str) -> dict:
    """도매매 상품 페이지에서 이미지·KC인증 스크래핑."""
    url = f"https://www.domaemae.co.kr/product_detail.php?no={product_id}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("도매매 스크래핑 실패(%s): %s", product_id, e)
        return {}

    soup = BeautifulSoup(resp.text, "lxml")
    result: dict = {}

    for sel in [".main-image img", ".product-thumb img", "#mainImg", ".rep-img img"]:
        el = soup.select_one(sel)
        if el and el.get("src"):
            result["main_image"] = _fix_url(el["src"])
            break

    subs = []
    for sel in [".sub-images img", ".thumbnail img", ".img-list img"]:
        imgs = soup.select(sel)
        if imgs:
            subs = [_fix_url(i["src"]) for i in imgs if i.get("src")][:9]
            break
    result["sub_images"] = subs

    for sel in [".product-detail", "#detailContent", ".detail-area", ".item-detail"]:
        el = soup.select_one(sel)
        if el:
            result["detail_html"] = str(el)
            result["detail_images"] = [_fix_url(i["src"]) for i in el.select("img") if i.get("src")]
            break

    m = re.search(r"[A-Z]{2,3}-\d{3,4}-\d{4,6}", resp.text)
    if m:
        result["kc_cert_no"] = m.group(0)

    return result


# ── 상품 정보 수집 ─────────────────────────────────────────────────

def fetch_product_info(url: str, client) -> dict:
    """도매매/도매꾹 URL에서 상품 정보 수집.

    Args:
        url: 상품 URL 또는 상품번호
        client: DomaemaeClient 인스턴스 (세션 관리 포함)

    Returns: {
        supplier, product_id, supplier_url,
        title, supply_price, stock,
        origin, model, kc_cert_no,
        main_image, sub_images, detail_images, detail_html,
        options, category_id, category_name
    }
    """
    supplier, product_id = extract_product_id(url)

    # API로 기본 정보 조회
    raw = client._get("getItemView", "4.5", {"no": product_id})
    basis    = raw.get("basis", {})
    price_d  = raw.get("price", {})
    qty      = raw.get("qty", {})
    cat_d    = raw.get("category", {})

    supply_price = int(price_d.get("supply") or price_d.get("dome") or 0)
    stock = int(qty.get("inventory", 0))

    # 카테고리
    cat_name = ""
    if isinstance(cat_d, dict):
        cat_name = (cat_d.get("name") or cat_d.get("cateName") or
                    cat_d.get("categoryName") or "")
    elif isinstance(cat_d, list) and cat_d:
        cat_name = str(cat_d[-1])

    # 옵션
    select_opt = raw.get("selectOpt", {})
    options = []
    if isinstance(select_opt, dict):
        for code, info in select_opt.items():
            if not isinstance(info, dict):
                continue
            if int(info.get("hid", 0)) == 2:
                continue
            name = str(info.get("name", "")).strip()
            if name:
                options.append({"id": code, "name": name})

    # API에서 이미지 시도
    img_d = raw.get("img", raw.get("images", {}))
    main_image = ""
    sub_images: list[str] = []
    detail_images: list[str] = []
    detail_html = ""

    if isinstance(img_d, dict):
        raw_main = img_d.get("main") or img_d.get("big") or img_d.get("url") or ""
        if isinstance(raw_main, list):
            raw_main = raw_main[0] if raw_main else ""
        main_image = _fix_url(str(raw_main)) if raw_main else ""

        raw_sub = img_d.get("sub") or img_d.get("list") or []
        if isinstance(raw_sub, list):
            sub_images = [_fix_url(str(s)) for s in raw_sub if s][:9]

        raw_detail = img_d.get("detail") or ""
        if isinstance(raw_detail, str) and raw_detail.startswith("<"):
            detail_html = raw_detail

    # 웹 스크래핑으로 보완
    scraped = (_scrape_domaekkuk if supplier == "domaekkuk" else _scrape_domaemae)(product_id)

    if not main_image:
        main_image = scraped.get("main_image", "")
    if not sub_images:
        sub_images = scraped.get("sub_images", [])
    if not detail_images:
        detail_images = scraped.get("detail_images", [])
    if not detail_html:
        detail_html = scraped.get("detail_html", "")

    kc_cert_no = (
        basis.get("kc_cert_no") or basis.get("kcCertNo") or
        basis.get("certNo") or scraped.get("kc_cert_no") or ""
    )

    return {
        "supplier":      supplier,
        "product_id":    product_id,
        "supplier_url":  url,
        "title":         basis.get("title", ""),
        "supply_price":  supply_price,
        "stock":         stock,
        "origin":        basis.get("origin") or basis.get("madeIn") or "",
        "model":         basis.get("model") or basis.get("modelNo") or "",
        "kc_cert_no":    kc_cert_no,
        "main_image":    main_image,
        "sub_images":    sub_images,
        "detail_images": detail_images,
        "detail_html":   detail_html,
        "options":       options,
        "category_id":   cat_name,   # 원본 카테고리명 (GUI에서 SS ID로 변환)
        "category_name": cat_name,
    }


# ── 스마트스토어 payload 구성 ────────────────────────────────────────

def build_smartstore_payload(
    info: dict,
    selling_price: int,
    settings: dict,
    category_id: str = "",
) -> dict:
    """스마트스토어 상품 등록 API payload 구성."""
    seller_phone   = settings.get("seller_phone", "")
    tags           = generate_tags(info["title"])
    default_cat_id = settings.get("default_category_id", "50000000")
    leaf_cat_id    = category_id or map_category(info.get("category_name", ""), default_cat_id)

    # 상세설명 HTML
    if info.get("detail_html"):
        detail_content = info["detail_html"]
    elif info.get("detail_images"):
        detail_content = "".join(f'<img src="{u}"/>' for u in info["detail_images"])
    else:
        detail_content = f"<p>{info.get('title', '')}</p>"

    # 이미지
    images: dict = {}
    if info.get("main_image"):
        images["representativeImage"] = {"url": info["main_image"]}
    if info.get("sub_images"):
        images["optionalImages"] = [{"url": u} for u in info["sub_images"][:9]]

    payload: dict = {
        "originProduct": {
            "statusType":     "SALE",
            "saleType":       "NEW",
            "leafCategoryId": leaf_cat_id,
            "name":           info["title"],
            "detailContent":  detail_content,
            "images":         images,
            "salePrice":      selling_price,
            "stockQuantity":  999,
            "deliveryInfo": {
                "deliveryType":          "DELIVERY",
                "deliveryAttributeType": "NORMAL",
                "deliveryFee": {
                    "deliveryFeeType":    "CHARGE",
                    "baseFee":            3000,
                    "deliveryFeePayType": "PREPAY",
                },
                "returnDeliveryFee":   3000,
                "exchangeDeliveryFee": 6000,
            },
            "detailAttribute": {
                "afterServiceInfo": {
                    "afterServiceTelephoneNumber": seller_phone,
                    "afterServiceGuideContent":    "구매 후 문의사항은 판매자에게 연락해주세요.",
                },
                "originAreaInfo": {
                    "originNation": info.get("origin") or "국내",
                },
                "taxType":          "TAX",
                "singlePackageYn":  False,
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "ETC",
                    "etc": {
                        "returnCostReason":         "상품 설명 참조",
                        "noRefundReason":           "상품 설명 참조",
                        "qualityAssuranceStandard": "상품 설명 참조",
                        "compensationProcedure":    "상품 설명 참조",
                        "troubleShootingContents":  "상품 설명 참조",
                    },
                },
            },
        }
    }

    # 모델명
    if info.get("model"):
        payload["originProduct"]["detailAttribute"]["naverShoppingSearchInfo"] = {
            "modelName": info["model"]
        }

    # KC 인증
    if info.get("kc_cert_no"):
        payload["originProduct"]["detailAttribute"]["productCertificationInfos"] = [
            {
                "certificationKind":   "KC_CERTIFICATION",
                "certificationNumber": info["kc_cert_no"],
            }
        ]

    # 옵션
    if info.get("options"):
        payload["originProduct"]["optionInfo"] = {
            "optionCombinationGroupNames": {"optionGroupName1": "옵션"},
            "optionCombinations": [
                {
                    "optionName1":   opt["name"],
                    "stockQuantity": 999,
                    "price":         0,
                    "usable":        True,
                }
                for opt in info["options"]
            ],
            "useStockManagement": True,
        }

    # 태그
    if tags:
        payload["originProduct"]["tag"] = [{"text": t} for t in tags]

    return payload


# ── 메인 등록 함수 ─────────────────────────────────────────────────

def register_product(
    url: str,
    margin: float,
    smartstore_api,
    supplier_client,
    settings: dict,
    mapping_repo,
    category_id: str = "",
) -> dict:
    """상품 수집 → 판매가 계산 → 스마트스토어 등록 → 매핑 저장.

    Returns:
        {"success": True,  "product_id": str, "selling_price": int, "info": dict}
        {"success": False, "error": str, "info": dict (수집 완료 시)}
    """
    # 1. 상품 정보 수집
    try:
        info = fetch_product_info(url, supplier_client)
    except Exception as e:
        return {"success": False, "error": f"상품 정보 수집 실패: {e}"}

    if not info.get("title"):
        return {"success": False, "error": "상품명을 가져오지 못했습니다.", "info": info}
    if not info.get("supply_price"):
        return {"success": False, "error": "공급가를 가져오지 못했습니다.", "info": info}

    # 2. 판매가 계산
    selling_price = calculate_selling_price(info["supply_price"], margin=margin)

    # 3. payload 구성
    payload = build_smartstore_payload(info, selling_price, settings, category_id)

    # 4. 스마트스토어 등록
    try:
        resp = requests.post(
            f"{smartstore_api.BASE_URL}/v2/products",
            headers=smartstore_api._headers(),
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        result     = resp.json()
        ss_prod_id = str(
            result.get("originProductNo") or
            result.get("id") or
            result.get("productNo") or ""
        )
    except Exception as e:
        logger.error("스마트스토어 상품 등록 실패: %s", e)
        return {"success": False, "error": str(e), "info": info, "selling_price": selling_price}

    # 5. 매핑 저장
    try:
        mapping_repo.add(
            ss_product_id      = ss_prod_id,
            supplier           = info["supplier"],
            supplier_url_or_id = url,
            price_margin_rate  = round(1 / (1 - margin), 6),
            memo               = f"자동등록 {info['title'][:40]}",
        )
    except Exception as e:
        logger.warning("매핑 저장 실패 (등록은 성공): %s", e)

    return {
        "success":       True,
        "product_id":    ss_prod_id,
        "selling_price": selling_price,
        "info":          info,
    }
