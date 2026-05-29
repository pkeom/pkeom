"""실제 MP3 파일로 전체 파이프라인 타이밍 상세 테스트.

실행: python -X utf8 test_timing_detail.py
"""
from __future__ import annotations
import io, sys, os, shutil, subprocess, tempfile, uuid, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

sys.path.insert(0, ".")
from pathlib import Path

# ── 테스트 대상 파일 ───────────────────────────────────────────────
MP3_FILES = [
    r"data\video_uploads\eadec3e2\사이드미러방수필름 끝까지.mp3",
    r"data\video_uploads\00f260e9\2026-05-28 20-23-27.mp3",
    r"data\video_uploads\73b05212\2026-05-28 20-23-27.mp3",
]
MP3_FILES = [p for p in MP3_FILES if Path(p).exists()]

# ── ffmpeg 경로 ────────────────────────────────────────────────────
def find_ffmpeg():
    import shutil as sh
    if sh.which("ffmpeg"):
        return "ffmpeg"
    username = os.getenv("USERNAME", "")
    for p in [
        r"C:\Program Files (x86)\HitPaw\HitPaw VoicePea\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        rf"C:\Users\{username}\ffmpeg\bin\ffmpeg.exe",
    ]:
        if os.path.exists(p):
            bdir = str(Path(p).parent)
            if bdir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bdir + os.pathsep + os.environ.get("PATH", "")
            return p
    raise RuntimeError("ffmpeg 없음")

FFMPEG = find_ffmpeg()

def safe_wav(mp3_path: str, tmp_dir: str, sr: int = 16000) -> str:
    """한글 경로 MP3 → ASCII 임시 WAV."""
    safe_mp3 = os.path.join(tmp_dir, f"in_{uuid.uuid4().hex[:6]}.mp3")
    wav_out  = os.path.join(tmp_dir, f"out_{uuid.uuid4().hex[:6]}.wav")
    shutil.copy2(mp3_path, safe_mp3)
    subprocess.run([FFMPEG, "-y", "-i", safe_mp3, "-ar", str(sr), "-ac", "1", wav_out],
                   capture_output=True, check=True)
    os.unlink(safe_mp3)
    return wav_out

# ── Step1: Demucs 보컬 분리 ────────────────────────────────────────
def separate_vocals(mp3_path: str, tmp_dir: str) -> str:
    import torch, numpy as np, soundfile as sf
    from demucs.pretrained import get_model
    from demucs.apply import apply_model

    model = get_model("htdemucs")
    model.eval()

    wav_in = safe_wav(mp3_path, tmp_dir, sr=model.samplerate)
    data, sr = sf.read(wav_in, dtype="float32")
    wav = torch.from_numpy(data.T if data.ndim > 1 else data[None])
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)
    ref = wav.mean(0)
    wav_norm = (wav - ref.mean()) / ref.std()

    with torch.no_grad():
        sources = apply_model(model, wav_norm.unsqueeze(0), device="cpu", progress=False)

    stems = list(model.sources)
    vi = stems.index("vocals")
    vocals = sources[0, vi] * ref.std() + ref.mean()
    vocals_np = vocals.cpu().numpy().T
    vocals_wav = os.path.join(tmp_dir, f"vocals_{uuid.uuid4().hex[:6]}.wav")
    sf.write(vocals_wav, vocals_np, model.samplerate)
    return vocals_wav

# ── Step2: whisper-timestamped medium + 강화된 hallucination 필터 ──
CONF_MIN    = 0.20   # 강화: 이전 0.15 → 0.20
TAIL_MARGIN = 0.3    # 오디오 끝 0.3초 이내 시작 단어도 제거

def transcribe_wt(wav_path: str) -> list[dict]:
    import whisper_timestamped as wt
    import soundfile as sf, numpy as np, math

    data, sr = sf.read(wav_path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    audio_duration = len(data) / sr
    cut_at = audio_duration - TAIL_MARGIN   # 강화: 끝 0.3초 전부터 차단
    if sr != 16000:
        indices = np.linspace(0, len(data) - 1, math.ceil(len(data) * 16000 / sr))
        data = np.interp(indices, np.arange(len(data)), data)
    audio_np = data.astype(np.float32)

    model = wt.load_model("medium", device="cpu")
    result = wt.transcribe(model, audio_np, language="ko", detect_disfluencies=False)

    segments = []
    filtered_count = 0
    for seg in result.get("segments", []):
        seg_start = float(seg["start"])
        if seg_start >= cut_at:              # 강화: 끝 tail 구간 세그먼트 제거
            filtered_count += len(seg.get("words", []))
            continue
        words = []
        for w in seg.get("words", []):
            w_start = float(w.get("start", seg_start))
            w_conf  = float(w.get("confidence", 1.0))
            if w_start >= cut_at:            # 강화: tail 구간 단어 제거
                filtered_count += 1
                continue
            if w_conf < CONF_MIN:            # 강화: 신뢰도 0.20 미달 제거
                filtered_count += 1
                continue
            words.append({
                "word":  w.get("text", ""),
                "start": w_start,
                "end":   min(float(w.get("end", seg_start)), audio_duration),
                "conf":  round(w_conf, 3),
            })
        if words:
            segments.append({
                "start": seg_start,
                "end":   min(float(seg["end"]), audio_duration),
                "text":  seg.get("text","").strip(),
                "words": words,
            })
    if filtered_count:
        print(f"  [필터] hallucination/low-conf/tail 단어 {filtered_count}개 제거됨")
    return segments

# ── Step3: onset detection ─────────────────────────────────────────
def get_onsets(wav_path: str) -> list[float]:
    import librosa, numpy as np
    y, sr = librosa.load(wav_path, sr=16000, mono=True)
    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time",
                                        backtrack=True, delta=0.07,
                                        pre_max=1, post_max=1, pre_avg=3, post_avg=3, wait=10)
    merged = []
    for t in onsets:
        if not merged or t - merged[-1] > 0.15:
            merged.append(float(t))
    return merged

