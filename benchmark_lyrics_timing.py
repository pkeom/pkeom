"""
노래 가사 타이밍 라이브러리 종합 벤치마크

테스트 라이브러리:
  1. faster-whisper base  (현재 방식)
  2. faster-whisper medium
  3. stable-ts base       (VAD + 오디오 에너지 정렬)
  4. stable-ts medium
  5. whisper-timestamped base  (DTW Cross-Attention)
  6. whisper-timestamped medium

오차 측정:
  - librosa onset detection → Reference 타임스탬프
  - 각 라이브러리 word timestamp vs onset 편차 측정
  - 100개 구절 매핑 시나리오 단조성/균일도 검증

실행: python benchmark_lyrics_timing.py <mp3_path>
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# Windows CP949 출력 인코딩 문제 방지
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PYTHON = sys.executable

MP3_PATH = (
    sys.argv[1]
    if len(sys.argv) > 1
    else str(next(Path("data/video_uploads").glob("**/*.mp3"), None) or "")
)

if not MP3_PATH or not Path(MP3_PATH).exists():
    print("[ERROR] MP3 파일을 찾을 수 없습니다.")
    print("  사용법: python benchmark_lyrics_timing.py <mp3_path>")
    sys.exit(1)

print(f"\n{'='*62}")
print(f"벤치마크 대상: {Path(MP3_PATH).name}")
print(f"{'='*62}\n")


# ── ffmpeg 경로 ─────────────────────────────────────────────────────

def _find_ffmpeg() -> str:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    username = os.getenv("USERNAME", os.getenv("USER", ""))
    for path in [
        r"C:\Program Files (x86)\HitPaw\HitPaw VoicePea\ffmpeg.exe",
        r"C:\Program Files\Wondershare\Wondershare UniConverter for Windows\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        rf"C:\Users\{username}\ffmpeg\bin\ffmpeg.exe",
    ]:
        if os.path.exists(path):
            bin_dir = str(Path(path).parent)
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return path
    raise RuntimeError("ffmpeg를 찾을 수 없습니다")


# ── 공통 유틸 ──────────────────────────────────────────────────────

def safe_copy_mp3(mp3_path: str) -> tuple[str, str]:
    """한글 경로 MP3 → ASCII 임시 경로로 복사. (tmp_dir, safe_mp3) 반환."""
    tmp_dir = tempfile.mkdtemp(prefix="bench_")
    safe_mp3 = os.path.join(tmp_dir, f"audio_{uuid.uuid4().hex[:8]}.mp3")
    shutil.copy2(mp3_path, safe_mp3)
    return tmp_dir, safe_mp3


def mp3_to_wav(mp3_path: str, out_dir: str, sr: int = 16000) -> str:
    """MP3 → 16kHz mono WAV 변환."""
    wav_path = os.path.join(out_dir, f"audio_{uuid.uuid4().hex[:8]}.wav")
    subprocess.run(
        [_find_ffmpeg(), "-y", "-i", mp3_path,
         "-ar", str(sr), "-ac", "1", wav_path],
        capture_output=True, check=True,
    )
    return wav_path


def get_duration_from_wav(wav_path: str) -> float:
    import soundfile as sf
    info = sf.info(wav_path)
    return info.frames / info.samplerate


def onset_reference(wav_path: str) -> list[float]:
    """librosa onset detection으로 Reference 타임스탬프 추출."""
    import librosa
    y, sr = librosa.load(wav_path, sr=16000, mono=True)
    onset_times = librosa.onset.onset_detect(
        y=y, sr=sr, units="time",
        backtrack=True,
        delta=0.07,
        pre_max=1, post_max=1, pre_avg=3, post_avg=3,
        wait=10,
    )
    # 0.15초 이내 중복 onset 병합
    merged: list[float] = []
    for t in onset_times:
        if not merged or t - merged[-1] > 0.15:
            merged.append(float(t))
    return merged


def timing_stats(word_times: list[float], onsets: list[float]) -> dict:
    """단어 타임스탬프 vs onset 편차 통계."""
    if not word_times or not onsets:
        return {"mean": 999, "median": 999, "std": 999, "within_03": 0.0, "n": 0}
    import statistics
    errors = [min(abs(t - o) for o in onsets) for t in word_times]
    within_03 = sum(1 for e in errors if e <= 0.3) / len(errors) * 100
    return {
        "mean":      round(statistics.mean(errors), 4),
        "median":    round(statistics.median(errors), 4),
        "std":       round(statistics.stdev(errors) if len(errors) > 1 else 0.0, 4),
        "within_03": round(within_03, 1),
        "n":         len(errors),
    }


def phrase_scenarios(word_times: list[float], audio_duration: float) -> dict:
    """100개 구절 매핑 시나리오: 단조 증가율 + 간격 균일도."""
    import statistics, random
    random.seed(42)
    results: list[dict] = []
    n_w = len(word_times)
    for _ in range(100):
        n = random.randint(3, min(12, max(3, n_w)))
        if n_w >= n:
            starts = [word_times[int(i * n_w / n)] for i in range(n)]
        else:
            starts = [audio_duration * i / n for i in range(n)]
        monotone = all(starts[i] < starts[i + 1] for i in range(len(starts) - 1))
        gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
        cv = (statistics.stdev(gaps) / statistics.mean(gaps)
              if len(gaps) > 1 and statistics.mean(gaps) > 0 else 0.0)
        results.append({"monotone": monotone, "cv": cv})
    mono_rate = sum(1 for r in results if r["monotone"]) / len(results) * 100
    avg_cv    = statistics.mean(r["cv"] for r in results)
    return {"monotone_%": round(mono_rate, 1), "gap_cv": round(avg_cv, 4)}


# ── 라이브러리별 전사 함수 ──────────────────────────────────────────

def run_faster_whisper(wav_path: str, model_size: str = "base") -> list[float]:
    from faster_whisper import WhisperModel
    model = WhisperModel(model_size, device="cpu", compute_type="float32")
    segs, _ = model.transcribe(
        wav_path, language="ko",
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    return [float(w.start) for seg in segs for w in (seg.words or [])]


def run_stable_ts(wav_path: str, model_size: str = "base") -> list[float]:
    import stable_whisper
    model = stable_whisper.load_model(model_size)
    result = model.transcribe(
        wav_path, language="ko",
        word_timestamps=True,
        vad=True,
        regroup=True,
    )
    return [float(w.start) for seg in result.segments for w in (seg.words or [])]


def run_whisper_timestamped(wav_path: str, model_size: str = "base") -> list[float]:
    import whisper_timestamped as wt
    import soundfile as sf, numpy as np, math
    data, sr = sf.read(wav_path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16000:
        indices = np.linspace(0, len(data) - 1, math.ceil(len(data) * 16000 / sr))
        data = np.interp(indices, np.arange(len(data)), data)
    model = wt.load_model(model_size, device="cpu")
    result = wt.transcribe(model, data.astype(np.float32), language="ko", detect_disfluencies=False)
    return [
        float(w.get("start", seg["start"]))
        for seg in result.get("segments", [])
        for w in seg.get("words", [])
    ]


# ── 벤치마크 실행 ──────────────────────────────────────────────────

METHODS = [
    ("faster-whisper-base   (현재)",  run_faster_whisper,        {"model_size": "base"}),
    ("faster-whisper-medium",         run_faster_whisper,        {"model_size": "medium"}),
    ("stable-ts-base        (VAD)",   run_stable_ts,             {"model_size": "base"}),
    ("stable-ts-medium      (VAD)",   run_stable_ts,             {"model_size": "medium"}),
    ("whisper-timestamped-base",      run_whisper_timestamped,   {"model_size": "base"}),
    ("whisper-timestamped-medium",    run_whisper_timestamped,   {"model_size": "medium"}),
]


def run_benchmark() -> list[dict]:
    # 1. 임시 ASCII 경로로 MP3 복사
    print("▶ 오디오 전처리 중...")
    tmp_dir, safe_mp3 = safe_copy_mp3(MP3_PATH)
    try:
        wav_path = mp3_to_wav(safe_mp3, tmp_dir)
        audio_duration = get_duration_from_wav(wav_path)
        print(f"  오디오 길이: {audio_duration:.1f}초")

        # 2. Onset Reference 추출
        print("\n▶ Onset Detection (Reference) 추출 중...")
        t0 = time.perf_counter()
        onsets = onset_reference(wav_path)
        print(f"  Onset 수: {len(onsets)}개  ({time.perf_counter()-t0:.2f}s)")

        results: list[dict] = []

        # 3. 각 라이브러리 테스트
        for name, fn, kwargs in METHODS:
            print(f"\n▶ [{name}] 실행 중...")
            try:
                t0 = time.perf_counter()
                word_times = fn(wav_path, **kwargs)
                elapsed = time.perf_counter() - t0

                stats  = timing_stats(word_times, onsets)
                phrase = phrase_scenarios(word_times, audio_duration)

                result = {
                    "name":           name,
                    "elapsed_s":      round(elapsed, 1),
                    "words":          stats["n"],
                    "mean_err":       stats["mean"],
                    "median_err":     stats["median"],
                    "std_err":        stats["std"],
                    "within_03_pct":  stats["within_03"],
                    "monotone_pct":   phrase["monotone_%"],
                    "gap_cv":         phrase["gap_cv"],
                    "status":         "OK",
                }
                print(
                    f"  ✓ {elapsed:.0f}s | 단어: {stats['n']} | "
                    f"평균오차: {stats['mean']:.3f}s | "
                    f"0.3s이내: {stats['within_03']:.0f}% | "
                    f"단조: {phrase['monotone_%']:.0f}%"
                )
            except Exception as e:
                print(f"  ✗ 오류: {e}")
                result = {"name": name, "status": f"ERROR: {e}",
                          "elapsed_s": 0, "words": 0,
                          "mean_err": 999, "median_err": 999, "std_err": 999,
                          "within_03_pct": 0, "monotone_pct": 0, "gap_cv": 999}

            results.append(result)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── 결과 테이블 ────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print("📊 벤치마크 결과 (librosa Onset 기준 타이밍 오차)")
    print(f"{'='*72}")
    hdr = f"  {'라이브러리':<34} {'시간':>5} {'단어':>5} {'평균오차':>8} {'중앙값':>7} {'Std':>6} {'0.3s내':>7} {'단조%':>6}"
    print(hdr)
    print("-" * 72)

    valid = [r for r in results if r["status"] == "OK"]
    for r in results:
        if r["status"] != "OK":
            print(f"  {r['name']:<34} ERROR")
            continue
        mark = " ◀ 현재" if "현재" in r["name"] else ""
        print(
            f"  {r['name']:<34} "
            f"{r['elapsed_s']:>4.0f}s "
            f"{r['words']:>5} "
            f"{r['mean_err']:>7.3f}s "
            f"{r['median_err']:>6.3f}s "
            f"{r['std_err']:>5.3f}s "
            f"{r['within_03_pct']:>6.1f}% "
            f"{r['monotone_pct']:>5.1f}%"
            f"{mark}"
        )

    print(f"\n{'='*72}")
    if valid:
        best = min(valid, key=lambda r: r["mean_err"])
        print(f"\n🏆 최고 정확도: {best['name']}")
        print(f"   평균오차 {best['mean_err']:.3f}s | 0.3s 이내 {best['within_03_pct']:.0f}% | 단조 {best['monotone_pct']:.0f}%")

        out_path = "data/benchmark_results.json"
        Path("data").mkdir(exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"audio": str(Path(MP3_PATH).name), "results": results}, f,
                      ensure_ascii=False, indent=2)
        print(f"\n결과 저장: {out_path}")

    return results


if __name__ == "__main__":
    run_benchmark()
