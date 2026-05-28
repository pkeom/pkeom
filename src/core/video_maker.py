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
    """ASS 자막 파일 내용 생성 (화면 중앙 배치, 단어별 표시)"""
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},52,&H00FFFFFF,&H000000FF,"
        "&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,3,2,5,80,80,60,0\n\n"
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


def words_to_timings(
    segments: list[dict],
    audio_duration: float,
) -> list[tuple[float, float, str]]:
    """Whisper word_timestamps 결과에서 단어별 자막 타이밍을 추출한다.

    word_timestamps=True 로 전사하면 각 segment 안에 'words' 배열이 생기고,
    각 원소는 {'word': str, 'start': float, 'end': float} 형태를 갖는다.
    이 함수는 그 단어들을 그대로 (start, end, word) 튜플로 펼쳐 반환한다.
    겹침이 있으면 이전 단어의 end를 다음 단어의 start로 맞춘다.
    """
    entries: list[tuple[float, float, str]] = []
    for seg in segments:
        for w in seg.get("words", []):
            word  = str(w.get("word", "")).strip()
            if not word:
                continue
            ws = float(w.get("start", seg["start"]))
            we = float(w.get("end",   seg["end"]))
            if we <= ws:
                we = ws + 0.3
            entries.append((ws, we, word))

    if not entries:
        return []

    # 각 단어의 end를 다음 단어의 start로 맞춤: 갭 없이 한 단어씩 이어서 표시
    for i in range(len(entries) - 1):
        s, _, w = entries[i]
        ns = entries[i + 1][0]
        entries[i] = (s, ns, w)

    return _apply_offset_correction(entries, audio_duration)


def _split_line_entries_to_words(
    line_entries: list[tuple[float, float, str]],
) -> list[tuple[float, float, str]]:
    """줄 단위 타이밍을 개별 단어로 균등 분할.
    words_to_timings()가 빈 배열일 때(word_timestamps 없음) fallback으로 사용."""
    result: list[tuple[float, float, str]] = []
    for start, end, text in line_entries:
        words = text.split()
        if not words:
            continue
        n = len(words)
        slot = (end - start) / n
        for i, word in enumerate(words):
            result.append((start + i * slot, start + (i + 1) * slot, word))
    # 각 단어 end를 다음 단어 start로 정렬 (갭/겹침 제거)
    for i in range(len(result) - 1):
        s, _, w = result[i]
        result[i] = (s, result[i + 1][0], w)
    return result


def map_script_to_timings(
    script_lines: list[str],
    segments: list[dict],
    audio_duration: float,
) -> list[tuple[float, float, str]]:
    """대본 줄을 Whisper 세그먼트 타이밍에 매핑 (CapCut 텍스트 트랙용 줄 단위 fallback).

    여러 줄이 같은 세그먼트에 할당될 때 세그먼트 시간을 균등 분배하여
    줄이 순서대로 표시되도록 한다.
    """
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
    n_segs  = len(segments)
    seg_of  = [min(int(i * n_segs / n_lines), n_segs - 1) for i in range(n_lines)]

    result: list[tuple[float, float, str]] = []
    i = 0
    while i < n_lines:
        s_idx = seg_of[i]
        seg   = segments[s_idx]
        seg_s = float(seg["start"])
        seg_e = float(seg["end"])

        j = i + 1
        while j < n_lines and seg_of[j] == s_idx:
            j += 1
        group = script_lines[i:j]
        count = len(group)

        if seg_e <= seg_s:
            seg_e = seg_s + count * 2.0

        slot = (seg_e - seg_s) / count
        for k, line in enumerate(group):
            result.append((seg_s + k * slot, seg_s + (k + 1) * slot, line.strip()))

        i = j

    for idx in range(1, len(result)):
        ps, pe, pt = result[idx - 1]
        cs, ce, ct = result[idx]
        if pe > cs:
            result[idx - 1] = (ps, cs, pt)

    return _apply_offset_correction(result, audio_duration)


