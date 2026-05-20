"""
스마트스토어 실제 상품등록 테스트 (도매꾹 5개 + 도매매 5개)
- 카테고리 자동매핑 + KC인증 처리 검증
- 실제 Naver Commerce API로 등록 시도
- 모두 성공할 때까지 오류 수정 후 재테스트

실행: python test_realapi.py
"""
import sys
import io
import json
import logging

# Windows 콘솔 UTF-8 출력 설정
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from src.utils.config_loader import load_config
from src.api.domaemae import DomaemaeClient
from src.api.smartstore import SmartstoreAPI
from src.core.product_register import register_product

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("realapi_test")

# 도매꾹 6개 (HOME_APPLIANCES + BAG + FURNITURE + ETC 포함)
DOMAEKKUK_URLS = [
    "https://www.domeggook.com/59922903",  # 휴대용선풍기 (디지털/가전 — HOME_APPLIANCES + KC)
    "https://www.domeggook.com/59916000",  # 서랍장 (가구/인테리어 — FURNITURE)
    "https://www.domeggook.com/59921000",  # 여성 숄더백 (BAG)
    "https://www.domeggook.com/59919000",  # 알림판 (ETC)
    "https://www.domeggook.com/59915000",  # 텀블러파우치 (ETC)
    "https://www.domeggook.com/59500000",  # 동전지갑 (BAG)
]
# 도매매 5개
DOMAEMAE_URLS = [
    "https://domeme.domeggook.com/s/59916000",  # 서랍장
    "https://domeme.domeggook.com/s/59921000",  # 여성 숄더백
    "https://domeme.domeggook.com/s/59919000",  # 알림판
    "https://domeme.domeggook.com/s/59915000",  # 텀블러파우치
    "https://domeme.domeggook.com/s/59600000",  # 남성 클러치백
]

TEST_PRODUCTS = DOMAEKKUK_URLS + DOMAEMAE_URLS


class _NullRepo:
    """매핑 저장을 무시하는 더미 repo (테스트용)."""
    def add(self, **kwargs):
        pass


def main():
    cfg = load_config()

    smartstore_api = SmartstoreAPI(
        client_id     = cfg["smartstore"]["client_id"],
        client_secret = cfg["smartstore"]["client_secret"],
    )
    supplier_client = DomaemaeClient(
        api_key  = cfg["domaemae"]["api_key"],
        user_id  = cfg["domaemae"].get("user_id", ""),
        password = cfg["domaemae"].get("password", ""),
    )
    mapping_repo = _NullRepo()

    print("=" * 70)
    print("스마트스토어 실제 상품등록 테스트 (도매꾹 5개 + 도매매 5개)")
    print("=" * 70)

    results = []
    for idx, url in enumerate(TEST_PRODUCTS, 1):
        supplier = "도매꾹" if "domeggook.com/" in url and "domeme" not in url else "도매매"
        print(f"\n[{idx:02d}/{len(TEST_PRODUCTS)}] [{supplier}] {url}")
        print("-" * 70)

        try:
            result = register_product(
                url             = url,
                selling_price   = 0,    # 0이면 공급가 기반 자동 계산
                smartstore_api  = smartstore_api,
                supplier_client = supplier_client,
                settings        = cfg,
                mapping_repo    = mapping_repo,
                category_id     = "",   # 자동 매핑
            )
        except Exception as e:
            import traceback
            result = {"success": False, "error": str(e), "traceback": traceback.format_exc()}

        results.append({"url": url, **result})

        if result.get("success"):
            print(f"  ✅ 등록 성공!")
            print(f"     originProductNo : {result.get('product_id', '-')}")
            print(f"     판매가          : {result.get('selling_price', 0):,}원")
            print(f"     카테고리        : {result.get('category_match', '-')} ({result.get('category_id', '-')})")
            if result.get("info"):
                print(f"     상품명          : {result['info'].get('title', '')[:60]}")
                print(f"     KC인증번호      : {result['info'].get('kc_cert_no', '없음') or '없음'}")
                print(f"     KC인증기관      : {result['info'].get('kc_cert_agency', '없음') or '없음'}")
        else:
            print(f"  ❌ 등록 실패: {result.get('error', '알 수 없음')}")
            if result.get("detail"):
                detail = result["detail"]
                if isinstance(detail, dict):
                    print(f"     상세: {json.dumps(detail, ensure_ascii=False, indent=2)[:500]}")
                else:
                    print(f"     상세: {str(detail)[:500]}")
            if result.get("traceback"):
                print(f"     Traceback:\n{result['traceback']}")

    print("\n" + "=" * 70)
    passed = sum(1 for r in results if r.get("success"))
    failed_urls = [r["url"] for r in results if not r.get("success")]

    print(f"최종 결과: {passed}/{len(results)} 성공")

    if failed_urls:
        print("\n실패한 상품:")
        for u in failed_urls:
            r = next(x for x in results if x["url"] == u)
            print(f"  - {u}")
            print(f"    오류: {r.get('error', '?')}")

    if passed == len(results):
        print("\n✅ 전체 등록 테스트 완료! 카테고리 자동매핑 + KC인증 모두 정상 처리")
    else:
        print(f"\n❌ {len(results) - passed}개 실패 — 로그를 확인하고 코드 수정 후 재테스트")
        sys.exit(1)


if __name__ == "__main__":
    main()
