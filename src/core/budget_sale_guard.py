"""예산 부족 시 스마트스토어 전체 상품 판매중지 / 예산 충전 후 자동 복구

상태 파일: data/budget_suspension.json
  {
    "suspended": true,
    "suspended_at": "2026-06-02T10:00:00+00:00",
    "suspended_ids": ["12345678", "87654321"]
  }

suspend_all(): 활성 매핑의 모든 SS 상품을 판매중지. 이미 중지 상태면 스킵(멱등).
restore_all(): 판매중지된 SS 상품을 판매재개. 미중지 상태면 스킵(멱등).
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_FILE = Path("data/budget_suspension.json")


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            with open(_STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"suspended": False, "suspended_at": "", "suspended_ids": []}


def _save_state(data: dict):
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_suspended() -> bool:
    return bool(_load_state().get("suspended"))


def suspend_all(ss_api, mapping_repo, notifier=None) -> int:
    """활성 매핑의 모든 SS 상품을 판매중지.

    이미 중지 상태면 스킵. 처리에 성공한 상품 수를 반환.
    """
    state = _load_state()
    if state.get("suspended"):
        logger.info("예산 판매중지 이미 적용 중 — 스킵")
        return 0

    mappings = mapping_repo.all()
    # 옵션이 여러 개여도 상품 ID는 하나이므로 중복 제거
    product_ids = list({
        m["ss_product_id"] for m in mappings if m.get("is_active", True)
    })
    if not product_ids:
        logger.info("판매중지 대상 상품 없음")
        return 0

    success_ids: list[str] = []
    for pid in product_ids:
        try:
            ss_api.set_product_sale_status(pid, on_sale=False)
            success_ids.append(pid)
            logger.info("예산 부족 판매중지: ss_product=%s", pid)
        except Exception as e:
            logger.error("판매중지 실패: ss_product=%s — %s", pid, e)

    _save_state({
        "suspended":     True,
        "suspended_at":  datetime.now(timezone.utc).isoformat(),
        "suspended_ids": success_ids,
    })

    count = len(success_ids)
    logger.warning("예산 부족 — %d개 상품 판매중지 완료", count)

    if notifier:
        _notify_suspended(notifier, count, success_ids)

    return count


def restore_all(ss_api, mapping_repo, notifier=None) -> int:
    """판매중지 상태인 SS 상품을 판매재개.

    판매중지 상태가 아니면 스킵. 처리에 성공한 상품 수를 반환.
    """
    state = _load_state()
    if not state.get("suspended"):
        return 0

    suspended_ids: list[str] = state.get("suspended_ids", [])
    if not suspended_ids:
        # 혹시 목록이 없으면 현재 활성 매핑으로 재구성
        mappings = mapping_repo.all()
        suspended_ids = list({
            m["ss_product_id"] for m in mappings if m.get("is_active", True)
        })

    success_ids: list[str] = []
    for pid in suspended_ids:
        try:
            ss_api.set_product_sale_status(pid, on_sale=True)
            success_ids.append(pid)
            logger.info("예산 충전 후 판매재개: ss_product=%s", pid)
        except Exception as e:
            logger.error("판매재개 실패: ss_product=%s — %s", pid, e)

    _save_state({
        "suspended":     False,
        "suspended_at":  "",
        "suspended_ids": [],
    })

    count = len(success_ids)
    logger.info("예산 충전 후 %d개 상품 판매재개 완료", count)

    if notifier and count:
        _notify_restored(notifier, count, success_ids)

    return count


def _notify_suspended(notifier, count: int, product_ids: list[str]):
    subject = f"[긴급] 예산 부족 — {count}개 상품 판매중지 처리"
    lines = [
        f"■ 예산 부족으로 {count}개 상품의 판매가 중지되었습니다.",
        "",
        "■ 복구 방법",
        "  1. 대시보드 → 예산 관리 → 예산 충전",
        "  2. 또는 CLI: python add_budget.py <금액>",
        "  예산 충전 시 전체 상품 판매가 자동으로 재개됩니다.",
        "",
        "■ 판매중지 상품 ID",
        *[f"  - {pid}" for pid in product_ids[:20]],
        *(["  … (이하 생략)"] if len(product_ids) > 20 else []),
    ]
    try:
        notifier.send(subject=subject, body="\n".join(lines))
    except Exception as e:
        logger.error("예산 부족 판매중지 알림 전송 실패: %s", e)


def _notify_restored(notifier, count: int, product_ids: list[str]):
    subject = f"[복구] 예산 충전 — {count}개 상품 판매재개 완료"
    lines = [
        f"■ 예산이 충전되어 {count}개 상품의 판매가 재개되었습니다.",
        "",
        "■ 판매재개 상품 ID",
        *[f"  - {pid}" for pid in product_ids[:20]],
        *(["  … (이하 생략)"] if len(product_ids) > 20 else []),
    ]
    try:
        notifier.send(subject=subject, body="\n".join(lines))
    except Exception as e:
        logger.error("예산 충전 판매재개 알림 전송 실패: %s", e)
