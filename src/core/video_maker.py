"""영상 초안 자동 생성 — ffmpeg + Whisper 기반 (9:16 세로형)"""

import json
import os
import shutil
import subprocess
import time
import uuid
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
    """Whisper로 오디오 전사. word_timestamps=True로 단어별 정밀 타이밍 획득.
    [{start, end, text, words}, ...] 반환"""
    try:
        import whisper  # type: ignore
    except ImportError:
        raise RuntimeError(
            "openai-whisper 패키지가 필요합니다: pip install openai-whisper"
        )
    model = whisper.load_model("base")
    result = model.transcribe(str(audio_path), word_timestamps=True, verbose=True)
    return result.get("segments", [])


def _apply_offset_correction(
    entries: list[tuple[float, float, str]],
    audio_duration: float,
) -> list[tuple[float, float, str]]:
    """첫 세그먼트 시작 시간을 기준으로 전체 타이밍을 0으로 정규화.
    Whisper가 무음 구간 이후 음성을 감지하면 첫 start가 0이 아닐 수 있어
    영상 시작 기준에 맞게 오프셋을 보정한다."""
    if not entries:
        return entries
    offset = entries[0][0]
    if offset == 0.0:
        return entries
    corrected = []
    for start, end, text in entries:
        new_start = max(0.0, start - offset)
        new_end = min(audio_duration, end - offset)
        if new_end <= new_start:
            new_end = new_start + 0.1
        corrected.append((new_start, new_end, text))
    return corrected


def map_script_to_timings(
    script_lines: list[str],
    segments: list[dict],
    audio_duration: float,
) -> list[tuple[float, float, str]]:
    """대본 줄을 Whisper 세그먼트의 정확한 start/end 시간에 매핑.
    비례 보간 대신 각 세그먼트의 실제 타이밍을 사용하고 오프셋을 보정한다."""
    if not script_lines:
        return []

    if not segments:
        n = len(script_lines)
        chunk = audio_duration / n
        return [
            (i * chunk, min((i + 1) * chunk, audio_duration), line.strip())
            for i, line in enumerate(script_lines)
        ]

    n_lines = len(script_lines)
    n_segs = len(segments)
    result = []

    for i, line in enumerate(script_lines):
        seg_idx = min(int(i * n_segs / n_lines), n_segs - 1)
        seg = segments[seg_idx]
        start = float(seg["start"])
        end = float(seg["end"])

        if end <= start:
            remaining = max(n_lines - i, 1)
            end = start + max(1.0, (audio_duration - start) / remaining)

        result.append((start, end, line.strip()))

    # 영상 시작 시간 기준 오프셋 보정
    result = _apply_offset_correction(result, audio_duration)
    return result


# ── CapCut 프로젝트 저장 헬퍼 ─────────────────────────────────────────

def _capcut_text_material(text_id: str, content: str) -> dict:
    return {
        "add_type": 0,
        "alignment": 1,
        "background_alpha": 1.0,
        "background_color": "",
        "background_height": 0.14,
        "background_horizontal_offset": 0.0,
        "background_round_radius": 0.0,
        "background_style": 0,
        "background_vertical_offset": 0.0,
        "background_width": 0.82,
        "base_content": "",
        "bold_width": 0.0,
        "border_alpha": 1.0,
        "border_color": "",
        "border_width": 0.08,
        "check_flag": 7,
        "combo_info": {"text_templates": []},
        "content": content,
        "fixed_height": -1.0,
        "fixed_width": -1.0,
        "font_size": 9.0,
        "global_alpha": 1.0,
        "group_id": "",
        "has_shadow": True,
        "id": text_id,
        "initial_scale": 1.0,
        "inner_padding": -1.0,
        "is_rich_text": False,
        "italic_degree": 0,
        "layer_weight": 1,
        "letter_spacing": 0.0,
        "line_feed": 1,
        "line_max_width": 0.82,
        "line_spacing": 0.02,
        "name": "",
        "original_size": [],
        "shadow_alpha": 0.9,
        "shadow_angle": -45.0,
        "shadow_color": "",
        "shadow_distance": 0.0,
        "shadow_point": {"x": 0.6364, "y": -0.6364},
        "shadow_smoothing": 1.0,
        "style_name": "",
        "sub_type": 0,
        "text_alpha": 1.0,
        "text_color": "#FFFFFF",
        "text_size": 30,
        "type": "text",
        "underline": False,
        "words": {"end_time": [], "start_time": [], "text": []},
    }


