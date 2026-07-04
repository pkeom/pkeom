# -*- coding: utf-8 -*-
"""영상 자동 합성 (영상제작✨ 탭 백엔드)

파이프라인:
  1. edge-tts 로 대본 문장별 한국어 음성(mp3) 생성 + ffprobe 길이 측정
     (video_auto/tts_test.py 에서 검증된 SubMaker 방식 재사용)
  2. 문장별 음성을 이어붙여 전체 내레이션 오디오 생성
  3. 측정한 길이로 전체 타임라인 자막(combined.srt) 생성
  4. ffmpeg 로 각 영상의 지정 구간 컷 → 9:16(1080×1920) 정규화 → 이어붙이기
  5. 영상(부족하면 루프) + 내레이션 오디오 합성, 자막 번인, 오디오 길이에 맞춰 트림
  6. 최종 mp4 출력 (BGM 없음)

기존 src/core/video_maker.py (CapCut 방식) 와는 완전히 분리된 독립 모듈.
"""
import asyncio
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import edge_tts

# 9:16 세로 규격
TARGET_W = 1080
TARGET_H = 1920
TARGET_FPS = 30

# 자막 일관 스타일 (libass force_style)
SUBTITLE_STYLE = (
    "FontName=Malgun Gothic,FontSize=16,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=120"
)


