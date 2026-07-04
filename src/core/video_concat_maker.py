# -*- coding: utf-8 -*-
"""영상 이어붙이기 → CapCut draft 생성 (영상 이어붙이기 탭 백엔드)

기존 src/core/video_maker.py 의 draft 골격을 재활용한 **독립 모듈**:
  - `save_capcut_project()` 의 draft_content/meta/root_meta 저장 패턴
  - `_cc_seg_base()` (uniform_scale 구조 포함), `_cc_text_content()`, 폰트/획 상수
  를 import 해서 그대로 활용한다 (video_maker.py / video_auto_maker.py 는 수정하지 않음).

차이점:
  - 영상 여러 개를 video material 로 등록 → 한 비디오 트랙에 순차(공백 없이) 이어붙임
  - 각 클립 전체에 확대율(uniform_scale.value = 확대율/100) 적용
  - 자막 한 줄을 전체 길이(start=0, duration=합계)에 걸쳐 text 트랙으로 추가
  - TTS/Demucs/whisper/MP3 일절 없음 — 영상 자체 오디오 유지, mp4 렌더링도 안 함
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

# 기존 draft 골격 재활용 (읽기 전용 import — 원본 파일 수정 없음)
from src.core.video_maker import (
    _cc_uid,
    _cc_seg_base,
    _cc_text_content,
    _FONT_PATH,
    _FONT_RES_ID,
    _FONT_SIZE,
    _STROKE_WIDTH,
)

TARGET_W = 1080
TARGET_H = 1920

# 자막 기본 위치 (single-clip 버전과 동일한 환산식: transform = px / 캔버스)
_SUBTITLE_X = 0.0
_SUBTITLE_Y = 1000.0


# ── ffmpeg / ffprobe 탐색 (PATH + winget Packages) ────────────────
def _find_exe(name: str) -> str | None:
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


def _probe(ffprobe: str, path: str) -> tuple[float, int, int]:
    """ffprobe 로 (duration_sec, width, height) 반환. 실패 시 기본값."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(out.stdout or "{}")
        dur = float(data.get("format", {}).get("duration", 0) or 0)
        st = (data.get("streams") or [{}])[0]
        w = int(st.get("width", 0) or 0)
        h = int(st.get("height", 0) or 0)
        return dur, w, h
    except Exception:
        return 0.0, 0, 0