def _capcut_segment(
    seg_id: str,
    material_id: str,
    start_us: int,
    duration_us: int,
    seg_type: str = "video",
) -> dict:
    return {
        "cartoon": False,
        "clip": {
            "alpha": 1.0,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": 0.0},
        },
        "common_keyframes": [],
        "enable_adjust": seg_type == "video",
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "extra_material_refs": [],
        "group_id": "",
        "hdr_settings": None,
        "id": seg_id,
        "intensifies_audio": False,
        "is_placeholder": False,
        "is_tone_modify": False,
        "key_frames": [],
        "last_nonzero_volume": 1.0,
        "material_id": material_id,
        "render_index": 0,
        "responsive_layout": {
            "enable": False,
            "horizontal_pos_layout": 0,
            "size_layout": 0,
            "target_follow": "",
            "vertical_pos_layout": 0,
        },
        "reverse": False,
        "source_timerange": {"duration": duration_us, "start": 0},
        "target_timerange": {"duration": duration_us, "start": start_us},
        "template_id": "",
        "template_scene": "default",
        "track_attribute": 0,
        "track_render_index": 0,
        "uniform_scale": None,
        "visible": True,
        "volume": 1.0,
    }


def save_capcut_project(
    video_path: str,
    audio_path: str,
    subtitle_entries: list[tuple[float, float, str]],
    project_name: str = "스마트스토어 영상",
) -> str:
    """CapCut 편집 가능한 프로젝트로 저장.

    - 영상 클립 트랙, 오디오 트랙, 자막 트랙을 각각 분리 저장
    - 저장 경로: C:\\Users\\{username}\\AppData\\Local\\CapCut\\User Data\\Projects
    - draft_info.json / draft_content.json / draft_meta_info.json 생성
    반환값: 프로젝트 폴더 절대 경로
    """
    try:
        import ffmpeg  # type: ignore
    except ImportError:
        raise RuntimeError("ffmpeg-python 패키지가 필요합니다: pip install ffmpeg-python")

    username = os.getenv("USERNAME", os.getenv("USER", ""))
    projects_root = Path(rf"C:\Users\{username}\AppData\Local\CapCut\User Data\Projects")
    projects_root.mkdir(parents=True, exist_ok=True)

    project_id = uuid.uuid4().hex.upper()
    project_dir = projects_root / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    resources_dir = project_dir / "resources"
    resources_dir.mkdir(exist_ok=True)

    # 미디어 파일을 프로젝트 resources 폴더로 복사
    video_res_id = uuid.uuid4().hex
    audio_res_id = uuid.uuid4().hex
    video_dest = resources_dir / f"video_{video_res_id}.mp4"
    audio_dest = resources_dir / f"audio_{audio_res_id}.mp3"
    shutil.copy2(video_path, video_dest)
    shutil.copy2(audio_path, audio_dest)

    # 영상 길이 (마이크로초)
    probe = ffmpeg.probe(str(video_path))
    duration_sec = float(probe["format"]["duration"])
    duration_us = int(duration_sec * 1_000_000)

    now_ms = int(time.time() * 1000)

    # material / track / segment ID 생성
    video_mat_id = uuid.uuid4().hex
    audio_mat_id = uuid.uuid4().hex
    video_track_id = uuid.uuid4().hex
    audio_track_id = uuid.uuid4().hex
    video_seg_id = uuid.uuid4().hex
    audio_seg_id = uuid.uuid4().hex

    # 자막 materials & 자막별 단독 트랙
    text_materials: list[dict] = []
    text_tracks: list[dict] = []
    for start, end, text in subtitle_entries:
        t_mat_id = uuid.uuid4().hex
        t_seg_id = uuid.uuid4().hex
        t_track_id = uuid.uuid4().hex
        start_us = int(start * 1_000_000)
        dur_us = max(1, int((end - start) * 1_000_000))
        text_materials.append(_capcut_text_material(t_mat_id, text))
        text_tracks.append({
            "attribute": 0,
            "flag": 0,
            "id": t_track_id,
            "is_default_name": True,
            "name": "",
            "segments": [_capcut_segment(t_seg_id, t_mat_id, start_us, dur_us, "text")],
            "type": "text",
        })

    # 비디오 material
    video_material = {
        "aigc_type": "none",
        "audio_fade": None,
        "cartoon_path": "",
        "category_id": "",
        "category_name": "",
        "check_flag": 63,
        "crop": {
            "lower_left_x": 0.0, "lower_left_y": 1.0,
            "lower_right_x": 1.0, "lower_right_y": 1.0,
            "upper_left_x": 0.0, "upper_left_y": 0.0,
            "upper_right_x": 1.0, "upper_right_y": 0.0,
        },
        "duration": duration_us,
        "extra_type_option": 0,
        "formula_id": "",
        "freeze": None,
        "has_audio": False,
        "height": 1920,
        "id": video_mat_id,
        "local_material_id": "",
        "material_name": Path(video_path).name,
        "media_path": str(video_dest),
        "path": str(video_dest),
        "picture_from": "none",
        "source": 0,
        "source_platform": 0,
        "type": "photo",
        "width": 1080,
    }

    # 오디오 material
    audio_material = {
        "app_id": "",
        "category_id": "",
        "category_name": "",
        "check_flag": 63,
        "duration": duration_us,
        "effect_id": "",
        "id": audio_mat_id,
        "local_material_id": "",
        "music_id": "",
        "name": Path(audio_path).name,
        "path": str(audio_dest),
        "type": "extract_music",
        "wave_points": [],
    }

    tracks = [
        {
            "attribute": 0,
            "flag": 0,
            "id": video_track_id,
            "is_default_name": True,
            "name": "",
            "segments": [_capcut_segment(video_seg_id, video_mat_id, 0, duration_us, "video")],
            "type": "video",
        },
        {
            "attribute": 0,
            "flag": 0,
            "id": audio_track_id,
            "is_default_name": True,
            "name": "",
            "segments": [_capcut_segment(audio_seg_id, audio_mat_id, 0, duration_us, "audio")],
            "type": "audio",
        },
        *text_tracks,
    ]

    # draft_content.json
    content = {
        "canvas_config": {"height": 1920, "ratio": "9:16", "width": 1080},
        "color_space": 0,
        "config": {
            "adjust_max_index": 1,
            "attachment_info": [],
            "combination_max_index": 1,
            "export_range": None,
            "extract_audio_last_index": 1,
            "lyrics_recognition_id": "",
            "lyrics_sync": False,
            "original_sound_last_index": 1,
            "record_audio_last_index": 1,
            "sticker_max_index": 1,
            "subtitle_recognition_id": "",
            "subtitle_sync": False,
            "video_mute": False,
        },
        "cover": None,
        "create_time": now_ms // 1000,
        "duration": duration_us,
        "fps": 30.0,
        "id": uuid.uuid4().hex,
        "keyframe_graph_list": [],
        "keyframes": {
            "adjusts": [], "audios": [], "effects": [], "filters": [],
            "handwrites": [], "stickers": [], "texts": [], "videos": [],
        },
        "materials": {
            "audios": [audio_material],
            "beats": [], "canvases": [], "chromas": [], "color_curves": [],
            "digital_humans": [], "drafts": [], "effects": [], "flowers": [],
            "green_screens": [], "handwrites": [], "hsl": [],
            "log_color_wheels": [], "loudnesses": [], "manual_deformations": [],
            "masks": [], "material_animations": [], "music_moodboard": [],
            "placeholders": [], "plugin_effects": [], "primary_color_wheels": [],
            "realtime_denoises": [], "shapes": [], "smart_crops": [],
            "smart_relights": [], "sound_channel_mappings": [], "speeds": [],
            "stickers": [], "tail_leaders": [],
            "texts": text_materials,
            "transitions": [], "video_effects": [], "video_trackings": [],
            "videos": [video_material],
            "vocal_beautifys": [], "vocal_separations": [],
        },
        "mutable_config": None,
        "new_version": "102.0.0",
        "platform": {
            "app_id": "3704",
            "app_source": "lv",
            "app_version": "4.0.0",
            "device_id": "",
            "hard_disk_id": "",
            "mac_address": "",
            "os": "windows",
            "os_version": "10.0.22631",
        },
        "relationships": [],
        "render_index_track_mode_on": False,
        "retouch_cover": None,
        "source": "default",
        "static_cover_image_path": "",
        "time_marks": None,
        "tracks": tracks,
        "update_time": now_ms // 1000,
        "version": 360000,
    }

    # draft_info.json
    draft_info = {
        "draft_cloud_last_action_download": False,
        "draft_fold_path": str(project_dir),
        "draft_id": project_id,
        "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts_used": False,
        "draft_is_article_video_draft": False,
        "draft_is_from_deeplink": "",
        "draft_is_invisible": False,
        "draft_name": project_name,
        "draft_removable_storage_device": "",
        "draft_root_path": str(project_dir),
        "draft_segment_extra_info": [],
        "draft_timeline_metedata": "",
        "tm_draft_create": now_ms,
        "tm_draft_modified": now_ms,
        "tm_duration": duration_us,
    }

    # draft_meta_info.json
    draft_meta_info = {
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": str(project_dir / "cover.jpg"),
        "draft_deeplink_url": "",
        "draft_enterprise_info": {
            "chorus_info": "",
            "draft_enterprise_extra": "",
            "enterprise_extra": "",
            "team_id": "",
            "team_name": "",
            "team_preview_url": "",
        },
        "draft_fold_path": str(project_dir),
        "draft_id": project_id,
        "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts_used": False,
        "draft_is_article_video_draft": False,
        "draft_is_from_deeplink": "",
        "draft_is_invisible": False,
        "draft_materials": [
            {"type": 0, "value": [str(video_dest)]},
            {"type": 11, "value": [str(audio_dest)]},
        ],
        "draft_name": project_name,
        "draft_new_version": "102.0.0",
        "draft_removable_storage_device": "",
        "draft_root_path": str(project_dir),
        "draft_segment_extra_info": [],
        "draft_timeline_metedata": "",
        "tm_draft_create": now_ms,
        "tm_draft_modified": now_ms,
        "tm_duration": duration_us,
    }

    (project_dir / "draft_info.json").write_text(
        json.dumps(draft_info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (project_dir / "draft_content.json").write_text(
        json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (project_dir / "draft_meta_info.json").write_text(
        json.dumps(draft_meta_info, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return str(project_dir)


def generate_video(
    bg_image_path: str,
    script_text: str,
    audio_path: str,
    output_dir: str = "data/video_output",
    output_filename: str = "draft.mp4",
) -> tuple[str, str]:
    """
    9:16 세로형 영상을 생성하고 CapCut 프로젝트로 저장.

    반환값: (영상 절대 경로, CapCut 프로젝트 폴더 경로)

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
    ffmpeg_bin = _find_ffmpeg()

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / output_filename

    script_lines = [ln for ln in script_text.strip().splitlines() if ln.strip()]

    # 오디오 길이 확인
    probe = ffmpeg.probe(str(audio_path))
    audio_duration = float(probe["format"]["duration"])

    # Whisper 전사 (word_timestamps=True로 정밀 타이밍)
    segments = transcribe_audio(str(audio_path))

    # 대본 → 정확한 타이밍 매핑 (오프셋 보정 포함)
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

    video_path = str(output_path.resolve())

    # CapCut 프로젝트 저장 (비디오·오디오·자막 각 트랙 분리)
    project_name = Path(output_filename).stem
    capcut_dir = save_capcut_project(
        video_path=video_path,
        audio_path=audio_path,
        subtitle_entries=entries,
        project_name=project_name,
    )

    return video_path, capcut_dir


def open_in_capcut(capcut_project_dir: str) -> str:
    """CapCut 실행. Projects 폴더에 저장된 프로젝트는 앱 시작 시 자동으로 목록에 표시됨.
    반환값: 'capcut' | 'explorer' | 'none'"""
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
            subprocess.Popen([path])
            return "capcut"
    # CapCut 없으면 프로젝트 폴더를 탐색기로 열기
    try:
        subprocess.Popen(["explorer", capcut_project_dir])
        return "explorer"
    except Exception:
        return "none"
