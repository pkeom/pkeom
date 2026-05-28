"""영상 초안 자동 생성 — ffmpeg + Demucs + WhisperX 기반 (9:16 세로형)"""

import difflib
import json
import os
import re
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



def _separate_vocals(audio_path: str) -> str:
    """Demucs htdemucs 모델로 보컬 스템 분리.

    배경음악과 보컬을 분리해 WhisperX가 보컬만 전사하도록 한다.
    반환: 임시 보컬 WAV 파일 경로 (호출자가 삭제 책임)
    """
    try:
        import torch
        import numpy as np
        import soundfile as sf
        from demucs.pretrained import get_model
        from demucs.apply import apply_model
    except ImportError as e:
        raise RuntimeError(
            f"demucs/soundfile 패키지가 필요합니다: pip install demucs soundfile\n원인: {e}"
        )

    model = get_model("htdemucs")
    model.eval()

    # torchaudio 2.11+ 는 MP3/WAV 모두 torchcodec 필요 → soundfile + ffmpeg로 대체
    # 1) ffmpeg로 PCM WAV 변환
    ffmpeg_bin = _find_ffmpeg()
    wav_input = str(Path(audio_path).with_suffix("")) + f"_input_{uuid.uuid4().hex[:8]}.wav"
    subprocess.run(
        [ffmpeg_bin, "-y", "-i", str(audio_path),
         "-ar", str(model.samplerate), "-ac", "2", "-f", "wav", wav_input],
        check=True, capture_output=True,
    )
    # 2) soundfile로 로드 → torch tensor (2, samples)
    try:
        data, sr = sf.read(wav_input, dtype="float32")  # (samples, channels)
    finally:
        try:
            os.unlink(wav_input)
        except Exception:
            pass
    wav = torch.from_numpy(data.T)  # (channels, samples)
    if wav.shape[0] == 1:
        wav = wav.repeat(2, 1)  # mono → stereo

    # 정규화
    ref = wav.mean(0)
    wav_norm = (wav - ref.mean()) / ref.std()

    with torch.no_grad():
        sources = apply_model(model, wav_norm.unsqueeze(0), device="cpu", progress=False)

    # 보컬 스템 추출 (htdemucs stems: drums/bass/other/vocals)
    stems = list(model.sources)
    if "vocals" not in stems:
        raise RuntimeError(f"htdemucs 모델에 vocals 스템 없음: {stems}")

    vocals_idx = stems.index("vocals")
    vocals = sources[0, vocals_idx] * ref.std() + ref.mean()

    # 임시 파일 저장 (soundfile 사용, torchaudio.save는 torchcodec 필요)
    vocals_tmp = str(Path(audio_path).with_suffix("")) + f"_vocals_{uuid.uuid4().hex[:8]}.wav"
    vocals_np = vocals.cpu().numpy().T  # (samples, channels)
    sf.write(vocals_tmp, vocals_np, model.samplerate)
    return vocals_tmp


def transcribe_audio(audio_path: str) -> list[dict]:
    """Demucs 보컬 분리 + faster-whisper 정밀 전사.

    1) Demucs htdemucs 로 배경음악 제거 → 보컬 WAV 추출
       (보컬이 없는 순수 배경음악이면 WhisperX와 동일하게 segments=[] 반환)
    2) faster-whisper base 모델로 전사 (CTranslate2 최적화, word_timestamps=True)

    반환: [{start, end, text, words:[{word, start, end}...]}, ...]
    Note: WhisperX는 Python 3.14 미지원 — faster-whisper(WhisperX 핵심 엔진)로 대체
    """
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        raise RuntimeError(
            "faster-whisper 패키지가 필요합니다: pip install faster-whisper"
        )

    vocals_path: str | None = None
    try:
        # 1단계: Demucs 보컬 분리
        vocals_path = _separate_vocals(str(audio_path))

        # 2단계: faster-whisper 전사 (word_timestamps=True)
        model = WhisperModel("base", device="cpu", compute_type="float32")
        segments_gen, _ = model.transcribe(
            vocals_path,
            language="ko",
            word_timestamps=True,
            condition_on_previous_text=False,
        )

        # generator → list 변환 (faster-whisper는 lazy generator 반환)
        segments: list[dict] = []
        for seg in segments_gen:
            words = []
            for w in (seg.words or []):
                words.append({"word": w.word, "start": w.start, "end": w.end})
            segments.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "words": words,
            })

        return segments

    finally:
        if vocals_path and os.path.exists(vocals_path):
            try:
                os.unlink(vocals_path)
            except Exception:
                pass


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



