"""mappings.json 기존 항목의 ss_product_id를
originProductNo → channelProductNo 로 마이그레이션.

SS API GET /v2/products/{originProductNo} 응답에서
channelProductNo 를 추출해 ss_product_id 를 교체하고,
originProductNo 는 origin_product_no 필드로 보존.

사용법:
    python migrate_mapping_ids.py            # 실제 저장
    python migrate_mapping_ids.py --dry-run  # 결과 미리보기만
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.utils.config_loader import load_config
from src.api.smartstore import SmartstoreAPI
from src.core.mapping_repository import MappingRepository


def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("[DRY-RUN] 실제 저장하지 않고 결과만 출력합니다.\n")

    cfg    = load_config()
    ss_cfg = {k: v for k, v in cfg["smartstore"].items()
              if k in ("client_id", "client_secret", "account_type")}
    ss_api = SmartstoreAPI(**ss_cfg)
    repo   = MappingRepository()

    mappings = repo._read()
    if not mappings:
        print("mappings.json 에 항목이 없습니다.")
        return

    print(f"총 {len(mappings)}건 확인 중...\n")

    updated = 0
    skipped = 0
    failed  = 0

    for m in mappings:
        ss_id          = m["ss_product_id"]
        existing_origin = m.get("origin_product_no", "")

        # origin_product_no 가 ss_product_id 와 다른 값으로 이미 설정 → 마이그레이션 완료
        if existing_origin and existing_origin != ss_id:
            print(f"  [SKIP] id={m['id']}  channel={ss_id}  origin={existing_origin}  (이미 완료)")
            skipped += 1
            continue

        memo_preview = (m.get("memo") or "")[:30]
        print(f"  조회: id={m['id']}  ss_product_id={ss_id}  ({memo_preview})")

        try:
            data = ss_api.get_product(ss_id)

            origin_id  = str(
                data.get("originProduct", {}).get("originProductNo") or ss_id
            )
            channel_id = str(
                data.get("smartstoreChannelProduct", {}).get("channelProductNo") or ""
            )

            if not channel_id:
                print(f"    [WARN] channelProductNo 없음 — 스킵")
                skipped += 1
                time.sleep(0.3)
                continue

            if channel_id == ss_id:
                # 이미 channelProductNo 로 저장된 경우 → origin 필드만 추가
                m["origin_product_no"] = origin_id
                print(f"    [OK]  ss_product_id 정상 (channel={channel_id}), origin_product_no 추가")
            else:
                m["ss_product_id"]     = channel_id
                m["origin_product_no"] = origin_id
                print(f"    [UPDATE]  {ss_id} → channel={channel_id}, origin={origin_id}")

            updated += 1
            time.sleep(0.2)

        except Exception as e:
            print(f"    [ERROR] {e}")
            failed += 1
            time.sleep(0.5)

    print(f"\n결과: 업데이트 {updated}건 / 스킵 {skipped}건 / 실패 {failed}건")

    if not dry_run and updated > 0:
        repo._write(mappings)
        print("mappings.json 저장 완료.")
    elif dry_run:
        print("[DRY-RUN] 저장하지 않음.")
    else:
        print("변경 사항 없음.")


if __name__ == "__main__":
    main()