# ── 실행 파일 탐색 (PATH + winget Packages) ───────────────────────
def _find_exe(name: str) -> str | None:
    """PATH 또는 winget 설치 경로에서 ffmpeg/ffprobe 를 찾는다."""
    p = shutil.which(name)
    if p:
        return p
    candidates = [
        os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\WinGet\Links\{name}.exe"),
    ]
    pkgroot = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(pkgroot):
        for root, _dirs, files in os.walk(pkgroot):
            if f"{name}.exe" in files:
                candidates.append(os.path.join(root, f"{name}.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _get_duration(ffprobe: str, path: str) -> float | None:
    """ffprobe 로 미디어 길이(초)를 측정한다. 실패 시 None."""
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def _run(cmd: list[str], cwd: str | None = None) -> None:
    """ffmpeg 명령 실행. 실패 시 stderr 를 담아 예외."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        tail = "\n".join((r.stderr or "").strip().splitlines()[-12:])
        raise RuntimeError(f"ffmpeg 실패 (code {r.returncode}):\n{tail}")


def _srt_ts(seconds: float) -> str:
    """초(float) -> SRT 타임스탬프 'HH:MM:SS,mmm'."""
    total_ms = int(round(timedelta(seconds=seconds).total_seconds() * 1000))
    h, rem = divmod(total_ms, 3600 * 1000)
    m, rem = divmod(rem, 60 * 1000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ── 1. TTS ────────────────────────────────────────────────────────
async def _synth_one(text: str, voice: str, mp3_path: str) -> None:
    """문장 1개 -> mp3 생성. 한국어 음성은 SentenceBoundary 도 보낸다."""
    communicate = edge_tts.Communicate(text, voice)
    with open(mp3_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])


async def _synth_all(sentences: list[str], voice: str, work: Path) -> list[str]:
    """문장 리스트 -> mp3 파일 경로 리스트."""
    paths = []
    for i, s in enumerate(sentences, 1):
        mp3 = work / f"line_{i:02d}.mp3"
        await _synth_one(s, voice, str(mp3))
        paths.append(str(mp3))
    return paths


# ── 4. 영상 클립 컷 + 9:16 정규화 ─────────────────────────────────
def _cut_clip(ffmpeg: str, src: str, start: float, end: float, out: str) -> None:
    """지정 구간을 잘라 1080×1920(9:16) 로 정규화한다 (가운데 크롭)."""
    dur = max(0.1, end - start)
    vf = (f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
          f"crop={TARGET_W}:{TARGET_H},setsar=1,fps={TARGET_FPS}")
    _run([
        ffmpeg, "-y",
        "-ss", f"{start}", "-i", src, "-t", f"{dur}",
        "-vf", vf, "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        out,
    ])


def _concat(ffmpeg: str, parts: list[str], out: str, work: Path) -> None:
    """동일 규격으로 정규화된 파일들을 concat demuxer 로 이어붙인다."""
    listfile = work / "concat.txt"
    listfile.write_text(
        "".join(f"file '{Path(p).name}'\n" for p in parts), encoding="utf-8")
    _run([ffmpeg, "-y", "-f", "concat", "-safe", "0",
          "-i", "concat.txt", "-c", "copy", out], cwd=str(work))


# ── 메인 ──────────────────────────────────────────────────────────
def generate_video_auto(
    clips: list[dict],
    script_text: str,
    voice: str,
    output_dir: str,
    output_filename: str,
    progress=None,
) -> str:
    """영상 자동 합성. 최종 mp4 절대경로를 반환.

    clips: [{"path": str, "start": float, "end": float}, ...] (사용 순서)
    script_text: 줄바꿈으로 구분된 대본
    voice: edge-tts 음성 (ko-KR-SunHiNeural / ko-KR-InJoonNeural)
    progress(msg): 진행 상태 콜백 (선택)
    """
    def _p(msg: str) -> None:
        if progress:
            progress(msg)

    ffmpeg = _find_exe("ffmpeg")
    ffprobe = _find_exe("ffprobe")
    if not ffmpeg:
        raise RuntimeError("ffmpeg 를 찾을 수 없습니다 (winget Gyan.FFmpeg 설치 필요)")

    sentences = [ln.strip() for ln in script_text.splitlines() if ln.strip()]
    if not sentences:
        raise RuntimeError("대본이 비어 있습니다")
    if not clips:
        raise RuntimeError("영상 클립이 없습니다")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / (Path(output_filename).stem + "_work")
    work.mkdir(parents=True, exist_ok=True)

    # 1. TTS
    _p(f"TTS 음성 생성 중… (문장 {len(sentences)}개)")
    mp3s = asyncio.run(_synth_all(sentences, voice, work))

    # 길이 측정 → 자막 타임라인용
    durations = []
    for m in mp3s:
        d = _get_duration(ffprobe, m) if ffprobe else None
        durations.append(d if d else 2.5)   # 측정 실패 시 기본 2.5초

    # 2. 음성 이어붙이기 → 전체 내레이션
    _p("내레이션 오디오 합치는 중…")
    alist = work / "audio.txt"
    alist.write_text(
        "".join(f"file '{Path(m).name}'\n" for m in mp3s), encoding="utf-8")
    audio_out = work / "narration.m4a"
    _run([ffmpeg, "-y", "-f", "concat", "-safe", "0",
          "-i", "audio.txt", "-c:a", "aac", "-b:a", "192k",
          str(audio_out.name)], cwd=str(work))
    audio_dur = _get_duration(ffprobe, str(audio_out)) or sum(durations)

    # 3. 자막 (combined.srt) — 측정 길이 누적
    _p("자막 생성 중…")
    srt_lines, cursor = [], 0.0
    for i, (s, d) in enumerate(zip(sentences, durations), 1):
        srt_lines += [str(i), f"{_srt_ts(cursor)} --> {_srt_ts(cursor + d)}", s, ""]
        cursor += d
    (work / "subs.srt").write_text("\n".join(srt_lines), encoding="utf-8")

    # 4. 영상 클립 컷 + 정규화
    parts = []
    for i, c in enumerate(clips, 1):
        _p(f"영상 클립 처리 중… ({i}/{len(clips)})")
        part = work / f"clip_{i:02d}.mp4"
        _cut_clip(ffmpeg, c["path"], float(c["start"]), float(c["end"]), str(part))
        parts.append(str(part))

    # 이어붙이기
    _p("클립 이어붙이는 중…")
    concat_out = work / "concat.mp4"
    _concat(ffmpeg, parts, str(concat_out.name), work)

    # 5. 영상(부족하면 루프) + 오디오 + 자막 번인, 오디오 길이로 트림
    _p("자막 합성 및 최종 인코딩 중…")
    final_out = out_dir / output_filename
    vf = f"subtitles=subs.srt:force_style='{SUBTITLE_STYLE}'"
    _run([
        ffmpeg, "-y",
        "-stream_loop", "-1", "-i", "concat.mp4",
        "-i", "narration.m4a",
        "-vf", vf,
        "-map", "0:v:0", "-map", "1:a:0",
        "-t", f"{audio_dur}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(Path("..") / output_filename),
    ], cwd=str(work))

    _p("완료")
    return str(final_out.resolve())
