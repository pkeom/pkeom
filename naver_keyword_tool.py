#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Roomood 키워드 검색량 자동조회 — 네이버 검색광고 API keywordstool"""
import os, sys, time, hmac, hashlib, base64, json
import urllib.request, urllib.parse, urllib.error

BASE = "https://api.searchad.naver.com"
URI  = "/keywordstool"
MIN_VOL = 100

def _env():
    cid = os.environ.get("NAVER_AD_CUSTOMER_ID")
    lic = os.environ.get("NAVER_AD_ACCESS_LICENSE")
    sec = os.environ.get("NAVER_AD_SECRET_KEY")
    if not all([cid, lic, sec]):
        sys.exit("환경변수 3개(NAVER_AD_CUSTOMER_ID/ACCESS_LICENSE/SECRET_KEY)를 ~/.env에 넣고 source 하세요.")
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
        sys.exit(f"API 오류 {e.code}: {e.read().decode(errors='ignore')}")
    except Exception as e:
        sys.exit(f"요청 실패: {e}")

def rank(hints):
    rows = []
    for k in fetch(hints).get("keywordList", []):
        vol = _num(k.get("monthlyPcQcCnt")) + _num(k.get("monthlyMobileQcCnt"))
        comp = k.get("compIdx", "-")
        if   vol < MIN_VOL: level = "🔴"
        elif comp == "낮음": level = "🟢"
        elif comp == "중간": level = "🟡"
        else: level = "🟠"
        rows.append((level, k.get("relKeyword", ""), vol, comp))
    order = {"🟢":0,"🟡":1,"🟠":2,"🔴":3}
    rows.sort(key=lambda x: (order[x[0]], -x[2]))
    return rows

if __name__ == "__main__":
    hints = sys.argv[1:] or ["무드등","불멍등","오일램프"]
    print(f"\n씨앗: {', '.join(hints)} / 기준: 월검색 {MIN_VOL}+ & 경쟁'낮음'=🟢\n")
    for level, kw, vol, comp in rank(hints):
        print(f"{level} {kw}  |  월검색 {vol:,}  |  경쟁 {comp}")
    print()
