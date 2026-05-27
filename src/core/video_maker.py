"""영상 초안 자동 생성 — ffmpeg + Whisper 기반 (9:16 세로형)"""

import os
import shutil
import subprocess
from pathlib import Path


def _find_ffmpeg() -> str:
    """
    ffmpeg 실행 파일 경로를 반환하고, PATH에 없는 경우 해당 디렉토리를
    os.environ['PATH']에 추가한다.
    ffmpeg-python 라이브러리가 내부적으로 ffprobe를 PATH에서 찾으므로
    반드시 PATH 등록이 필요하다.
    """
    if shutil.which("ffmpeg"):
        return "ffmpeg"

    username = os.getenv("USERNAME", os.getenv("USER", ""))
    candidates = [
        r"C:\Program Files (x86)\HitPaw\HitPaw VoicePea\ffmpeg.exe",
        r"C:\Program Files\Wondershare\Wondershare UniConverter for Windows\ffmpeg.exe",
        r"C:\Program Files (x86)\AirDroid\IncludeAdb\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Tools\ffmpeg\bin\ffmpeg.exe",
        rf"C:\Users\{username}\ffmpeg\bin\ffmpeg.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            # ffmpeg-python(ffprobe 포함)이 PATH에서 찾을 수 있도록 등록
            bin_dir = str(Path(path).parent)
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return path

    raise RuntimeError(
        "ffmpeg 바이너리를 찾을 수 없습니다.\n"
        "아래 중 하나를 실행하세요:\n"
        "  winget install --id Gyan.FFmpeg -e\n"
        "  또는 https://www.gyan.dev/ffmpeg/builds/ 에서 다운로드 후\n"
        "  C:\\ffmpeg\\bin\\ 에 압축 해제하세요."
    )


def _find_font() -> str:
    """Windows / Linux에서 적절한 한글 폰트 파일 경로 반환"""
    candidates = [
        r"C:\Windows\Fonts\malgunbd.ttf",
        r"C:\Windows\Fonts\malgun.ttf",
        r"C:\Windows\Fonts\NanumGothicBold.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for f in candidates:
        if os.path.exists(f):
            return f
    return ""


_FNAME_TO_FONTNAME = {
    "malgunbd": "Malgun Gothic Bold",
    "malgun": "Malgun Gothic",
    "NanumGothicBold": "NanumGothicBold",
    "arialbd": "Arial Bold",
    "arial": "Arial",
    "LiberationSans-Bold": "Liberation Sans",
}


def _fmt_ass_time(seconds: float) -> str:
    """초 → ASS 시간 형식 H:MM:SS.cs"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    cs = int((s % 1) * 100)
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"


def _build_ass(entries: list[tuple[float, float, str]], font_name: str) -> str:
    """ASS 자막 파일 내용 생성 (중앙 배치, 크고 굵은 흰색)"""
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},72,&H00FFFFFF,&H000000FF,"
        "&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,4,3,5,10,10,0,0\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    )
    lines = [header]
    for start, end, text in entries:
        safe = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        lines.append(
            f"Dialogue: 0,{_fmt_ass_time(start)},{_fmt_ass_time(end)},"
            f"Default,,0,0,0,,{safe}"
        )
    return "\n".join(lines)


def transcribe_audio(audio_path: str) -> list[dict]:
    """Whisper로 오디오 전사. [{start, end, text}, ...] 반환"""
    try:
        import whisper  # type: ignore
    except ImportError:
        raise RuntimeError(
            "openai-whisper 패키지가 필요합니다: pip install openai-whisper"
        )
    model = whisper.load_model("base")
    result = model.transcribe(str(audio_path), verbose=False)
    return result.get("segments", [])


def map_script_to_timings(
    script_lines: list[str],
    segments: list[dict],
    audio_duration: float,
) -> list[tuple[float, float, str]]:
    """대본 줄을 Whisper 타이밍에 비례 매핑. (start, end, text) 리스트 반환"""
    if not script_lines:
        return []

    if not segments:
        n = len(script_lines)
        chunk = audio_duration / n
        return [
            (i * chunk, min((i + 1) * chunk, audio_duration), line)
            for i, line in enumerate(script_lines)
        ]

    n_lines = len(script_lines)
    n_segs = len(segments)
    result = []

    for i, line in enumerate(script_lines):
        seg_idx = min(int(i * n_segs / n_lines), n_segs - 1)
        seg = segments[seg_idx]
        start = seg["start"]

        if i + 1 < n_lines:
            nxt = min(int((i + 1) * n_segs / n_lines), n_segs - 1)
            end = segments[nxt]["start"] if nxt != seg_idx else seg["end"]
        else:
            end = seg["end"]

        if end <= start:
            remaining = max(n_lines - i, 1)
            end = start + max(1.0, (audio_duration - start) / remaining)

        result.append((start, end, line.strip()))

    return result


def generate_video(
    bg_image_path: str,
    script_text: str,
    audio_path: str,
    output_dir: str = "data/video_output",
    output_filename: str = "draft.mp4",
) -> str:
    """
    9:16 세로형 영상을 생성하고 출력 파일의 절대 경로를 반환.

    - bg_image_path : JPG/PNG 배경 이미지
    - script_text   : 줄바꿈으로 구분된 가사/대본
    - audio_path    : MP3 파일
    """
    try:
        import ffmpeg  # type: ignore
    except ImportError:
        raise RuntimeError(
            "ffmpeg-python 패키지가 필요합니다: pip install ffmpeg-python"
        )

    # PATH 등록을 먼저 — ffmpeg.probe()가 ffprobe를 PATH에서 찾으므로
    # _find_ffmpeg() 호출이 ffmpeg.probe() 보다 반드시 앞에 있어야 함
    ffmpeg_bin = _find_ffmpeg()

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / output_filename

    script_lines = [ln for ln in script_text.strip().splitlines() if ln.strip()]

    # 오디오 길이 확인
    probe = ffmpeg.probe(str(audio_path))
    audio_duration = float(probe["format"]["duration"])

    # Whisper 전사
    segments = transcribe_audio(str(audio_path))

    # 대본 → 타이밍 매핑
    entries = map_script_to_timings(script_lines, segments, audio_duration)

    # ASS 자막 파일 생성
    font_path = _find_font()
    stem = Path(font_path).stem if font_path and os.path.exists(font_path) else ""
    font_name = _FNAME_TO_FONTNAME.get(stem, "Malgun Gothic")

    ass_path = out_dir / output_filename.replace(".mp4", ".ass")
    ass_path.write_text(_build_ass(entries, font_name), encoding="utf-8-sig")

    # ffmpeg filter — ASS 경로의 드라이브 콜론을 이스케이프
    ass_ffmpeg = str(ass_path).replace("\\", "/").replace(":", "\\:")
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920,setsar=1,subtitles='{ass_ffmpeg}'"
    )
    cmd = [
        ffmpeg_bin, "-y",
        "-loop", "1", "-framerate", "30",
        "-i", str(bg_image_path),
        "-i", str(audio_path),
        "-vf", vf,
        "-map", "0:v", "-map", "1:a",
        "-t", str(audio_duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        str(output_path),
    ]

    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 실패 (종료코드 {proc.returncode}):\n{proc.stderr[-3000:]}"
        )

    return str(output_path.resolve())


def open_in_capcut(file_path: str) -> str:
    """CapCut으로 파일 열기. 반환값: 'capcut' | 'default' | 'none'"""
    username = os.getenv("USERNAME", os.getenv("USER", ""))
    capcut_candidates = [
        rf"C:\Users\{username}\AppData\Local\CapCut\Apps\CapCut.exe",
        r"C:\Program Files\CapCut\CapCut.exe",
        r"C:\Program Files (x86)\CapCut\CapCut.exe",
        rf"C:\Users\{username}\AppData\Local\Programs\CapCut\CapCut.exe",
        rf"C:\Users\{username}\AppData\Local\CapCut\CapCut.exe",
    ]
    for path in capcut_candidates:
        if os.path.exists(path):
            subprocess.Popen([path, file_path])
            return "capcut"
    try:
        os.startfile(str(file_path))
        return "default"
    except Exception:
        return "none"
