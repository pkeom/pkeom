"""settings.yaml 로드 유틸"""
import yaml


def load_config(path: str = "config/settings.yaml") -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {path}")
    except yaml.YAMLError as e:
        raise ValueError(f"설정 파일 파싱 오류 ({path}): {e}")
