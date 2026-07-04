"""구절 매핑 로직만 단독 검증 (Whisper/Demucs 재실행 없음 — debug_words.json 재사용)"""
# -*- coding: utf-8 -*-
import sys, os, json, difflib
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(__file__))

# 직전 debug_timing.py 가 저장한 단어 타임스탬프가 없으므로
# 디버깅 결과에서 수동으로 재현한 segments
SEGMENTS = [
    {"start": 0.36, "end": 2.60, "text": "운전하는 사람이면 끝까지 봐", "words": [
        {"word": "운전하는",  "start": 0.36, "end": 1.02, "confidence": 0.66},
        {"word": "사람이면",  "start": 1.02, "end": 1.62, "confidence": 0.96},
        {"word": "끝까지",   "start": 1.62, "end": 2.28, "confidence": 0.99},
        {"word": "봐",       "start": 2.28, "end": 2.60, "confidence": 0.93},
    ]},
    {"start": 2.68, "end": 7.41, "text": "이제 곧 장마철인데 비올때 운전하는 당신과 당신의 차의 안전의", "words": [
        {"word": "이제",     "start": 2.68, "end": 2.94, "confidence": 0.97},
        {"word": "곧",       "start": 2.94, "end": 3.14, "confidence": 0.99},
        {"word": "장마철인데","start": 3.14, "end": 3.86, "confidence": 0.79},
        {"word": "비올때",   "start": 3.86, "end": 4.36, "confidence": 0.61},
        {"word": "운전하는",  "start": 4.36, "end": 5.14, "confidence": 0.95},
        {"word": "당신과",   "start": 5.14, "end": 5.84, "confidence": 0.92},
        {"word": "당신의",   "start": 5.84, "end": 6.52, "confidence": 0.94},
        {"word": "차의",     "start": 6.52, "end": 6.82, "confidence": 0.43},
        {"word": "안전의",   "start": 6.82, "end": 7.41, "confidence": 0.64},
    ]},
    {"start": 7.41, "end": 10.52, "text": "보탬이 될 제품을 소개한다 바로 사이드 미러", "words": [
        {"word": "보탬이",   "start": 7.41, "end": 8.04, "confidence": 0.51},
        {"word": "될",       "start": 8.04, "end": 8.18, "confidence": 0.97},
        {"word": "제품을",   "start": 8.18, "end": 8.74, "confidence": 0.99},
        {"word": "소개한다",  "start": 8.74, "end": 9.24, "confidence": 0.97},
        {"word": "바로",     "start": 9.24, "end": 9.60, "confidence": 0.88},
        {"word": "사이드",   "start": 9.60, "end": 10.14,"confidence": 0.70},
        {"word": "미러",     "start": 10.14,"end": 10.52,"confidence": 0.93},
    ]},
    {"start": 10.56, "end": 13.68, "text": "다시 빌런 빗방울 때문에 사이드 미러가 안 보여", "words": [
        {"word": "다시",     "start": 10.56,"end": 10.88,"confidence": 0.28},
        {"word": "빌런",     "start": 10.88,"end": 11.32,"confidence": 0.35},
        {"word": "빗방울",   "start": 11.32,"end": 11.90,"confidence": 0.83},
        {"word": "때문에",   "start": 11.90,"end": 12.26,"confidence": 0.96},
        {"word": "사이드",   "start": 12.26,"end": 12.66,"confidence": 0.98},
        {"word": "미러가",   "start": 12.66,"end": 13.24,"confidence": 0.99},
        {"word": "안",       "start": 13.24,"end": 13.50,"confidence": 0.49},
        {"word": "보여",     "start": 13.50,"end": 13.68,"confidence": 0.97},
    ]},
    {"start": 13.74, "end": 18.74, "text": "사고 나서 수리비 내는 것 보단 저렴한 가격으로 당신의 안전을 지켜라", "words": [
        {"word": "사고",     "start": 13.74,"end": 14.04,"confidence": 1.00},
        {"word": "나서",     "start": 14.04,"end": 14.32,"confidence": 0.71},
        {"word": "수리비",   "start": 14.32,"end": 14.90,"confidence": 0.83},
        {"word": "내는",     "start": 14.90,"end": 15.24,"confidence": 1.00},
        {"word": "것",       "start": 15.24,"end": 15.42,"confidence": 0.44},
        {"word": "보단",     "start": 15.42,"end": 15.66,"confidence": 0.53},
        {"word": "저렴한",   "start": 15.66,"end": 16.32,"confidence": 0.99},
        {"word": "가격으로",  "start": 16.32,"end": 16.94,"confidence": 0.99},
        {"word": "당신의",   "start": 16.94,"end": 17.72,"confidence": 0.97},
        {"word": "안전을",   "start": 17.72,"end": 18.22,"confidence": 0.99},
        {"word": "지켜라",   "start": 18.22,"end": 18.74,"confidence": 1.00},
    ]},
    {"start": 18.84, "end": 21.24, "text": "구매링크는 프로필에 가면 있다", "words": [
        {"word": "구매링크는", "start": 18.84,"end": 19.66,"confidence": 0.80},
        {"word": "프로필에",  "start": 19.66,"end": 20.42,"confidence": 0.97},
        {"word": "가면",     "start": 20.42,"end": 20.82,"confidence": 0.98},
        {"word": "있다",     "start": 20.82,"end": 21.24,"confidence": 0.69},
    ]},
]
AUDIO_DURATION = 22.10

# 테스트 케이스: (구절, 예상 시작시간 근처)
TEST_CASES = [
    (["운전하는사람이면", "끝까지봐", "비올때", "사이드미러에"],
     [0.36, 1.62, 3.86, 9.60]),
    # 조사 없는 버전
    (["운전하는사람이면", "끝까지봐", "비올때", "사이드미러"],
     [0.36, 1.62, 3.86, 9.60]),
    # 다양한 조사 테스트
    (["사이드미러가", "빗방울때문에", "안전을지켜라"],
     [9.60, 11.32, 17.72]),
]

from src.core.video_maker import (
    map_phrases_to_timings, _correct_timing_drift, _particle_variants, _decompose_jamo
)

# _particle_variants 동작 확인
print("[_particle_variants 동작 확인]")
for phrase in ["사이드미러에", "운전하는사람이면", "끝까지봐", "비올때", "사이드미러가"]:
    variants = _particle_variants(phrase)
    print(f"  '{phrase}' -> {[v[0] for v in variants]}")

print()
segs = _correct_timing_drift(SEGMENTS, AUDIO_DURATION)

for i, (phrases, expected) in enumerate(TEST_CASES):
    print(f"[케이스 {i+1}] {phrases}")
    entries = map_phrases_to_timings(phrases, segs, AUDIO_DURATION)
    ok = True
    for j, (start, end, text) in enumerate(entries):
        exp = expected[j] if j < len(expected) else None
        diff = abs(start - exp) if exp is not None else None
        flag = ""
        if diff is not None and diff > 0.5:
            flag = "  <-- DRIFT!"
            ok = False
        print(f"  '{text}': {start:.2f}s  (예상 {exp}s, diff={diff:.2f}s){flag}")
    print(f"  => {'OK' if ok else 'DRIFT 있음'}")
    print()
