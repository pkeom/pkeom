"""
스마트스토어 실제 상품등록 테스트 (2회)
- dry-run 50회 연속 통과 후 실행
- 도매꾹 1개 + 도매매 1개를 실제 Naver Commerce API로 등록
- 등록 성공 시 originProductNo 반환

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

# 테스트 대상 상품 (도매꾹 1개 + 도매매 1개)
TEST_PRODUCTS = [
    "https://www.domeggook.com/59500000",        # 동전지갑 — KC인증 불필요 (패션잡화)
    "https://domeme.domeggook.com/s/59921000",  # 여성 숄더백 (28,000원)
]


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

    print("=" * 60)
    print("스마트스토어 실제 상품등록 테스트 (2회)")
    print("=" * 60)

    results = []
    for idx, url in enumerate(TEST_PRODUCTS, 1):
        print(f"\n[{idx}/{len(TEST_PRODUCTS)}] 등록 중: {url}")
        print("-" * 60)

        try:
            result = register_product(
                url             = url,
                selling_price   = 0,    # 0이면 공급가 기반 자동 계산
                smartstore_api  = smartstore_api,
                supplier_client = supplier_client,
                settings        = cfg,
                mapping_repo    = mapping_repo,
                category_id     = "",   # 자동 매칭
            )
        except Exception as e:
            import traceback
            result = {"success": False, "error": str(e), "traceback": traceback.format_exc()}

        results.append(result)

        if result.get("success"):
            print(f"  ✅ 등록 성공!")
            print(f"     originProductNo : {result.get('product_id', '-')}")
            print(f"     판매가          : {result.get('selling_price', 0):,}원")
            print(f"     카테고리 매칭   : {result.get('category_match', '-')}")
            if result.get("info"):
                print(f"     상품명          : {result['info'].get('title', '')[:60]}")
        else:
            print(f"  ❌ 등록 실패")
            print(f"     오류: {result.get('error', '알 수 없음')}")
            if result.get("detail"):
                detail = result["detail"]
                if isinstance(detail, dict):
                    print(f"     상세: {json.dumps(detail, ensure_ascii=False, indent=2)[:500]}")
                else:
                    print(f"     상세: {str(detail)[:500]}")
            if result.get("traceback"):
                print(f"     Traceback:\n{result['traceback']}")

    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r.get("success"))
    print(f"최종 결과: {passed}/{len(results)} 성공")

    if passed == len(results):
        print("✅ 실제 API 등록 테스트 완료!")
    else:
        print("❌ 일부 등록 실패 — 로그를 확인하세요.")
        sys.exit(1)


if __name__ == "__main__":
    main()