def map_script_words_to_timings(
    script_lines: list[str],
    segments: list[dict],
    audio_duration: float,
) -> list[tuple[float, float, str]]:
    """대본 단어를 Whisper 타이밍에 순서대로 매핑한다.

    Whisper가 인식한 텍스트(가사)는 완전히 무시하고 타이밍만 사용.
    표시할 텍스트는 script_lines에서 추출한 단어만 사용한다.
    """
    # 대본 전체 단어 목록
    script_words: list[str] = []
    for line in script_lines:
        script_words.extend(w for w in line.split() if w)
    if not script_words:
        return []

    # Whisper word-level 타이밍만 추출 (텍스트는 버림)
    whisper_times: list[tuple[float, float]] = []
    for seg in segments:
        for w in seg.get("words", []):
            ws = float(w.get("start", seg["start"]))
            we = float(w.get("end",   seg["end"]))
            if we <= ws:
                we = ws + 0.3
            whisper_times.append((ws, we))

    # word timestamps 없으면 segment 단위 타이밍으로 대체
    if not whisper_times:
        for seg in segments:
            s, e = float(seg["start"]), float(seg["end"])
            if e > s:
                whisper_times.append((s, e))

    # segment도 없으면 전체 시간 균등 분할
    if not whisper_times:
        n = len(script_words)
        slot = audio_duration / n
        entries = [(i * slot, (i + 1) * slot, w) for i, w in enumerate(script_words)]
        for i in range(len(entries) - 1):
            s, _, w = entries[i]
            entries[i] = (s, entries[i + 1][0], w)
        return entries

    # 비율 매핑: 대본 단어 i → Whisper 타이밍 j
    n_s = len(script_words)
    n_w = len(whisper_times)
    entries: list[tuple[float, float, str]] = []
    for i, word in enumerate(script_words):
        j = min(int(i * n_w / n_s), n_w - 1)
        ws, we = whisper_times[j]
        entries.append((ws, we, word))

    # 각 단어 end = 다음 단어 start (연속 표시, 갭/겹침 없음)
    for i in range(len(entries) - 1):
        s, _, w = entries[i]
        entries[i] = (s, entries[i + 1][0], w)

    return _apply_offset_correction(entries, audio_duration)


# ── CapCut 프로젝트 저장 ─────────────────────────────────────────────

def _cc_uid() -> str:
    """CapCut 스타일 UUID 생성 (대문자 하이픈 형식)"""
    return str(uuid.uuid4()).upper()


def _cc_text_content(text: str) -> str:
    """CapCut text material의 content 필드용 JSON 문자열 생성"""
    return json.dumps({
        "text": text,
        "styles": [{
            "fill": {"content": {"render_type": "solid", "solid": {"color": [1, 1, 1]}}},
            "font": {"path": "", "id": ""},
            "size": 15,
            "range": [0, len(text)],
        }],
    }, ensure_ascii=False)


def _cc_seg_base(seg_id: str, mat_id: str, start_us: int, dur_us: int) -> dict:
    """세그먼트 공통 구조"""
    return {
        "id": seg_id,
        "source_timerange": {"start": 0, "duration": dur_us},
        "target_timerange": {"start": start_us, "duration": dur_us},
        "render_timerange": {"start": 0, "duration": 0},
        "desc": "", "state": 0, "speed": 1.0,
        "is_loop": False, "is_tone_modify": False,
        "reverse": False, "intensifies_audio": False,
        "cartoon": False, "volume": 1.0, "last_nonzero_volume": 1.0,
        "clip": {
            "scale": {"x": 1.0, "y": 1.0},
            "rotation": 0.0,
            "transform": {"x": 0.0, "y": 0.0},
            "flip": {"vertical": False, "horizontal": False},
            "alpha": 1.0,
        },
        "uniform_scale": {"on": True, "value": 1.0},
        "material_id": mat_id,
        "extra_material_refs": [],
        "render_index": 0,
        "keyframe_refs": [],
        "enable_lut": False, "enable_adjust": False,
        "enable_hsl": False, "enable_hsl_curves": True,
        "enable_color_curves": True, "enable_color_wheels": True,
        "enable_smart_color_adjust": False,
        "enable_color_match_adjust": False,
        "enable_color_correct_adjust": False,
        "enable_adjust_mask": False,
        "enable_color_adjust_pro": False,
        "enable_video_mask": False,
        "visible": True, "group_id": "",
        "track_render_index": 0, "track_attribute": 0,
        "hdr_settings": None, "is_placeholder": False,
        "template_id": "", "template_scene": "default",
        "common_keyframes": [], "caption_info": None,
        "responsive_layout": {
            "enable": False, "target_follow": "",
            "size_layout": 0, "horizontal_pos_layout": 0,
            "vertical_pos_layout": 0,
        },
        "raw_segment_id": "", "lyric_keyframes": None,
        "digital_human_template_group_id": "",
        "color_correct_alg_result": "",
        "source": "segmentsourcenormal",
        "enable_mask_stroke": False, "enable_mask_shadow": False,
    }


