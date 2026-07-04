"""타이밍 테스트 v3 - 사용자가 실제 입력할 법한 구절로 집중 테스트"""
import sys, os, glob, json, subprocess, re, difflib, logging

logging.basicConfig(level=logging.WARNING)  # suppress INFO noise
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from core.video_maker import transcribe_audio, map_phrases_to_timings, _find_ffmpeg

def get_dur(path):
    ff = _find_ffmpeg().replace("ffmpeg.exe","ffprobe.exe").replace("ffmpeg","ffprobe")
    r = subprocess.run([ff,"-v","quiet","-print_format","json","-show_format",path],
                       capture_output=True)
    return float(json.loads(r.stdout)["format"]["duration"])

def norm(t):
    return re.sub(r"[\s\W]+","",t,flags=re.UNICODE).lower()

# ── 파일 선택 ────────────────────────────────────────────────────────
all_mp3 = sorted(glob.glob("data/video_uploads/**/*.mp3", recursive=True))
target = next((f for f in all_mp3 if "20-23-27" in f), all_mp3[-1])
print(f"[파일] {target}")
dur = get_dur(target)
print(f"[길이] {dur:.2f}s\n")

# ── Whisper 전사 ──────────────────────────────────────────────────────
print("[전사 중... 약 30s]")
segs = transcribe_audio(target)
words = []
for seg in segs:
    for w in seg.get("words",[]):
        words.append({"t":float(w["start"]),"e":float(w["end"]),"w":w["word"].strip()})

print(f"segments={len(segs)}  words={len(words)}")
wtext_list = [w["w"] for w in words]
print("단어:", wtext_list)
print()

# ── 정확한 GT 계산 (짧은 구절에만 사용) ─────────────────────────────
def find_gt(phrase, max_win=6):
    """각 구절에 대해 Whisper 단어 시퀀스에서 best-match 위치 반환."""
    np = norm(phrase)
    wn = [(w["t"], norm(w["w"])) for w in words if norm(w["w"])]
    best_sc, best_t = -1.0, 0.0
    for j in range(len(wn)):
        for win in range(1, min(max_win+1, len(wn)-j+1)):
            wt = "".join(wn[j+k][1] for k in range(win))
            sc = difflib.SequenceMatcher(None, np, wt).ratio()
            if sc > best_sc:
                best_sc, best_t = sc, wn[j][0]
    return best_t, best_sc

# ── 테스트 함수 ────────────────────────────────────────────────────────
def run_test(phrases, label):
    entries = map_phrases_to_timings(phrases, segs, dur)
    print(f"\n[{label}]")
    print(f"  {'구절':<22} {'mapped':>8} {'gt':>8} {'err':>6}  {'score':>6}  판정")
    print(f"  {'-'*60}")
    errors = []
    for phrase, (s,e,_) in zip(phrases, entries):
        gt, sc = find_gt(phrase)
        err = abs(s - gt)
        errors.append(err)
        ok = "ok" if err<=0.3 else "FAIL"
        print(f"  {phrase[:20]:<22} {s:>8.3f} {gt:>8.3f} {err:>6.3f}  {sc:>6.3f}  {ok}")
    avg = sum(errors)/len(errors) if errors else 0
    mx = max(errors) if errors else 0
    n_ok = sum(1 for e in errors if e<=0.3)
    print(f"  avg={avg:.3f}s  max={mx:.3f}s  ok={n_ok}/{len(errors)}")
    return avg, mx

# ── 케이스1: Whisper 2단어씩 (완전일치) ──────────────────────────────
ph2 = ["".join(wtext_list[i:i+2]) for i in range(0, len(wtext_list)-1, 2)][:6]
run_test(ph2, "2단어그룹(완전일치)")

# ── 케이스2: Whisper 3단어씩 (완전일치) ──────────────────────────────
ph3 = ["".join(wtext_list[i:i+3]) for i in range(0, len(wtext_list)-2, 3)][:5]
run_test(ph3, "3단어그룹(완전일치)")

# ── 케이스3: 사용자 전형 구절 (사이드미러방수필름 제품 영상 추정) ─────
user_phrases_1 = [
    "운전하는사람이면",
    "끝까지봐",
    "이제곧장마철인데",
    "비올때",
    "사이드미러에",
    "방수필름붙이면",
]
run_test(user_phrases_1, "사용자구절-1(드라이빙+장마)")

user_phrases_2 = [
    "사이드미러방수필름",
    "이거하나면충분",
    "붙이기도쉽고",
    "효과도좋아요",
]
run_test(user_phrases_2, "사용자구절-2(제품특징)")

# ── 케이스4: 오타/변형 구절 ───────────────────────────────────────────
typo_phrases = [p[:-1] if len(p)>3 else p for p in ph2[:4]]  # 마지막 글자 제거
run_test(typo_phrases, "오타시뮬(마지막글자제거)")

# ── 케이스5: 구절 수가 단어 수보다 많을 때 ───────────────────────────
many_phrases = ["".join(wtext_list[i:i+1]) for i in range(0, min(12, len(wtext_list)), 1)][:10]
run_test(many_phrases, "구절수많음(1단어씩10개)")

# ── 케이스6: 첫 단어가 실제보다 늦게 시작하는 경우 ───────────────────
late_phrases = ["".join(wtext_list[5:7]), "".join(wtext_list[10:12]),
                "".join(wtext_list[15:17]), "".join(wtext_list[20:22])]
run_test(late_phrases, "중간시작구절(5번~)")

# ── 케이스7: 세그먼트 텍스트 그대로 (긴 구절) ───────────────────────
seg_phrases = [s["text"].strip().replace(" ","") for s in segs]
print(f"\n[세그먼트 텍스트 그대로(참고)] {seg_phrases}")
# GT 계산이 긴 구절에 맞지 않아 오차 측정은 생략, 매핑 결과만 출력
entries7 = map_phrases_to_timings(seg_phrases, segs, dur)
for s_t,e_t,txt in entries7:
    print(f"  {txt[:25]:<27} {s_t:.3f}s ~ {e_t:.3f}s")

print("\n\n[결론]")
print("위 테스트가 모두 ok면 알고리즘은 정상 — 실제 오류는 사용자 입력 구절 확인 필요")
print("실제 구절을 dashboard에서 입력 후 data/logs/app.log 확인")
