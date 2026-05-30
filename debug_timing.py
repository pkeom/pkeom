"""타이밍 디버깅: Demucs 보컬 분리 -> wav2vec2 CTC 강제 정렬 -> 구절 매핑 측정"""
# -*- coding: utf-8 -*-
import sys, os, time, shutil, glob, math, traceback
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(__file__))

mp3_files = sorted(
    glob.glob("data/video_uploads/**/*.mp3", recursive=True),
    key=os.path.getmtime, reverse=True,
)
if not mp3_files:
    print("MP3 파일 없음"); sys.exit(1)

AUDIO = mp3_files[0]
print(f"[대상 MP3] {AUDIO}")
print(f"[파일크기] {os.path.getsize(AUDIO)/1024:.1f} KB")
print("=" * 60)

TEST_PHRASES = ["운전하는사람이면", "끝까지봐", "비올때", "사이드미러에"]

# ── 1. Demucs 보컬 분리 ──────────────────────────────────────
print("\n[1] Demucs 보컬 분리 시작...")
t0 = time.time()
vocals_path = None
audio_duration = None

try:
    from src.core.video_maker import _separate_vocals
    vocals_path = _separate_vocals(AUDIO)
    elapsed = time.time() - t0
    vocals_size = os.path.getsize(vocals_path)
    print(f"  OK ({elapsed:.1f}초)")
    print(f"  보컬 파일: {vocals_path}")
    print(f"  보컬 크기: {vocals_size/1024:.1f} KB")
    print(f"  원본 크기: {os.path.getsize(AUDIO)/1024:.1f} KB")
    if vocals_size < 1000:
        print("  WARNING: 보컬 파일이 너무 작음 -- 분리 실패 가능성")
except Exception as e:
    print(f"  FAIL: {e}")
    traceback.print_exc()
    sys.exit(1)

print("=" * 60)

# ── 2. wav2vec2 CTC 강제 정렬 ────────────────────────────────
print("\n[2] wav2vec2 CTC 강제 정렬 시작...")
print(f"  모델: kresnik/wav2vec2-large-xlsr-korean")
print(f"  가사: {TEST_PHRASES}")
t0 = time.time()
segments = []
all_words = []

try:
    import soundfile as sf
    import numpy as np
    from src.core.video_maker import _forced_align_wav2vec2

    data, sr = sf.read(vocals_path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    audio_duration = len(data) / sr
    print(f"  보컬 오디오 길이: {audio_duration:.2f}초")

    if sr != 16000:
        new_len = math.ceil(len(data) * 16000 / sr)
        indices = np.linspace(0, len(data) - 1, new_len)
        data = np.interp(indices, np.arange(len(data)), data)
    audio_np = data.astype(np.float32)

    segments = _forced_align_wav2vec2(
        audio_np=audio_np,
        audio_duration=audio_duration,
        phrases=TEST_PHRASES,
    )
    elapsed = time.time() - t0
    print(f"  OK ({elapsed:.1f}초) / 세그먼트 수: {len(segments)}")

    print("\n  [강제 정렬 결과 -- 전체]")
    for i, seg in enumerate(segments):
        seg_start = float(seg.get("start", 0))
        seg_end   = float(seg.get("end", 0))
        seg_text  = seg.get("text", "").strip()
        print(f"  seg[{i:02d}] {seg_start:6.2f}s ~ {seg_end:6.2f}s  |  {seg_text}")
        for w in seg.get("words", []):
            ws      = float(w.get("start", seg_start))
            we      = float(w.get("end",   seg_start))
            wscore  = float(w.get("score", 0.0))
            wt_text = w.get("word", "")
            print(f"    phrase: [{ws:6.2f}~{we:6.2f}] score={wscore:.2f}  '{wt_text}'")
            all_words.append((ws, we, wt_text, wscore))

except Exception as e:
    print(f"  FAIL: {e}")
    traceback.print_exc()
    sys.exit(1)

print("=" * 60)

# ── 3. 타이밍 오차 분석 ──────────────────────────────────────
print("\n[3] 타이밍 오차 분석...")

if all_words and audio_duration:
    print(f"\n  구절별 정렬 결과:")
    for ws, we, word, score in all_words:
        pct = ws / audio_duration * 100
        dur = we - ws
        print(f"    '{word}': {ws:.3f}s ~ {we:.3f}s  (길이 {dur:.3f}s, 위치 {pct:.1f}%, score={score:.2f})")

    starts = [w[0] for w in all_words]
    if len(starts) >= 2:
        intervals = [starts[i+1] - starts[i] for i in range(len(starts)-1)]
        expected  = audio_duration / len(starts)
        max_drift = max(abs(v - expected) for v in intervals)
        print(f"\n  예상 균등 간격: {expected:.3f}초")
        print(f"  실제 간격: {[f'{v:.3f}s' for v in intervals]}")
        print(f"  최대 drift: {max_drift:.3f}초 ({max_drift/expected*100:.1f}%)")

    # score 분포
    scores = [w[3] for w in all_words]
    if scores:
        print(f"\n  score 분포:")
        print(f"    최솟값: {min(scores):.3f}")
        print(f"    평균: {sum(scores)/len(scores):.3f}")
        print(f"    최댓값: {max(scores):.3f}")

print("=" * 60)

# ── 4. 구절 매핑 테스트 ──────────────────────────────────────
print("\n[4] 구절->타이밍 매핑 테스트 (map_phrases_to_timings)...")

try:
    from src.core.video_maker import map_phrases_to_timings

    entries = map_phrases_to_timings(TEST_PHRASES, segments, audio_duration)

    print(f"  전체 오디오 길이: {audio_duration:.2f}초")
    print()
    for start, end, text in entries:
        pct = start / audio_duration * 100
        dur = end - start
        print(f"  '{text}': {start:.3f}s ~ {end:.3f}s  (길이 {dur:.3f}s, 위치 {pct:.1f}%)")

    starts = [e[0] for e in entries]
    if len(starts) >= 2:
        expected_interval = audio_duration / len(starts)
        actual_intervals  = [starts[i+1]-starts[i] for i in range(len(starts)-1)]
        max_drift = max(abs(v - expected_interval) for v in actual_intervals)
        print()
        print(f"  예상 균등 간격: {expected_interval:.3f}초")
        print(f"  실제 간격: {[f'{v:.3f}s' for v in actual_intervals]}")
        print(f"  최대 drift: {max_drift:.3f}초 ({max_drift/expected_interval*100:.1f}%)")

except Exception as e:
    print(f"  FAIL: {e}")
    traceback.print_exc()

finally:
    if vocals_path:
        tmp_dir = os.path.dirname(vocals_path)
        if os.path.basename(tmp_dir).startswith("vcl_"):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            print(f"\n  [정리] 임시 디렉토리 삭제: {tmp_dir}")

print("\n" + "=" * 60)
print("디버깅 완료")
