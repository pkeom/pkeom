"""
스마트스토어 상품등록 payload dry-run 테스트
- 실제 Naver 상품등록 API 호출 없이 payload 구조·필드 유효성만 검증
- 도매꾹 5개 + 도매매 5개 상품, 각 5회씩 = 총 50회 연속 통과해야 완료
- 이미지 업로드는 mock (placeholder Naver CDN URL 사용)

실행: python test_dryrun.py
"""
import sys
import io
import time
import logging
import traceback

# Windows 콘솔 UTF-8 출력 설정
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from src.utils.config_loader import load_config
from src.api.domaemae import DomaemaeClient
from src.core.product_register import (
    fetch_product_info, build_smartstore_payload, calculate_selling_price,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("dryrun")

# ── 테스트 상품 URL 목록 ────────────────────────────────────────────
# 도매꾹 5개 (유효성 확인된 상품)
DOMAEKKUK_URLS = [
    "https://www.domeggook.com/59922903",  # 터빈선풍기 (7,900원)
    "https://www.domeggook.com/59921000",  # 여성 숄더백 (28,000원)
    "https://www.domeggook.com/59919000",  # 알림판 - 수량별 가격 형식
    "https://www.domeggook.com/59915000",  # 텀블러파우치 (2,430원)
    "https://www.domeggook.com/59500000",  # 동전지갑 - 수량별 가격 형식
]
# 도매매 5개 (동일 상품번호, domeme.domeggook.com)
DOMAEMAE_URLS = [
    "https://domeme.domeggook.com/s/59922903",  # 터빈선풍기
    "https://domeme.domeggook.com/s/59921000",  # 여성 숄더백
    "https://domeme.domeggook.com/s/59919000",  # 알림판
    "https://domeme.domeggook.com/s/59915000",  # 텀블러파우치
    "https://domeme.domeggook.com/s/59600000",  # 남성 클러치백 (5,000원)
]

PLACEHOLDER_IMG = "https://shop-phinf.pstatic.net/test/placeholder.jpg"
RUNS_PER_PRODUCT = 5
REQUIRED_TOTAL   = 50  # 목표: 10개 상품 × 5회 = 50회 연속 통과

VALID_ORIGIN_CODES = {
    "01", "02", "03", "04",
    "CN", "JP", "US", "VN", "TH", "IN", "DE", "IT", "FR", "GB",
    "AU", "CA", "MX", "BR", "ID", "MY", "PH", "SG", "TW", "HK",
}


# ── payload 유효성 검사 ─────────────────────────────────────────────

def validate_payload(payload: dict) -> list[str]:
    """필드 구조·값 유효성 검사. 오류 목록 반환 (빈 리스트 = 통과)."""
    errors: list[str] = []
    op = payload.get("originProduct", {})

    # 상품명
    name = op.get("name", "")
    if not name:
        errors.append("상품명(name) 누락")
    elif len(name) > 100:
        errors.append(f"상품명 100자 초과: {len(name)}자 — {name[:30]}...")

    # 판매가
    price = op.get("salePrice", 0)
    if not price or price <= 0:
        errors.append(f"판매가(salePrice) 유효하지 않음: {price}")

    # 대표이미지
    rep_img = op.get("images", {}).get("representativeImage", {}).get("url", "")
    if not rep_img:
        errors.append("대표이미지(representativeImage) 누락")

    # 상세설명
    if not op.get("detailContent"):
        errors.append("상세설명(detailContent) 누락")

    da = op.get("detailAttribute", {})

    # afterServiceInfo
    asi = da.get("afterServiceInfo", {})
    if not asi.get("afterServiceTelephoneNumber"):
        errors.append("A/S 전화번호 누락")

    # originAreaInfo
    oai = da.get("originAreaInfo", {})
    if not oai:
        errors.append("originAreaInfo 누락")
    else:
        code = oai.get("originAreaCode", "")
        if not code:
            errors.append("originAreaCode 누락")
        elif code not in VALID_ORIGIN_CODES:
            errors.append(f"originAreaCode 유효하지 않음: {code!r}")
        if code == "04" and not oai.get("content"):
            errors.append("originAreaCode='04'인데 content 비어있음")

    # productInfoProvidedNotice.etc.itemName ≤ 50자
    ppn_etc = da.get("productInfoProvidedNotice", {}).get("etc", {})
    item_name = ppn_etc.get("itemName", "")
    if item_name and len(item_name) > 50:
        errors.append(f"itemName 50자 초과: {len(item_name)}자")

    # KC인증: 있으면 certificationKind + certificationNumber 모두 있어야 함
    cert_infos = da.get("productCertificationInfos", [])
    for i, ci in enumerate(cert_infos):
        if not ci.get("certificationKind"):
            errors.append(f"productCertificationInfos[{i}].certificationKind 누락")
        if not ci.get("certificationNumber"):
            errors.append(f"productCertificationInfos[{i}].certificationNumber 누락")
        if ci.get("name") is not None and not ci.get("name"):
            errors.append(f"productCertificationInfos[{i}].name 비어있음 (키 제거 필요)")

    # 배송정보
    di = op.get("deliveryInfo", {})
    if not di:
        errors.append("deliveryInfo 누락")
    else:
        if not di.get("deliveryType"):
            errors.append("deliveryInfo.deliveryType 누락")
        fee = di.get("deliveryFee", {})
        if not fee.get("deliveryFeeType"):
            errors.append("deliveryFee.deliveryFeeType 누락")

    return errors


# ── mock 이미지 적용 ────────────────────────────────────────────────

def mock_images(info: dict) -> dict:
    """이미지 URL을 placeholder Naver CDN URL로 교체 (업로드 없이 payload 구성용).

    dry-run에서는 실제 이미지 업로드 없이 payload 구조만 검증하므로
    main_image가 없어도 placeholder를 항상 주입한다.
    """
    result = dict(info)
    # 대표이미지: 없으면 placeholder 주입 (dry-run에서는 항상 필요)
    result["main_image"] = PLACEHOLDER_IMG
    # 추가이미지
    if result.get("sub_images"):
        result["sub_images"] = [PLACEHOLDER_IMG] * min(len(result["sub_images"]), 3)
    else:
        result["sub_images"] = [PLACEHOLDER_IMG]
    # 상세이미지: detail_html 대신 detail_images 사용 (build_smartstore_payload 내부 로직)
    result["detail_html"]   = ""
    result["detail_images"] = [PLACEHOLDER_IMG]
    return result


# ── 상품 정보 수집 (캐시) ───────────────────────────────────────────

def collect_products(urls: list[str], client, settings: dict) -> list[dict]:
    """URL 목록에서 상품 정보 수집. 실패한 URL은 건너뜀."""
    products = []
    for url in urls:
        try:
            logger.warning("[수집] %s", url)
            info = fetch_product_info(url, client)
            if not info.get("title") or not info.get("supply_price"):
                logger.warning("  → 상품명/가격 없음, 스킵: %s", url)
                continue
            products.append(info)
            logger.warning("  → OK: %s / 공급가=%s", info["title"][:40], info["supply_price"])
        except Exception as e:
            logger.warning("  → 수집 실패 (%s): %s", url, e)
    return products


# ── 단일 dry-run ────────────────────────────────────────────────────

def run_single(info: dict, settings: dict) -> tuple[bool, list[str]]:
    """한 상품에 대해 payload 생성 + 유효성 검사. (성공 여부, 오류목록)"""
    try:
        selling_price = calculate_selling_price(
            info["supply_price"],
            margin=settings.get("default_margin", 0.3),
        )
        mocked = mock_images(info)
        payload = build_smartstore_payload(mocked, selling_price, settings)
        errors  = validate_payload(payload)
        return (len(errors) == 0), errors
    except Exception as e:
        return False, [f"예외 발생: {e}\n{traceback.format_exc()}"]


# ── 메인 ────────────────────────────────────────────────────────────

def main():
    cfg      = load_config()
    settings = cfg

    client = DomaemaeClient(
        api_key  = cfg["domaemae"]["api_key"],
        user_id  = cfg["domaemae"].get("user_id", ""),
        password = cfg["domaemae"].get("password", ""),
    )

    print("=" * 60)
    print("스마트스토어 상품등록 Dry-Run 테스트")
    print("=" * 60)

    # ── 상품 수집 ─────────────────────────────────────────────────
    print("\n[1단계] 상품 정보 수집 중...")
    all_urls = DOMAEKKUK_URLS + DOMAEMAE_URLS
    products = collect_products(all_urls, client, settings)

    if not products:
        print("수집된 상품이 없습니다. URL을 확인하세요.")
        sys.exit(1)

    print(f"\n수집 완료: {len(products)}개 상품")

    # ── dry-run 루프 ──────────────────────────────────────────────
    attempt = 0
    while True:
        attempt += 1
        print(f"\n[2단계] Dry-Run 시도 #{attempt} ({len(products)}개 x {RUNS_PER_PRODUCT}회)")
        print("-" * 60)

        total   = 0
        passed  = 0
        failed  = 0
        first_fail_info = None

        for idx, info in enumerate(products):
            for run in range(RUNS_PER_PRODUCT):
                total += 1
                ok, errors = run_single(info, settings)
                label = f"  [{total:02d}] {info['title'][:35]:<35} run#{run+1}"
                if ok:
                    passed += 1
                    print(f"{label} → ✅ PASS")
                else:
                    failed += 1
                    print(f"{label} → ❌ FAIL")
                    for err in errors:
                        print(f"       오류: {err}")
                    if first_fail_info is None:
                        first_fail_info = (info, errors)

        print("-" * 60)
        print(f"결과: {passed}/{total} 통과 ({failed}건 실패)")

        if failed == 0:
            marker = "50회" if total >= REQUIRED_TOTAL else f"{total}회"
            print(f"\n✅ {marker} 연속 dry-run 성공! (시도 #{attempt})")
            break
        else:
            print(f"\n❌ {failed}건 실패 — 원인 분석 후 재시도 필요")
            if first_fail_info:
                info, errors = first_fail_info
                print(f"  첫 실패 상품: {info['title'][:50]}")
                print(f"  오류 목록: {errors}")
            print("\n코드 수정 후 다시 실행하거나, Ctrl+C로 종료하세요.")
            sys.exit(1)


if __name__ == "__main__":
    main()
