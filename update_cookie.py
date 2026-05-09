"""브라우저 쿠키를 config/settings.yaml에 저장하는 스크립트

사용법:
  1. 브라우저에서 https://domeggook.com 로그인
  2. F12 → Network 탭 → 아무 요청 선택 → Request Headers → Cookie 값 복사
  3. 이 스크립트 실행 후 붙여넣기
"""
import re
import sys
import io
from pathlib import Path

import yaml

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stdin  = io.TextIOWrapper(sys.stdin.buffer,  encoding="utf-8", errors="replace")

CONFIG_PATH = Path("config/settings.yaml")


def parse_cookie_string(raw: str) -> dict:
    """'name=value; name2=value2' 형식 → dict"""
    raw = re.sub(r"^[Cc]ookie:\s*", "", raw.strip())
    result = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            result[name.strip()] = value.strip()
    return result


def main():
    print("=" * 60)
    print("  도매매(domeggook.com) 쿠키 업데이트")
    print("=" * 60)
    print()
    print("1. 브라우저에서 https://domeggook.com 로그인")
    print("2. F12 → Network 탭 → 아무 요청 선택")
    print("3. Request Headers → Cookie 값 전체 복사")
    print()
    print("Cookie 값을 붙여넣고 Enter 두 번:")
    print("-" * 60)

    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "" and lines:
            break
        lines.append(line)

    raw = " ".join(lines).strip()
    if not raw:
        print("\n[오류] 입력값이 없습니다.")
        sys.exit(1)

    cookies = parse_cookie_string(raw)
    if not cookies:
        print("\n[오류] 쿠키 파싱 실패 — 형식 확인 (name=value; name2=value2)")
        sys.exit(1)

    print(f"\n파싱된 쿠키 {len(cookies)}개:")
    for k, v in cookies.items():
        masked = v[:4] + "***" if len(v) > 4 else "***"
        print(f"  {k} = {masked}")

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg.setdefault("domaemae", {})["cookies"] = cookies

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"\n[완료] {CONFIG_PATH} 저장 완료.")
    print("이제 main.py 또는 scripts/run_task.py 를 실행하면 새 쿠키가 적용됩니다.")


if __name__ == "__main__":
    main()