def _text_match_starts(
    phrases: list[str],
    segments: list[dict],
    audio_duration: float,
) -> list[float] | None:
    """텍스트 유사도 기반 구절→Whisper 타임스탬프 매핑.

    각 구절을 Whisper 단어 시퀀스에서 SequenceMatcher로 가장 유사한 위치에 매핑.
    - 정방향 greedy: phrase[i] 매핑 위치 이후에서만 phrase[i+1] 탐색
    - window 크기 1~8 범위 슬라이딩으로 최대 유사도 위치 선택
    - 평균 매칭 점수가 0.2 미만이면 None 반환 (fallback 유도)
    """
    words: list[tuple[float, str]] = []
    for seg in segments:
        for w in seg.get("words", []):
            norm = re.sub(r"[\s\W]+", "", w.get("word", ""), flags=re.UNICODE).lower()
            if norm:
                words.append((float(w.get("start", seg["start"])), norm))

    if not words:
        return None

    n_words = len(words)
    n_phrases = len(phrases)
    search_from = 0
    starts: list[float] = []
    scores: list[float] = []

    for ph_idx, phrase in enumerate(phrases):
        norm_phrase = re.sub(r"[\s\W]+", "", phrase, flags=re.UNICODE).lower()
        if not norm_phrase:
            starts.append(words[min(search_from, n_words - 1)][0])
            scores.append(0.0)
            continue

        best_score = -1.0
        best_start = words[min(search_from, n_words - 1)][0]
        best_pos = search_from

        # 남은 구절 수에 따라 탐색 상한 조정 (뒤쪽 구절에 자리를 남김)
        remaining_after = n_phrases - ph_idx - 1
        search_limit = max(search_from + 1, n_words - remaining_after)

        for pos in range(search_from, min(search_limit, n_words)):
            for win in range(1, min(9, n_words - pos + 1)):
                window_text = "".join(w[1] for w in words[pos: pos + win])
                score = difflib.SequenceMatcher(
                    None, norm_phrase, window_text
                ).ratio()
                # 구절보다 현저히 짧은 window에 패널티
                # (예: "비올때" vs "때" → 0.5가 "비었대" vs "비올때" 0.333을 이기는 오작동 방지)
                length_ratio = len(window_text) / max(1, len(norm_phrase))
                if length_ratio < 0.6:
                    score *= length_ratio
                if score > best_score:
                    best_score = score
                    best_start = words[pos][0]
                    best_pos = pos

        starts.append(best_start)
        scores.append(best_score)
        search_from = best_pos + 1

        if search_from >= n_words:
            # 남은 구절은 마지막 단어 이후 균등 배분
            remaining = n_phrases - len(starts)
            if remaining > 0:
                last_t = starts[-1]
                step = max(0.3, (audio_duration - last_t) / (remaining + 1))
                for k in range(1, remaining + 1):
                    starts.append(min(last_t + step * k, audio_duration - 0.1))
                    scores.append(0.0)
            break

    if len(starts) != n_phrases:
        return None

    avg_score = sum(scores) / len(scores) if scores else 0.0
    if avg_score < 0.2:
        return None

    return starts


def _enforce_monotone(
    starts: list[float],
    audio_duration: float,
    min_gap: float = 0.05,
) -> list[float]:
    """시작 시간이 단조 증가하고 오디오 범위 내에 있도록 보정."""
    if not starts:
        return starts
    n = len(starts)
    result = [max(0.0, starts[0])]
    for i in range(1, n):
        prev = result[-1]
        t = max(starts[i], prev + min_gap)
        t = min(t, audio_duration - min_gap * (n - i))
        result.append(t)
    return result


