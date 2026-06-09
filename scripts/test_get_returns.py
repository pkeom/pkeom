# -*- coding: utf-8 -*-
"""반품 감지 API 동작 테스트 -- 수정 후 검증용.

출력 항목:
  1. 실제 요청 URL + 쿼리 파라미터
  2. HTTP 응답 코드
  3. 응답 본문 raw (앞부분)
  4. data 언랩 후 lastChangeStatuses 건수 (파싱 정상 여부 확인)
  5. product-orders/query 로 넘어가는 ID 수
  6. 상세 조회 결과 건수
  7. claimStatus 필터 후 최종 건수 (RETURN_REQUEST / EXCHANGE_REQUEST)
"""
import json
import sys
import os
from datetime import datetime, timedelta, timezone
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests as _req

from src.utils.config_loader import load_config
from src.api.smartstore import SmartstoreAPI

# ── requests 패치 -- 요청/응답 투명 출력 ────────────────────────────────────
_orig_get  = _req.get
_orig_post = _req.post

SEP = "=" * 70


def _patched_get(url, **kw):
    params = kw.get("params") or {}
    print(f"\n{SEP}")
    print(f"[GET] {url}")
    for k, v in params.items():
        print(f"  {k} = {v!r}")
    print(SEP)
    r = _orig_get(url, **kw)
    print(f"[HTTP] {r.status_code}")
    snippet = r.text[:2000]
    print(snippet)
    if len(r.text) > 2000:
        print(f"  ... ({len(r.text)} chars total)")
    print(SEP)
    return r


def _patched_post(url, **kw):
    body = kw.get("json") or {}
    print(f"\n{SEP}")
    print(f"[POST] {url}")
    ids = body.get("productOrderIds", [])
    if ids:
        print(f"  productOrderIds: {len(ids)} ids -- {ids[:5]}")
    print(SEP)
    r = _orig_post(url, **kw)
    print(f"[HTTP] {r.status_code}")
    snippet = r.text[:3000]
    print(snippet)
    if len(r.text) > 3000:
        print(f"  ... ({len(r.text)} chars total)")
    print(SEP)
    return r


_req.get  = _patched_get
_req.post = _patched_post


# ── 핵심 테스트: get_returns() 단계별 투명 실행 ──────────────────────────────
def run_test(api: SmartstoreAPI):
    hours = 24
    now          = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=hours)
    from_str     = window_start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to_str       = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    print(f"\n[WINDOW] {from_str} ~ {to_str} ({hours}h)")

    all_ids: list[str] = []

    for status in ("RETURN_REQUEST", "EXCHANGE_REQUEST"):
        print(f"\n>>> [1단계] productOrderStatuses={status}")
        try:
            r = _req.get(
                f"{api.BASE_URL}/v1/pay-order/seller/product-orders/last-changed-statuses",
                headers=api._headers(),
                params={
                    "lastChangedFrom":      from_str,
                    "lastChangedTo":        to_str,
                    "productOrderStatuses": status,
                    "limitCount":           300,
                },
                timeout=10,
            )
            r.raise_for_status()
            # 수정된 파싱 -- data 래퍼 언랩
            raw_changed = r.json().get("data", {}).get("lastChangeStatuses", [])
            # 구버전 버그 재현 (비교용)
            bug_changed = r.json().get("lastChangeStatuses", [])
            ids = [item["productOrderId"] for item in raw_changed if item.get("productOrderId")]
            all_ids += ids
            print(f"\n[파싱 결과]")
            print(f"  data.lastChangeStatuses (수정 후) : {len(raw_changed)}건")
            print(f"  lastChangeStatuses (구버전 버그)  : {len(bug_changed)}건  <- 항상 0")
            print(f"  추출된 productOrderId            : {len(ids)}건")
            if raw_changed:
                print("  claimStatus 분포:")
                for k, v in Counter(
                    item.get("claimStatus", "(없음)") for item in raw_changed
                ).most_common():
                    print(f"    {k}: {v}건")
        except Exception as e:
            print(f"  [FAIL] {e}")

    all_ids = list(dict.fromkeys(all_ids))
    print(f"\n[합계] 1단계 수집 ID: {len(all_ids)}개 (중복 제거)")

    if not all_ids:
        print("\n[SKIP] product-orders/query -- ID 없음")
        print("\n[최종 결과] 0건 (지난 24h 내 반품/교환 신청 없음 -- 정상)")
        return

    # ── 2단계: 상세 조회 ────────────────────────────────────────────────────
    print(f"\n>>> [2단계] product-orders/query ({len(all_ids)}개)")
    try:
        r2 = _req.post(
            f"{api.BASE_URL}/v1/pay-order/seller/product-orders/query",
            headers=api._headers(),
            json={"productOrderIds": all_ids},
            timeout=10,
        )
        r2.raise_for_status()
        items = r2.json().get("data", [])
    except Exception as e:
        print(f"  [FAIL] {e}")
        return

    print(f"\n[2단계 결과] 상세 조회 {len(items)}건")

    # ── 3단계: claimStatus 필터 ─────────────────────────────────────────────
    _ACTIVE = {"RETURN_REQUEST", "EXCHANGE_REQUEST"}
    filtered = [
        item for item in items
        if (
            item.get("claim", {}).get("claimStatus")
            or item.get("productOrder", {}).get("claimStatus", "")
        ) in _ACTIVE
    ]

    print(f"\n{SEP}")
    print(f"[최종 결과 요약]")
    print(f"  1단계 ID 수집    : {len(all_ids)}건")
    print(f"  2단계 상세 조회  : {len(items)}건")
    print(f"  3단계 클레임 필터: {len(filtered)}건 (RETURN/EXCHANGE_REQUEST만)")
    print(SEP)

    if filtered:
        print("\n[필터된 주문 목록]")
        for i, item in enumerate(filtered, 1):
            po    = item.get("productOrder", item)
            claim = item.get("claim", {})
            print(f"  [{i}] order_id={po.get('productOrderId','?')} "
                  f"claimStatus={claim.get('claimStatus','?')} "
                  f"product={po.get('productName','?')!r}")
    elif items:
        print("\n[note] 상세 조회에서 건들이 왔으나 현재 claimStatus가 RETURN/EXCHANGE_REQUEST 아님")
        print("       (이미 해소된 건 -- 정상)")
        print("  claimStatus 분포:")
        for k, v in Counter(
            (item.get("claim", {}).get("claimStatus")
             or item.get("productOrder", {}).get("claimStatus", "(없음)"))
            for item in items
        ).most_common():
            print(f"    {k}: {v}건")


def main():
    cfg    = load_config()
    ss_cfg = cfg.get("smartstore", {})
    api    = SmartstoreAPI(
        client_id=ss_cfg["client_id"],
        client_secret=ss_cfg["client_secret"],
        account_type=ss_cfg.get("account_type", "SELF"),
    )
    print("[AUTH] 토큰 발급 중...")
    try:
        tok = api._get_token()
        print(f"[AUTH] 성공 (앞 20자: {tok[:20]}...)")
    except Exception as e:
        print(f"[AUTH FAIL] {e}")
        sys.exit(1)

    run_test(api)


if __name__ == "__main__":
    main()
