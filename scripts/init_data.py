"""데이터·로그 디렉토리 초기화 (파일 없을 때만 생성)
GitHub Actions 최초 실행 시 또는 새 환경 세팅 시 사용.
"""
import json
import os

DEFAULTS = {
    "data/orders.json":       {"orders": []},
    "data/mappings.json":     {"mappings": []},
    "data/price_alerts.json": {"alerts": []},
    "data/stock_cache.json":  {"status": {}},
}

DIRS = ["data", "data/logs", "config"]


def main():
    for d in DIRS:
        os.makedirs(d, exist_ok=True)

    for path, default in DEFAULTS.items():
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=2)
            print(f"생성: {path}")
        else:
            print(f"존재: {path}")

    print("초기화 완료")


if __name__ == "__main__":
    main()