def map_phrases_to_timings(
    phrases: list[str],
    segments: list[dict],
    audio_duration: float,
) -> list[tuple[float, float, str]]:
    """쉼표 구분 구절 목록을 Whisper 타이밍에 순서대로 매핑.

    우선순위:
    1) 텍스트 유사도 매핑 (_text_match_starts) — Demucs 원본 타임라인 보존
    2) 비율/슬롯 분배 fallback → offset 보정 적용
    """
    if not phrases:
        return []

    n_s = len(phrases)

    # 1) 텍스트 유사도 매핑 (segments 있을 때)
    if segments:
        matched = _text_match_starts(phrases, segments, audio_duration)
        if matched is not None and len(matched) == n_s:
            starts = _enforce_monotone(matched, audio_duration)
            entries: list[tuple[float, float, str]] = []
            for i, phrase in enumerate(phrases):
                s = starts[i]
                e = starts[i + 1] if i + 1 < n_s else audio_duration
                if e <= s:
                    e = s + 0.1
                entries.append((s, e, phrase))
            return entries  # Demucs가 원본 타임라인 보존 → offset 보정 불필요

    # 2) fallback: 기존 ratio/slot/char-proportional 로직
    whisper_starts: list[float] = []
    for seg in segments:
        for w in seg.get("words", []):
            whisper_starts.append(float(w.get("start", seg["start"])))

    if not whisper_starts:
        for seg in segments:
            whisper_starts.append(float(seg["start"]))

    if not whisper_starts:
        total_chars = sum(len(p) for p in phrases) or 1
        t = 0.0
        starts = []
        for p in phrases:
            starts.append(t)
            t += audio_duration * len(p) / total_chars

    elif len(whisper_starts) >= n_s:
        n_w = len(whisper_starts)
        starts = [whisper_starts[int(i * n_w / n_s)] for i in range(n_s)]

    else:
        n_w = len(whisper_starts)
        slot_ends = whisper_starts[1:] + [audio_duration]
        slot_of = [min(int(i * n_w / n_s), n_w - 1) for i in range(n_s)]

        slot_count: dict[int, int] = {}
        for j in slot_of:
            slot_count[j] = slot_count.get(j, 0) + 1

        slot_pos: dict[int, int] = {}
        starts = []
        for i in range(n_s):
            j = slot_of[i]
            pos = slot_pos.get(j, 0)
            slot_pos[j] = pos + 1
            s_slot = whisper_starts[j]
            e_slot = slot_ends[j]
            starts.append(s_slot + pos * (e_slot - s_slot) / slot_count[j])

    entries2: list[tuple[float, float, str]] = []
    for i, phrase in enumerate(phrases):
        s = starts[i]
        e = starts[i + 1] if i + 1 < n_s else audio_duration
        if e <= s:
            e = s + 0.1
        entries2.append((s, e, phrase))

    return _apply_offset_correction(entries2, audio_duration)


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

    # 파일 복사 결과 확인
    if not video_dest.exists():
        raise RuntimeError(f"영상 파일 복사 실패: {video_dest}")
    if not audio_dest.exists():
        raise RuntimeError(f"오디오 파일 복사 실패: {audio_dest}")

    # 경로 포맷: 절대 경로, Windows 구분자(\) 사용
    video_fwd = str(video_dest).replace("/", "\\")
    audio_fwd = str(audio_dest).replace("/", "\\")
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
        "fps": 30.0, "is_drop_frame_timecode": False, "color_space": -1,
        "config": {
            "video_mute": False,
            "record_audio_last_index": 1, "extract_audio_last_index": 1,
            "original_sound_last_index": 1,
            "subtitle_recognition_id": "", "subtitle_taskinfo": [],
            "lyrics_recognition_id": "", "lyrics_taskinfo": [],
            "subtitle_sync": False, "lyrics_sync": False,
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
            "app_source": "cc",
            "device_id": "4965670a7390ba59e25329604cff041a",
            "hard_disk_id": "84e76ec0bed721bfff2591b4d4866f15",
            "mac_address": "5bcf6954982058d14083924a8a56bc06,e464c2c5c062a2872eef9b484c39d8e8",
        },
        "last_modified_platform": {
            "os": "windows", "os_version": "10.0.26200",
            "app_id": 359289, "app_version": "8.7.0",
            "app_source": "cc",
            "device_id": "4965670a7390ba59e25329604cff041a",
            "hard_disk_id": "84e76ec0bed721bfff2591b4d4866f15",
            "mac_address": "5bcf6954982058d14083924a8a56bc06,e464c2c5c062a2872eef9b484c39d8e8",
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
    - script_text   : 쉼표(,)로 구분된 자막 구절 (예: "운전하는사람이면,끝까지봐,비올때")
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

    # 쉼표 구분 구절 파싱
    phrases = [p.strip() for p in script_text.split(",") if p.strip()]
    if not phrases:
        raise RuntimeError("자막 구절을 입력하세요. 예: 운전하는사람이면,끝까지봐,비올때")

    # 오디오 길이 확인
    probe = ffmpeg.probe(str(audio_path))
    audio_duration = float(probe["format"]["duration"])

    # Whisper 전사 (타이밍 추출용, 텍스트는 사용하지 않음)
    segments = transcribe_audio(str(audio_path))

    # 구절을 Whisper 타이밍에 매핑
    capcut_entries = map_phrases_to_timings(phrases, segments, audio_duration)

    # 자막 내용 검증: 반드시 입력한 구절만 포함되어야 함
    phrases_set = set(phrases)
    for _, _, text in capcut_entries:
        if text not in phrases_set:
            raise RuntimeError(
                f"자막 텍스트 오염 감지: '{text}'는 입력 구절에 없습니다. "
                "Whisper 텍스트가 자막으로 유입되었습니다."
            )

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