# ── 비디오 material (save_capcut_project 의 video_material 구조 복제) ──
def _video_material(mat_id: str, path: str, dur_us: int, w: int, h: int) -> dict:
    path_bs = str(path).replace("/", "\\")
    return {
        "id": mat_id, "unique_id": "", "type": "video",
        "duration": dur_us,
        "path": path_bs, "media_path": "",
        "local_id": "", "has_audio": True,
        "reverse_path": "", "intensifies_path": "",
        "reverse_intensifies_path": "", "intensifies_audio_path": "",
        "cartoon_path": "",
        "width": w or TARGET_W, "height": h or TARGET_H,
        "category_id": "", "category_name": "local",
        "material_id": "", "material_name": Path(path).name, "material_url": "",
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


def _extra_set() -> tuple[dict, list]:
    """클립 1개분 extra material 세트 생성. (ids, [materials...]) 반환."""
    speed_id, canvas_id = _cc_uid(), _cc_uid()
    ph_id, scm_id = _cc_uid(), _cc_uid()
    mc_id, loud_id, vsep_id = _cc_uid(), _cc_uid(), _cc_uid()
    ids = {"speed": speed_id, "canvas": canvas_id, "ph": ph_id,
           "scm": scm_id, "mc": mc_id, "loud": loud_id, "vsep": vsep_id}
    mats = [
        {"id": speed_id, "type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None},
        {"id": canvas_id, "type": "canvas_color", "color": "", "blur": 0.0,
         "image": "", "album_image": "", "image_id": "", "image_name": "",
         "source_platform": 0, "team_id": ""},
        {"id": ph_id, "type": "placeholder_info", "meta_type": "none",
         "res_path": "", "res_text": "", "error_path": "", "error_text": ""},
        {"id": scm_id, "type": "none", "audio_channel_mapping": 0, "is_config_open": False},
        {"id": mc_id, "is_color_clip": False, "is_gradient": False,
         "solid_color": "", "gradient_colors": [], "gradient_percents": [],
         "gradient_angle": 90.0, "width": 0.0, "height": 0.0},
        {"id": loud_id, "enable": False, "time_range": None,
         "file_id": "", "target_loudness": 0.0, "loudness_param": None},
        {"id": vsep_id, "type": "vocal_separation", "choice": 0,
         "removed_sounds": [], "time_range": None,
         "production_path": "", "final_algorithm": "", "enter_from": ""},
    ]
    return ids, mats


def _text_material(mat_id: str, text: str) -> dict:
    """save_capcut_project 의 text material 구조 복제 (_cc_text_content 재활용)."""
    return {
        "id": mat_id, "type": "text", "name": "",
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
        "line_spacing": 0.02, "has_shadow": False,
        "border_alpha": 1.0, "border_color": "#000000",
        "border_width": _STROKE_WIDTH, "border_mode": 0,
        "style_name": "", "text_color": "#FFFFFF",
        "text_alpha": 1.0, "font_name": "",
        "font_title": "none", "font_size": float(_FONT_SIZE),
        "font_path": _FONT_PATH, "font_id": _FONT_RES_ID,
        "font_resource_id": _FONT_RES_ID, "initial_scale": 1.0,
        "font_url": "", "typesetting": 0, "alignment": 1,
        "line_feed": 1, "use_effect_default_color": False,
        "is_rich_text": False, "shape_clip_x": False,
        "shape_clip_y": False, "ktv_color": "",
        "text_to_audio_ids": [], "bold_width": 0.0,
        "italic_degree": 0, "underline": False,
        "underline_width": 0.05, "underline_offset": 0.22,
        "sub_type": 0, "check_flag": 62978047, "text_size": _FONT_SIZE,
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
    }


# ── 메인: 영상 이어붙이기 draft 저장 ──────────────────────────────
def save_capcut_concat_project(
    video_paths: list[str],
    subtitle_text: str,
    scale_percent: int = 110,
    project_name: str = "이어붙이기 영상",
    progress=None,
) -> dict:
    """첨부 영상들을 한 비디오 트랙에 순차 이어붙인 CapCut draft 저장.

    반환: {"project_dir": str, "duration_sec": float, "clip_count": int, "project_name": str}
    """
    def _p(msg: str) -> None:
        if progress:
            progress(msg)

    ffprobe = _find_exe("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe 를 찾을 수 없습니다 (winget Gyan.FFmpeg 설치 필요)")
    if not video_paths:
        raise RuntimeError("영상이 없습니다")

    scale = max(1, int(scale_percent)) / 100.0

    # 1) 프로젝트 폴더 (CapCut User Data Projects 경로 — 기존 골격과 동일)
    username = os.getenv("USERNAME", os.getenv("USER", ""))
    lveditor_root = Path(
        rf"C:\Users\{username}\AppData\Local\CapCut\User Data\Projects\com.lveditor.draft"
    )
    lveditor_root.mkdir(parents=True, exist_ok=True)
    project_dir = lveditor_root / project_name
    idx = 1
    while project_dir.exists():
        project_dir = lveditor_root / f"{project_name} ({idx})"
        idx += 1
    final_name = project_dir.name
    project_dir.mkdir(parents=True, exist_ok=True)
    for sub in ["Resources", "adjust_mask", "common_attachment",
                "matting", "qr_upload", "smart_crop", "subdraft", "Timelines"]:
        (project_dir / sub).mkdir(exist_ok=True)

    proj_fwd = str(project_dir).replace("\\", "/")
    root_fwd = str(lveditor_root).replace("\\", "/")

    # 2) 각 영상 복사 + ffprobe 길이/해상도 측정 → 클립 구성
    video_materials: list[dict] = []
    video_segments: list[dict] = []
    extra_mats: dict[str, list] = {
        "speeds": [], "canvases": [], "placeholder_infos": [],
        "sound_channel_mappings": [], "material_colors": [],
        "loudnesses": [], "vocal_separations": [],
    }
    cursor_us = 0  # 다음 클립이 놓일 타임라인 위치(editStart)

    for i, src in enumerate(video_paths):
        _p(f"영상 분석 중… ({i + 1}/{len(video_paths)})")
        dur_sec, w, h = _probe(ffprobe, src)
        if dur_sec <= 0:
            raise RuntimeError(f"영상 길이를 읽을 수 없습니다: {Path(src).name}")
        dur_us = int(dur_sec * 1_000_000)

        # Resources 로 복사 (이름 충돌 방지 인덱스 prefix)
        dest = project_dir / "Resources" / f"{i:02d}_{Path(src).name}"
        shutil.copy2(src, dest)
        if not dest.exists():
            raise RuntimeError(f"영상 복사 실패: {dest}")

        mat_id = _cc_uid()
        seg_id = _cc_uid()
        video_materials.append(_video_material(mat_id, str(dest), dur_us, w, h))

        # 세그먼트: source 0~dur, target cursor~cursor+dur (공백 없이 순차)
        seg = _cc_seg_base(seg_id, mat_id, cursor_us, dur_us)
        seg["source_timerange"] = {"start": 0, "duration": dur_us}
        seg["target_timerange"] = {"start": cursor_us, "duration": dur_us}
        # 확대율: uniform_scale.value + clip.scale 동기화 (실제 CapCut 110% 형태)
        seg["uniform_scale"] = {"on": True, "value": scale}
        seg["clip"]["scale"] = {"x": scale, "y": scale}
        seg.update({"enable_lut": True, "enable_adjust": True})

        # extra material 세트 (클립마다 1세트)
        ids, mats = _extra_set()
        seg["extra_material_refs"] = [
            ids["speed"], ids["canvas"], ids["ph"], ids["scm"],
            ids["mc"], ids["loud"], ids["vsep"],
        ]
        # 타입별 배열에 분배
        for m in mats:
            if m["id"] == ids["speed"]:   extra_mats["speeds"].append(m)
            elif m["id"] == ids["canvas"]: extra_mats["canvases"].append(m)
            elif m["id"] == ids["ph"]:     extra_mats["placeholder_infos"].append(m)
            elif m["id"] == ids["scm"]:    extra_mats["sound_channel_mappings"].append(m)
            elif m["id"] == ids["mc"]:     extra_mats["material_colors"].append(m)
            elif m["id"] == ids["loud"]:   extra_mats["loudnesses"].append(m)
            elif m["id"] == ids["vsep"]:   extra_mats["vocal_separations"].append(m)

        video_segments.append(seg)
        cursor_us += dur_us

    total_us = cursor_us
    total_sec = total_us / 1_000_000

    # 3) 자막 (한 줄, 전체 길이) — text material + segment + track
    _p("자막 추가 중…")
    text_materials: list[dict] = []
    text_tracks: list[dict] = []
    subtitle_text = (subtitle_text or "").strip()
    if subtitle_text:
        t_mat_id, t_seg_id, t_trk_id = _cc_uid(), _cc_uid(), _cc_uid()
        text_materials.append(_text_material(t_mat_id, subtitle_text))
        tseg = _cc_seg_base(t_seg_id, t_mat_id, 0, total_us)
        tseg["source_timerange"] = None
        tseg["target_timerange"] = {"start": 0, "duration": total_us}
        tseg["render_index"] = 14000
        tseg["enable_video_mask"] = True
        tseg["clip"]["transform"]["x"] = round(_SUBTITLE_X / TARGET_W, 10)
        tseg["clip"]["transform"]["y"] = round(_SUBTITLE_Y / TARGET_H, 10)
        text_tracks.append({
            "id": t_trk_id, "type": "text", "segments": [tseg],
            "flag": 0, "attribute": 0, "name": "", "is_default_name": True,
        })

    # 4) draft_content.json
    _p("draft 작성 중…")
    content_id = _cc_uid()
    project_id = _cc_uid()
    video_trk_id = _cc_uid()
    now_us = int(time.time() * 1_000_000)
    now_sec = int(time.time())

    tracks = [
        {"id": video_trk_id, "type": "video", "flag": 0, "attribute": 0,
         "name": "", "is_default_name": True, "segments": video_segments},
        *text_tracks,
    ]

    content = {
        "id": content_id, "version": 360000,
        "new_version": "171.0.0", "name": "",
        "duration": total_us, "create_time": 0, "update_time": 0,
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
        "canvas_config": {"ratio": "9:16", "width": TARGET_W, "height": TARGET_H, "background": None},
        "tracks": tracks,
        "group_container": None,
        "materials": {
            "flowers": [],
            "videos": video_materials,
            "tail_leaders": [],
            "audios": [],
            "images": [],
            "texts": text_materials,
            "effects": [], "stickers": [], "canvases": extra_mats["canvases"],
            "transitions": [], "audio_effects": [], "audio_fades": [],
            "beats": [], "material_animations": [],
            "placeholders": [], "placeholder_infos": extra_mats["placeholder_infos"],
            "speeds": extra_mats["speeds"],
            "common_mask": [], "chromas": [], "text_templates": [],
            "realtime_denoises": [], "audio_pannings": [],
            "audio_pitch_shifts": [], "video_trackings": [],
            "hsl": [], "drafts": [], "color_curves": [], "hsl_curves": [],
            "primary_color_wheels": [], "log_color_wheels": [],
            "video_effects": [], "audio_balances": [],
            "handwrites": [], "manual_deformations": [],
            "manual_beautys": [], "plugin_effects": [],
            "sound_channel_mappings": extra_mats["sound_channel_mappings"],
            "green_screens": [], "shapes": [],
            "material_colors": extra_mats["material_colors"],
            "digital_humans": [], "digital_human_model_dressing": [],
            "smart_crops": [], "ai_translates": [],
            "audio_track_indexes": [],
            "loudnesses": extra_mats["loudnesses"],
            "vocal_beautifys": [], "vocal_separations": extra_mats["vocal_separations"],
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
            "app_id": 359289, "app_version": "8.7.0", "app_source": "cc",
            "device_id": "4965670a7390ba59e25329604cff041a",
            "hard_disk_id": "84e76ec0bed721bfff2591b4d4866f15",
            "mac_address": "5bcf6954982058d14083924a8a56bc06,e464c2c5c062a2872eef9b484c39d8e8",
        },
        "last_modified_platform": {
            "os": "windows", "os_version": "10.0.26200",
            "app_id": 359289, "app_version": "8.7.0", "app_source": "cc",
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

    # 5) draft_meta_info.json
    draft_meta_info = {
        "cloud_draft_cover": False, "cloud_draft_sync": False,
        "cloud_package_completed_time": "",
        "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False,
        "draft_cloud_package_type": "",
        "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "draft_cover.jpg", "draft_deeplink_url": "",
        "draft_enterprise_info": {
            "draft_enterprise_extra": "", "draft_enterprise_id": "",
            "draft_enterprise_name": "", "enterprise_material": [],
        },
        "draft_fold_path": proj_fwd, "draft_id": project_id,
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
        "tm_draft_removed": 0, "tm_duration": total_us,
    }

    # 6) 보조 파일
    (project_dir / "draft_biz_config.json").write_bytes(b"")
    (project_dir / "draft_agency_config.json").write_text(
        '{"is_auto_agency_enabled":false,"is_auto_agency_popup":false,'
        '"is_single_agency_mode":false,"marterials":null,"use_converter":false,'
        '"video_resolution":720}', encoding="utf-8",
    )
    (project_dir / "draft_settings").write_text(
        f"[General]\ndraft_create_time={now_sec}\n"
        f"draft_last_edit_time={now_sec}\n"
        "real_edit_seconds=0\nreal_edit_keys=0\n"
        "cloud_last_modify_platform=windows\n", encoding="utf-8",
    )
    (project_dir / "timeline_layout.json").write_text(
        json.dumps({
            "dockItems": [{"dockIndex": 0, "ratio": 1,
                           "timelineIds": [content_id],
                           "timelineNames": ["타임라인 01"]}],
            "layoutOrientation": 1,
        }), encoding="utf-8",
    )

    # 7) 본 파일 쓰기
    (project_dir / "draft_content.json").write_text(
        json.dumps(content, ensure_ascii=False), encoding="utf-8")
    (project_dir / "draft_meta_info.json").write_text(
        json.dumps(draft_meta_info, ensure_ascii=False), encoding="utf-8")

    # 8) root_meta_info.json 등록 (CapCut 목록 노출)
    root_meta_path = lveditor_root / "root_meta_info.json"
    try:
        root_meta = json.loads(root_meta_path.read_text(encoding="utf-8"))
    except Exception:
        root_meta = {"all_draft_store": [], "draft_ids": 0, "root_path": root_fwd}
    root_meta.setdefault("all_draft_store", [])
    root_meta["all_draft_store"].insert(0, {
        "cloud_draft_cover": False, "cloud_draft_sync": False,
        "draft_cloud_last_action_download": False,
        "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "",
        "draft_cover": proj_fwd + "/draft_cover.jpg",
        "draft_fold_path": proj_fwd, "draft_id": project_id,
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
        "tm_draft_removed": 0, "tm_duration": total_us,
    })
    root_meta["draft_ids"] = len(root_meta["all_draft_store"])
    root_meta["root_path"] = root_fwd
    root_meta_path.write_text(json.dumps(root_meta, ensure_ascii=False), encoding="utf-8")

    _p("완료")
    return {
        "project_dir": str(project_dir),
        "duration_sec": round(total_sec, 3),
        "clip_count": len(video_segments),
        "project_name": final_name,
    }
