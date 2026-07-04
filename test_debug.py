"""수정 후 검증 - 동일 구절+동일 segments로 before/after 비교"""
import sys, os, glob, difflib, re, json, logging
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from core.video_maker import transcribe_audio, map_phrases_to_timings

def normk(t):
    return re.sub(r"[\s\W]+","",t,flags=re.UNICODE).lower()

all_mp3 = sorted(glob.glob("data/video_uploads/**/*.mp3", recursive=True))
target = next((f for f in all_mp3 if "20-23-27" in f), all_mp3[-1])

# 캐시된 words 파일 사용 (다시 전사하지 않음)
with open("debug_words.json", encoding="utf-8") as f:
    wdata = json.load(f)

words_raw = wdata["words"]
print(f"[Words] {len(words_raw)}개 (cached)")
print("단어 목록:")
for w in words_raw:
    print(f"  [{w['i']:02d}] {w['t']:6.3f}s  '{w['raw']}'  (norm: '{w['n']}')")

# segments 재구성 (cached words로)
# _text_match_starts가 segments에서 words를 추출하므로 segment 구조 필요
# 모든 단어를 하나의 segment로 묶어서 전달
fake_segs = [{
    "start": 0.0,
    "end": wdata["words"][-1]["t"] + 1.0,
    "text": " ".join(w["raw"] for w in wdata["words"]),
    "words": [{"word": w["raw"], "start": w["t"], "end": w["t"]+0.5} for w in wdata["words"]]
}]

dur = wdata["words"][-1]["t"] + 2.0

# 테스트 구절들
test_cases = [
    (["운전하는사람이면","끝까지봐","이제곧장마철인데","비올때","사이드미러에","방수필름붙이면"],
     "사용자구절-1"),
    (["사이드미러방수필름","이거하나면충분","붙이기도쉽고","효과도좋아요"],
     "사용자구절-2"),
]

# Ground truth: 직접 words에서 최고점 위치
def find_gt(phrase, all_words):
    np = normk(phrase)
    wn = [(w["t"], w["n"]) for w in all_words]
    best_sc, best_t = -1.0, 0.0
    for j in range(len(wn)):
        for win in range(1, min(7, len(wn)-j+1)):
            wt = "".join(wn[j+k][1] for k in range(win))
            sc = difflib.SequenceMatcher(None, np, wt).ratio()
            lr = len(wt) / max(1, len(np))
            if lr < 0.6:
                sc *= lr
            if sc > best_sc:
                best_sc, best_t = sc, wn[j][0]
    return best_t, best_sc

print()
all_ok = True
for phrases, label in test_cases:
    entries = map_phrases_to_timings(phrases, fake_segs, dur)
    print(f"[{label}]")
    errors = []
    for phrase, (s, e, _) in zip(phrases, entries):
        gt, sc = find_gt(phrase, words_raw)
        err = abs(s - gt)
        errors.append(err)
        flag = "FAIL" if err > 0.3 else "ok"
        print(f"  {phrase:<20} -> {s:6.3f}s  (gt={gt:.3f}s  err={err:.3f}  {flag})")
    avg = sum(errors)/len(errors)
    mx = max(errors)
    n_ok = sum(1 for e in errors if e<=0.3)
    print(f"  avg={avg:.3f}s  max={mx:.3f}s  ok={n_ok}/{len(errors)}")
    if mx > 0.3:
        all_ok = False
    print()

if all_ok:
    print("[PASS] 모든 구절 0.3s 이내")
else:
    print("[일부 FAIL] 추가 분석 필요")