# ── 출력 ──────────────────────────────────────────────────────────
def nearest(t, onsets):
    if not onsets: return 0.0
    return min(onsets, key=lambda o: abs(o - t))

def run_test(mp3_path: str):
    print(f"\n{'='*65}")
    print(f"파일: {Path(mp3_path).name}  ({Path(mp3_path).stat().st_size//1024} KB)")
    print(f"{'='*65}")

    tmp = tempfile.mkdtemp(prefix="wttest_")
    try:
        print("\n[1/3] Demucs 보컬 분리 중...")
        vocals_wav = separate_vocals(mp3_path, tmp)

        print("[2/3] whisper-timestamped 전사 중...")
        segments = transcribe_wt(vocals_wav)

        print("[3/3] Onset Reference 추출 중...")
        onsets = get_onsets(vocals_wav)

        # ── 전체 단어 출력 ──────────────────────────────────────────
        print(f"\n■ 전사 결과 ({sum(len(s['words']) for s in segments)}개 단어, {len(segments)}개 세그먼트)\n")
        print(f"  {'#':>3}  {'start':>6}  {'end':>6}  {'오차':>6}  {'conf':>5}  단어")
        print(f"  {'-'*55}")

        all_words = []
        for seg in segments:
            for w in seg["words"]:
                all_words.append(w)

        problem_words = []
        for i, w in enumerate(all_words):
            nearest_onset = nearest(w["start"], onsets)
            err = abs(w["start"] - nearest_onset)
            flag = "  ← 오차큼!" if err > 0.4 else ""
            if err > 0.4:
                problem_words.append((i, w, err, nearest_onset))
            print(f"  {i:>3}  {w['start']:>6.2f}  {w['end']:>6.2f}  {err:>6.3f}s  {w['conf']:>5.3f}  {w['word']}{flag}")

        # ── 세그먼트별 전사 텍스트 ──────────────────────────────────
        print(f"\n■ 세그먼트 전체 텍스트\n")
        for i, seg in enumerate(segments):
            print(f"  [{i:>2}] {seg['start']:5.2f}s ~ {seg['end']:5.2f}s  |  {seg['text']}")

        # ── 오차 큰 구간 분석 ───────────────────────────────────────
        if problem_words:
            print(f"\n■ 오차 0.4s 초과 단어 ({len(problem_words)}개)\n")
            for i, w, err, ref_t in problem_words:
                print(f"  [{i:>2}] '{w['word']}'  →  Whisper: {w['start']:.2f}s, onset: {ref_t:.2f}s, 오차: {err:.3f}s")
        else:
            print(f"\n■ 오차 0.4s 초과 단어: 없음")

        # ── 통계 ────────────────────────────────────────────────────
        if all_words and onsets:
            import statistics
            errs = [abs(w["start"] - nearest(w["start"], onsets)) for w in all_words]
            within03 = sum(1 for e in errs if e <= 0.3) / len(errs) * 100
            print(f"\n■ 통계")
            print(f"  단어 수: {len(all_words)}")
            print(f"  onset 수: {len(onsets)}")
            print(f"  평균 오차: {statistics.mean(errs):.3f}s")
            print(f"  중앙값 오차: {statistics.median(errs):.3f}s")
            print(f"  최대 오차: {max(errs):.3f}s")
            print(f"  0.3s 이내: {within03:.1f}%")

            # 오차 분포
            buckets = [(0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.5), (0.5, 1.0), (1.0, 99)]
            print(f"\n  오차 분포:")
            for lo, hi in buckets:
                n = sum(1 for e in errs if lo <= e < hi)
                bar = "█" * n
                print(f"    {lo:.1f}~{hi:.1f}s : {n:>3}개  {bar}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if not MP3_FILES:
        print("MP3 파일이 없습니다.")
        sys.exit(1)
    for mp3 in MP3_FILES:
        run_test(mp3)
    print("\n완료.")
