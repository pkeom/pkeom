#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Roomood 키워드 검색량 자동조회 — 네이버 검색광고 API keywordstool"""
import os, sys, time, hmac, hashlib, base64, json
import urllib.request, urllib.parse, urllib.error

BASE = "https://api.searchad.naver.com"
URI  = "/keywordstool"
SHOP_URL = "https://openapi.naver.com/v1/search/shop.json"
MIN_VOL = 100

# 상품수 조회는 검색량 상위 N개만 (쇼핑 API 하루 25,000회 제한)
TOP_N_SHOP = 100
# 경쟁강도(상품수/월검색량) 신호등 기준 — 낮을수록 좋음
COMP_GREEN  = 1
COMP_YELLOW = 10

def _env():
    cid = os.environ.get("NAVER_AD_CUSTOMER_ID")
    lic = os.environ.get("NAVER_AD_ACCESS_LICENSE")
    sec = os.environ.get("NAVER_AD_SECRET_KEY")
    if not all([cid, lic, sec]):
        raise RuntimeError("환경변수 3개(NAVER_AD_CUSTOMER_ID/ACCESS_LICENSE/SECRET_KEY)를 .env에 넣으세요.")
    return cid, lic, sec

def _sign(secret, ts, method, uri):
    msg = f"{ts}.{method}.{uri}"
    return base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()

def _num(v):
    if isinstance(v, str):
        v = v.replace("<", "").replace(",", "").strip()
        try: return int(float(v))
        except: return 0
    return int(v or 0)

def shop_total(keyword):
    """네이버 쇼핑 검색 API로 키워드의 총 상품수(total) 반환. 실패/미설정 시 None."""
    cid = os.environ.get("NAVER_SEARCH_CLIENT_ID")
    sec = os.environ.get("NAVER_SEARCH_CLIENT_SECRET")
    if not (cid and sec):
        return None
    qs = urllib.parse.urlencode({"query": keyword, "display": 1})
    req = urllib.request.Request(f"{SHOP_URL}?{qs}")
    req.add_header("X-Naver-Client-Id", cid)
    req.add_header("X-Naver-Client-Secret", sec)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return int(json.loads(r.read().decode()).get("total"))
    except Exception:
        return None

def _level(vol, strength, comp):
    """신호등 계산: 경쟁강도 있으면 그걸로(낮을수록 좋음), 없으면 광고경쟁(compIdx) 폴백."""
    if vol < MIN_VOL:
        return "🔴"
    if strength is not None:
        if strength <= COMP_GREEN:  return "🟢"
        if strength <= COMP_YELLOW: return "🟡"
        return "🟠"
    if comp == "낮음": return "🟢"
    if comp == "중간": return "🟡"
    return "🟠"

def fetch(hints):
    cid, lic, sec = _env()
    ts = str(round(time.time() * 1000))
    qs = urllib.parse.urlencode({"hintKeywords": ",".join(hints[:5]), "showDetail": "1"})
    req = urllib.request.Request(f"{BASE}{URI}?{qs}")
    req.add_header("X-Timestamp", ts)
    req.add_header("X-API-KEY", lic)
    req.add_header("X-Customer", str(cid))
    req.add_header("X-Signature", _sign(sec, ts, "GET", URI))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API 오류 {e.code}: {e.read().decode(errors='ignore')}")
    except Exception as e:
        raise RuntimeError(f"요청 실패: {e}")

def rank(hints):
    rows = []
    for k in fetch(hints).get("keywordList", []):
        vol = _num(k.get("monthlyPcQcCnt")) + _num(k.get("monthlyMobileQcCnt"))
        rows.append({
            "keyword": k.get("relKeyword", ""),
            "volume":  vol,
            "comp":    k.get("compIdx", "-"),   # 광고경쟁 (compIdx)
            "total":         None,               # 상품수 (쇼핑 API)
            "comp_strength": None,               # 경쟁강도 = 상품수/월검색량
        })

    # 상품수: 검색량 상위 TOP_N_SHOP 개만 조회 (쇼핑 API 호출수 절약)
    top_idx = sorted(range(len(rows)), key=lambda i: -rows[i]["volume"])[:TOP_N_SHOP]
    for i in top_idx:
        total = shop_total(rows[i]["keyword"])
        rows[i]["total"] = total
        if total is not None and rows[i]["volume"] > 0:
            rows[i]["comp_strength"] = round(total / rows[i]["volume"], 2)
        time.sleep(0.05)

    for r in rows:
        r["level"] = _level(r["volume"], r["comp_strength"], r["comp"])

    order = {"🟢":0,"🟡":1,"🟠":2,"🔴":3}
    rows.sort(key=lambda r: (order[r["level"]], -r["volume"]))
    return rows

if __name__ == "__main__":
    hints = sys.argv[1:] or ["무드등","불멍등","오일램프"]
    print(f"\n씨앗: {', '.join(hints)} / 기준: 월검색 {MIN_VOL}+ & 경쟁'낮음'=🟢\n")
    try:
        for r in rank(hints):
            ts = f"  |  상품수 {r['total']:,}" if r['total'] is not None else ""
            cs = f"  |  경쟁강도 {r['comp_strength']}" if r['comp_strength'] is not None else ""
            print(f"{r['level']} {r['keyword']}  |  월검색 {r['volume']:,}  |  경쟁 {r['comp']}{ts}{cs}")
    except RuntimeError as e:
        print(f"[오류] {e}")
        sys.exit(1)
    print()