def save_capcut_project(
    video_path: str,
    audio_path: str,
    subtitle_entries: list[tuple[float, float, str]],
    project_name: str = "스마트스토어 영상",
) -> str:
    """CapCut 편집 가능한 프로젝트 저장.

    - 저장 경로: com.lveditor.draft/{project_name}/
    - root_meta_info.json 업데이트 → CapCut 프로젝트 목록에 즉시 표시
    - 영상 클립 / 오디오 / 자막 트랙 각각 분리
    반환값: 프로젝트 폴더 절대 경로
    """
    try:
        import ffmpeg  # type: ignore
    except ImportError:
        raise RuntimeError("ffmpeg-python 패키지가 필요합니다: pip install ffmpeg-python")

    username = os.getenv("USERNAME", os.getenv("USER", ""))
    lveditor_root = Path(
        rf"C:\Users\{username}\AppData\Local\CapCut\User Data\Projects\com.lveditor.draft"
    )
    lveditor_root.mkdir(parents=True, exist_ok=True)

    # 중복 이름 방지
    project_dir = lveditor_root / project_name
    idx = 1
    while project_dir.exists():
        project_dir = lveditor_root / f"{project_name} ({idx})"
        idx += 1
    final_name = project_dir.name
    project_dir.mkdir(parents=True, exist_ok=True)

    # 하위 폴더 생성 (CapCut이 기대하는 구조)
    for sub in ["Resources", "adjust_mask", "common_attachment",
                "matting", "qr_upload", "smart_crop", "subdraft", "Timelines"]:
        (project_dir / sub).mkdir(exist_ok=True)

    # 미디어 파일 Resources 폴더로 복사
    video_dest = project_dir / "Resources" / Path(video_path).name
    audio_dest = project_dir / "Resources" / Path(audio_path).name
    shutil.copy2(video_path, video_dest)
    shutil.copy2(audio_path, audio_dest)

    # 경로 포맷: CapCut은 forward slash 사용
    video_fwd = str(video_dest).replace("\\", "/")
    audio_fwd = str(audio_dest).replace("\\", "/")
    proj_fwd  = str(project_dir).replace("\\", "/")
    root_fwd  = str(lveditor_root).replace("\\", "/")

    # 영상 길이 (마이크로초)
    probe = ffmpeg.probe(str(video_path))
    duration_sec = float(probe["format"]["duration"])
    duration_us  = int(duration_sec * 1_000_000)

    # CapCut 타임스탬프: microseconds since Unix epoch
    now_us  = int(time.time() * 1_000_000)
    now_sec = int(time.time())

    # ── IDs ──────────────────────────────────────────────────────────
    project_id    = _cc_uid()
    content_id    = _cc_uid()
    video_mat_id  = _cc_uid()
    audio_mat_id  = _cc_uid()
    video_trk_id  = _cc_uid()
    audio_trk_id  = _cc_uid()
    video_seg_id  = _cc_uid()
    audio_seg_id  = _cc_uid()
    # 비디오 세그먼트 extra refs (CapCut 내부 처리용)
    speed_id  = _cc_uid()
    canvas_id = _cc_uid()
    ph_id     = _cc_uid()   # placeholder_info
    scm_id    = _cc_uid()   # sound_channel_mapping
    mc_id     = _cc_uid()   # material_color
    loud_id   = _cc_uid()   # loudness
    vsep_id   = _cc_uid()   # vocal_separation

    # ── 자막 materials & tracks ──────────────────────────────────────
    text_materials: list[dict] = []
    mat_animations: list[dict] = []
    text_tracks:    list[dict] = []

    for start, end, text in subtitle_entries:
        t_mat_id  = _cc_uid()
        t_seg_id  = _cc_uid()
        t_trk_id  = _cc_uid()
        t_anim_id = _cc_uid()
        s_us  = int(start * 1_000_000)
        d_us  = max(1, int((end - start) * 1_000_000))

        text_materials.append({
            "id": t_mat_id, "type": "text", "name": "",
            "recognize_task_id": "", "recognize_text": "",
            "recognize_model": "", "punc_model": "",
            "content": _cc_text_content(text),
            "base_content": "",
            "words": {"start_time": [], "end_time": [], "text": []},
            "current_words": {"start_time": [], "end_time": [], "text": []},
            "global_alpha": 1.0,
            "combo_info": {"text_templates": []},
            "caption_template_info": {
                "resource_id": "", "third_resource_id": "",
                "resource_name": "", "category_id": "", "category_name": "",
                "effect_id": "", "request_id": "", "path": "",
                "is_new": False, "source_platform": 0,
            },
            "layer_weight": 1, "letter_spacing": 0.0,
            "line_spacing": 0.02, "has_shadow": True,
            "shadow_color": "", "shadow_alpha": 0.9,
            "shadow_smoothing": 0.45, "shadow_distance": 5.0,
            "shadow_point": {"x": 0.6363961030678928, "y": -0.6363961030678928},
            "shadow_angle": -45.0,
            "shadow_thickness_projection_enable": False,
            "shadow_thickness_projection_angle": 0.0,
            "shadow_thickness_projection_distance": 0.0,
            "border_alpha": 1.0, "border_color": "",
            "border_width": 0.08, "border_mode": 0,
            "style_name": "", "text_color": "#FFFFFF",
            "text_alpha": 1.0, "font_name": "",
            "font_title": "none", "font_size": 15.0,
            "font_path": "", "font_id": "",
            "font_resource_id": "", "initial_scale": 1.0,
            "font_url": "", "typesetting": 0, "alignment": 1,
            "line_feed": 1, "use_effect_default_color": True,
            "is_rich_text": False, "shape_clip_x": False,
            "shape_clip_y": False, "ktv_color": "",
            "text_to_audio_ids": [], "bold_width": 0.0,
            "italic_degree": 0, "underline": False,
            "underline_width": 0.05, "underline_offset": 0.22,
            "sub_type": 0, "check_flag": 7, "text_size": 30,
            "font_category_name": "", "font_source_platform": 0,
            "font_category_id": "", "add_type": 0,
            "operation_type": 0, "recognize_type": 0,
            "fonts": [], "background_color": "",
            "background_alpha": 1.0, "background_style": 0,
            "background_round_radius": 0.0,
            "background_width": 0.14, "background_height": 0.14,
            "background_vertical_offset": 0.0,
            "background_horizontal_offset": 0.0,
            "background_fill": "",
            "single_char_bg_enable": False,
            "single_char_bg_color": "", "single_char_bg_alpha": 1.0,
            "single_char_bg_round_radius": 0.3,
            "single_char_bg_width": 0.0, "single_char_bg_height": 0.0,
            "single_char_bg_vertical_offset": 0.0,
            "single_char_bg_horizontal_offset": 0.0,
            "font_team_id": "", "tts_auto_update": False,
            "text_preset_resource_id": "", "group_id": "",
            "preset_id": "", "preset_name": "",
            "preset_category": "", "preset_category_id": "",
            "preset_index": 0, "preset_has_set_alignment": False,
            "force_apply_line_max_width": False, "language": "",
            "relevance_segment": [], "original_size": [],
            "fixed_width": -1.0, "fixed_height": -1.0,
            "line_max_width": 0.82, "oneline_cutoff": False,
            "cutoff_postfix": "",
            "subtitle_template_original_fontsize": 0.0,
            "subtitle_keywords": None, "inner_padding": -1.0,
            "multi_language_current": "none",
            "source_from": "", "is_lyric_effect": False,
            "lyric_group_id": "",
            "lyrics_template": {
                "resource_id": "", "resource_name": "",
                "panel": "", "effect_id": "", "path": "",
                "category_id": "", "category_name": "",
                "request_id": "",
            },
            "is_batch_replace": False, "is_words_linear": False,
            "ssml_content": "", "subtitle_keywords_config": None,
            "sub_template_id": -1, "translate_original_text": "",
        })

        mat_animations.append({
            "id": t_anim_id,
            "type": "sticker_animation",
            "animations": [],
            "multi_language_current": "none",
        })

        tseg = _cc_seg_base(t_seg_id, t_mat_id, s_us, d_us)
        tseg["source_timerange"] = None          # 텍스트 세그먼트는 source null
        tseg["render_index"]     = 14000
        tseg["enable_video_mask"] = True
        tseg["extra_material_refs"] = [t_anim_id]

        text_tracks.append({
            "id": t_trk_id, "type": "text",
            "segments": [tseg],
            "flag": 0, "attribute": 0,
            "name": "", "is_default_name": True,
        })

    # ── 비디오 material ───────────────────────────────────────────────
    video_material = {
        "id": video_mat_id, "unique_id": "",
        "type": "video",
        "duration": duration_us,
        "path": video_fwd, "media_path": "",
        "local_id": "", "has_audio": True,
        "reverse_path": "", "intensifies_path": "",
        "reverse_intensifies_path": "", "intensifies_audio_path": "",
        "cartoon_path": "",
        "width": 1080, "height": 1920,
        "category_id": "", "category_name": "local",
        "material_id": "", "material_name": Path(video_path).name,
        "material_url": "",
        "crop": {
            "upper_left_x": 0.0, "upper_left_y": 0.0,
            "upper_right_x": 1.0, "upper_right_y": 0.0,
            "lower_left_x": 0.0, "lower_left_y": 1.0,
            "lower_right_x": 1.0, "lower_right_y": 1.0,
        },
        "crop_ratio": "free", "audio_fade": None,
        "crop_scale": 1.0, "extra_type_option": 0,
        "stable": {"stable_level": 0, "matrix_path": "",
                   "time_range": {"start": 0, "duration": 0}},
        "matting": {
            "flag": 0, "path": "", "interactiveTime": [],
            "has_use_quick_brush": False, "strokes": [],
            "has_use_quick_eraser": False, "expansion": 0,
            "feather": 0, "reverse": False,
            "custom_matting_id": "", "enable_matting_stroke": False,
        },
        "source": 0, "source_platform": 0, "formula_id": "",
        "check_flag": 62978047,
        "video_algorithm": {
            "algorithms": [], "time_range": None, "path": "",
            "gameplay_configs": [], "ai_in_painting_config": [],
            "complement_frame_config": None, "motion_blur_config": None,
            "deflicker": None, "noise_reduction": None,
            "quality_enhance": None, "super_resolution": None,
            "ai_background_configs": [], "smart_complement_frame": None,
            "aigc_generate": None, "aigc_generate_list": [],
            "mouth_shape_driver": None, "ai_expression_driven": None,
            "ai_motion_driven": None, "image_interpretation": None,
            "story_video_modify_video_config": {
                "task_id": "", "is_overwrite_last_video": False,
                "tracker_task_id": "",
            },
            "skip_algorithm_index": [],
        },
        "is_unified_beauty_mode": False, "object_locked": None,
        "smart_motion": None, "multi_camera_info": None, "freeze": None,
        "picture_from": "none",
        "picture_set_category_id": "", "picture_set_category_name": "",
        "team_id": "", "local_material_id": "", "origin_material_id": "",
        "request_id": "", "has_sound_separated": False,
        "is_text_edit_overdub": False, "is_ai_generate_content": False,
        "aigc_type": "none", "is_copyright": False,
        "aigc_history_id": "", "aigc_item_id": "",
        "local_material_from": "", "smart_match_info": None,
        "beauty_face_preset_infos": [], "beauty_body_preset_id": "",
        "beauty_face_auto_preset": {"preset_id": "", "name": "", "rate_map": "", "scene": ""},
        "beauty_face_auto_preset_infos": [],
        "beauty_body_auto_preset": None,
        "live_photo_timestamp": -1, "live_photo_cover_path": "",
        "content_feature_info": None, "corner_pin": None,
        "surface_trackings": [],
        "video_mask_stroke": {
            "resource_id": "", "path": "", "type": "", "color": "",
            "size": 0.0, "alpha": 0.0, "distance": 0.0,
            "texture": 0.0, "horizontal_shift": 0.0, "vertical_shift": 0.0,
        },
        "video_mask_shadow": {
            "resource_id": "", "path": "", "color": "",
            "alpha": 0.0, "blur": 0.0, "distance": 0.0, "angle": 0.0,
        },
    }

    # ── 오디오 material ───────────────────────────────────────────────
    audio_material = {
        "id": audio_mat_id, "type": "extract_music",
        "name": Path(audio_path).name,
        "path": audio_fwd, "duration": duration_us,
        "check_flag": 1, "wave_points": [],
        "app_id": "", "category_id": "", "category_name": "",
        "effect_id": "", "formula_id": "",
        "local_material_id": "", "music_id": "",
        "request_id": "", "resource_id": "",
        "search_id": "", "source_platform": 0,
        "team_id": "", "text_id": "",
        "tone_folder_path": "", "query": "",
        "extra_content": "", "intensifies_audio_id": "",
    }

    # ── extra materials ───────────────────────────────────────────────
    speed_mat  = {"id": speed_id,  "type": "speed",  "mode": 0, "speed": 1.0, "curve_speed": None}
    canvas_mat = {"id": canvas_id, "type": "canvas_color", "color": "", "blur": 0.0,
                  "image": "", "album_image": "", "image_id": "", "image_name": "",
                  "source_platform": 0, "team_id": ""}
    ph_mat     = {"id": ph_id, "type": "placeholder_info", "meta_type": "none",
                  "res_path": "", "res_text": "", "error_path": "", "error_text": ""}
    scm_mat    = {"id": scm_id, "type": "none",
                  "audio_channel_mapping": 0, "is_config_open": False}
    mc_mat     = {"id": mc_id, "is_color_clip": False, "is_gradient": False,
                  "solid_color": "", "gradient_colors": [], "gradient_percents": [],
                  "gradient_angle": 90.0, "width": 0.0, "height": 0.0}
    loud_mat   = {"id": loud_id, "enable": False, "time_range": None,
                  "file_id": "", "target_loudness": 0.0, "loudness_param": None}
    vsep_mat   = {"id": vsep_id, "type": "vocal_separation", "choice": 0,
                  "removed_sounds": [], "time_range": None,
                  "production_path": "", "final_algorithm": "", "enter_from": ""}

    # ── 트랙 구성 ─────────────────────────────────────────────────────
    vseg = _cc_seg_base(video_seg_id, video_mat_id, 0, duration_us)
    vseg.update({
        "enable_lut": True, "enable_adjust": True,
        "enable_video_mask": True,
        "hdr_settings": {"mode": 1, "intensity": 1.0, "nits": 1000},
        "extra_material_refs": [speed_id, canvas_id, ph_id, scm_id, mc_id, loud_id, vsep_id],
    })

    aseg = _cc_seg_base(audio_seg_id, audio_mat_id, 0, duration_us)

    tracks = [
        {"id": video_trk_id, "type": "video", "flag": 0, "attribute": 0,
         "name": "", "is_default_name": True, "segments": [vseg]},
        {"id": audio_trk_id, "type": "audio", "flag": 0, "attribute": 0,
         "name": "", "is_default_name": True, "segments": [aseg]},
        *text_tracks,
    ]

    # ── draft_content.json ────────────────────────────────────────────
    content = {
        "id": content_id, "version": 360000,
        "new_version": "171.0.0", "name": "",
        "duration": duration_us, "create_time": 0, "update_time": 0,
        "fps": 30.0, "is_drop_frame_timecode": False, "color_space": 0,
        "config": {
            "video_mute": False,
            "record_audio_last_index": 1, "extract_audio_last_index": 1,
            "original_sound_last_index": 1,
            "subtitle_recognition_id": "", "subtitle_taskinfo": [],
            "lyrics_recognition_id": "", "lyrics_taskinfo": [],
            "subtitle_sync": True, "lyrics_sync": True,
            "voice_change_sync": False, "sticker_max_index": 1,
            "adjust_max_index": 1, "material_save_mode": 0,
            "export_range": None, "maintrack_adsorb": True,
            "combination_max_index": 1, "attachment_info": [],
            "zoom_info_params": None, "system_font_list": [],
            "multi_language_mode": "none", "multi_language_main": "none",
            "multi_language_current": "none", "multi_language_list": [],
            "subtitle_keywords_config": None, "use_float_render": False,
        },
        "canvas_config": {"ratio": "9:16", "width": 1080, "height": 1920, "background": None},
        "tracks": tracks,
        "group_container": None,
        "materials": {
            "flowers": [],
            "videos": [video_material],
            "tail_leaders": [],
            "audios": [audio_material],
            "images": [],
            "texts": text_materials,
            "effects": [], "stickers": [], "canvases": [canvas_mat],
            "transitions": [], "audio_effects": [], "audio_fades": [],
            "beats": [], "material_animations": mat_animations,
            "placeholders": [], "placeholder_infos": [ph_mat],
            "speeds": [speed_mat],
            "common_mask": [], "chromas": [], "text_templates": [],
            "realtime_denoises": [], "audio_pannings": [],
            "audio_pitch_shifts": [], "video_trackings": [],
            "hsl": [], "drafts": [], "color_curves": [], "hsl_curves": [],
            "primary_color_wheels": [], "log_color_wheels": [],
            "video_effects": [], "audio_balances": [],
            "handwrites": [], "manual_deformations": [],
            "manual_beautys": [], "plugin_effects": [],
            "sound_channel_mappings": [scm_mat],
            "green_screens": [], "shapes": [],
            "material_colors": [mc_mat],
            "digital_humans": [], "digital_human_model_dressing": [],
            "smart_crops": [], "ai_translates": [],
            "audio_track_indexes": [],
            "loudnesses": [loud_mat],
            "vocal_beautifys": [], "vocal_separations": [vsep_mat],
            "smart_relights": [], "time_marks": [],
            "multi_language_refs": [], "video_shadows": [],
            "video_strokes": [], "video_radius": [],
        },
        "keyframes": {
            "videos": [], "audios": [], "texts": [],
            "stickers": [], "filters": [], "adjusts": [],
            "handwrites": [], "effects": [],
        },
        "keyframe_graph_list": [],
        "platform": {
            "os": "windows", "os_version": "10.0.26200",
            "app_id": 359289, "app_version": "8.7.0",
            "app_source": "cc", "device_id": "", "hard_disk_id": "",
            "mac_address": "",
        },
        "last_modified_platform": {
            "os": "windows", "os_version": "10.0.26200",
            "app_id": 359289, "app_version": "8.7.0",
            "app_source": "cc", "device_id": "", "hard_disk_id": "",
            "mac_address": "",
        },
        "mutable_config": None, "cover": None, "retouch_cover": None,
        "extra_info": None, "relationships": [],
        "render_index_track_mode_on": True,
        "free_render_index_mode_on": False,
        "static_cover_image_path": "", "source": "default",
        "time_marks": None, "path": "", "lyrics_effects": [],
        "uneven_animation_template_info": {
            "composition": "", "content": "", "order": "",
            "sub_template_info_list": [],
        },
        "draft_type": "video",
        "smart_ads_info": {"page_from": "", "routine": "", "draft_url": ""},
        "function_assistant_info": {
            "smart_rec_applied": False, "fixed_rec_applied": False,
            "auto_adjust": False, "auto_adjust_segid_list": [],
            "color_correction": False, "color_correction_segid_list": [],
            "enhance_quality": False, "smooth_slow_motion": False,
            "deflicker_segid_list": [], "video_noise_segid_list": [],
            "enhance_quality_segid_list": [], "smart_segid_list": [],
            "retouch": False, "retouch_segid_list": [],
            "enhande_voice": False, "enhance_voice_segid_list": [],
            "audio_noise_segid_list": [], "auto_caption": False,
            "auto_caption_segid_list": [], "auto_caption_template_id": "",
            "caption_opt": False, "caption_opt_segid_list": [],
            "eye_correction": False, "eye_correction_segid_list": [],
            "normalize_loudness": False, "normalize_loudness_segid_list": [],
            "normalize_loudness_audio_denoise_segid_list": [],
            "auto_adjust_fixed": False, "auto_adjust_fixed_value": 50.0,
            "color_correction_fixed": False, "color_correction_fixed_value": 50.0,
            "normalize_loudness_fixed": False, "enhande_voice_fixed": False,
            "retouch_fixed": False, "enhance_quality_fixed": False,
            "smooth_slow_motion_fixed": False,
            "fps": {"num": 0, "den": 1},
        },
    }

    # ── draft_meta_info.json ──────────────────────────────────────────
    draft_meta_info = {
        "cloud_draft_cover": False, "cloud_draft_sync": False,
        "cloud_package_completed_time": "",
        "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False,
        "draft_cloud_package_type": "",
        "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "draft_cover.jpg",
        "draft_deeplink_url": "",
        "draft_enterprise_info": {
            "draft_enterprise_extra": "", "draft_enterprise_id": "",
            "draft_enterprise_name": "", "enterprise_material": [],
        },
        "draft_fold_path": proj_fwd,
        "draft_id": project_id,
        "draft_is_ae_produce": False, "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False, "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False,
        "draft_is_cloud_temp_draft": False,
        "draft_is_from_deeplink": "false",
        "draft_is_invisible": False, "draft_is_pippit_draft": False,
        "draft_is_web_article_video": False,
        "draft_materials": [
            {"type": 0, "value": []}, {"type": 1, "value": []},
            {"type": 2, "value": []}, {"type": 3, "value": []},
            {"type": 6, "value": []}, {"type": 7, "value": []},
            {"type": 8, "value": []},
        ],
        "draft_materials_copied_info": [],
        "draft_name": final_name,
        "draft_need_rename_folder": False, "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": root_fwd,
        "draft_segment_extra_info": [],
        "draft_timeline_materials_size_": 0,
        "draft_type": "", "draft_web_article_video_enter_from": "",
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_entry_id": -1, "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_parent_entry_id": -1,
        "tm_draft_cloud_space_id": -1, "tm_draft_cloud_user_id": -1,
        "tm_draft_create": now_us, "tm_draft_modified": now_us,
        "tm_draft_removed": 0, "tm_duration": duration_us,
    }

    # ── 보조 파일 ─────────────────────────────────────────────────────
    (project_dir / "draft_biz_config.json").write_bytes(b"")
    (project_dir / "draft_agency_config.json").write_text(
        '{"is_auto_agency_enabled":false,"is_auto_agency_popup":false,'
        '"is_single_agency_mode":false,"marterials":null,"use_converter":false,'
        '"video_resolution":720}',
        encoding="utf-8",
    )
    (project_dir / "draft_settings").write_text(
        f"[General]\ndraft_create_time={now_sec}\n"
        f"draft_last_edit_time={now_sec}\n"
        "real_edit_seconds=0\nreal_edit_keys=0\n"
        "cloud_last_modify_platform=windows\n",
        encoding="utf-8",
    )
    (project_dir / "timeline_layout.json").write_text(
        json.dumps({
            "dockItems": [{"dockIndex": 0, "ratio": 1,
                           "timelineIds": [content_id],
                           "timelineNames": ["타임라인 01"]}],
            "layoutOrientation": 1,
        }),
        encoding="utf-8",
    )

    # ── 파일 쓰기 ─────────────────────────────────────────────────────
    (project_dir / "draft_content.json").write_text(
        json.dumps(content, ensure_ascii=False), encoding="utf-8"
    )
    (project_dir / "draft_meta_info.json").write_text(
        json.dumps(draft_meta_info, ensure_ascii=False), encoding="utf-8"
    )

    # ── root_meta_info.json 업데이트 ─────────────────────────────────
    # CapCut이 이 파일의 all_draft_store 배열을 읽어 프로젝트 목록 표시
    root_meta_path = lveditor_root / "root_meta_info.json"
    try:
        root_meta = json.loads(root_meta_path.read_text(encoding="utf-8"))
    except Exception:
        root_meta = {"all_draft_store": [], "draft_ids": 0,
                     "root_path": root_fwd}

    new_entry = {
        "cloud_draft_cover": False, "cloud_draft_sync": False,
        "draft_cloud_last_action_download": False,
        "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "",
        "draft_cover": proj_fwd + "/draft_cover.jpg",
        "draft_fold_path": proj_fwd,
        "draft_id": project_id,
        "draft_is_ai_shorts": False, "draft_is_cloud_temp_draft": False,
        "draft_is_invisible": False, "draft_is_web_article_video": False,
        "draft_json_file": proj_fwd + "/draft_content.json",
        "draft_name": final_name, "draft_new_version": "",
        "draft_root_path": root_fwd,
        "draft_timeline_materials_size": 0,
        "draft_type": "", "draft_web_article_video_enter_from": "",
        "streaming_edit_draft_ready": True,
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_entry_id": -1, "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_parent_entry_id": -1,
        "tm_draft_cloud_space_id": -1, "tm_draft_cloud_user_id": -1,
        "tm_draft_create": now_us, "tm_draft_modified": now_us,
        "tm_draft_removed": 0, "tm_duration": duration_us,
    }
    root_meta["all_draft_store"].insert(0, new_entry)
    root_meta["draft_ids"] = len(root_meta["all_draft_store"])
    root_meta["root_path"] = root_fwd

    root_meta_path.write_text(
        json.dumps(root_meta, ensure_ascii=False), encoding="utf-8"
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

    # 대본 단어를 Whisper 타이밍에 매핑 (텍스트: 대본, 타이밍: Whisper)
    capcut_entries = map_script_words_to_timings(script_lines, segments, audio_duration)

    # ffmpeg filter — 자막 burn-in 없이 배경 이미지만 인코딩
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
    cmd = [
        ffmpeg_bin, "-y",
        "-loop", "1", "-r", "30",
        "-i", str(bg_image_path),
        "-i", str(audio_path),
        "-vf", vf,
        "-map", "0:v", "-map", "1:a",
        "-t", str(audio_duration),
        # CapCut 호환 코덱 설정
        "-c:v", "libx264",
        "-profile:v", "high", "-level:v", "4.0",
        "-preset", "fast", "-crf", "23",
        "-r", "30",                   # 출력 프레임레이트
        "-c:a", "aac", "-b:a", "192k",
        "-ar", "44100", "-ac", "2",   # 44.1 kHz 스테레오
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",    # MP4 moov 앞으로 이동 — 스트리밍/앱 호환성
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

    # 파일 유효성 확인 — 10 KB 미만이면 인코딩 실패로 간주
    out_size = output_path.stat().st_size
    if out_size < 10_000:
        raise RuntimeError(
            f"영상 파일이 비정상적으로 작습니다 ({out_size} bytes). "
            "ffmpeg 인코딩을 확인하세요."
        )

    video_path = str(output_path.resolve())

    # CapCut 프로젝트 저장 (비디오·오디오·자막 각 트랙 분리)
    project_name = Path(output_filename).stem
    capcut_dir = save_capcut_project(
        video_path=video_path,
        audio_path=audio_path,
        subtitle_entries=capcut_entries,
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
