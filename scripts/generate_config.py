"""GitHub Secrets 환경변수 → config/settings.yaml 생성
GitHub Actions에서 실행. 로컬에서는 기존 settings.yaml을 직접 편집.
"""
import os
import sys
import yaml


def require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        print(f"[ERROR] 필수 Secret 누락: {key}", flush=True)
        sys.exit(1)
    return val


def optional(key: str, default: str = "") -> str:
    return os.environ.get(key, "").strip() or default


def main():
    config = {
        "smartstore": {
            "client_id":     require("SS_CLIENT_ID"),
            "client_secret": require("SS_CLIENT_SECRET"),
        },
        "domaekkuk": {
            "api_key":  require("DOMAEKKUK_API_KEY"),
            "user_id":  optional("DOMAEKKUK_USER_ID"),
            "password": optional("DOMAEKKUK_PASSWORD"),
        },
        "domaemae": {
            "user_id":  require("DOMAEMAE_USER_ID"),
            "password": require("DOMAEMAE_PASSWORD"),
        },
        "email": {
            "smtp_host": optional("EMAIL_SMTP_HOST", "smtp.gmail.com"),
            "smtp_port": int(optional("EMAIL_SMTP_PORT", "587")),
            "sender":    optional("EMAIL_SENDER"),
            "password":  optional("EMAIL_PASSWORD"),
            "recipients": [
                r.strip()
                for r in optional("EMAIL_RECIPIENTS", "").split(",")
                if r.strip()
            ],
        },
        "database": {
            "path": "data/orders.db",
        },
        "logging": {
            "level":        "INFO",
            "log_dir":      "data/logs",
            "max_bytes":    10_485_760,
            "backup_count": 5,
        },
        "schedule": {
            "order_collect_interval":  10,
            "invoice_sync_interval":   15,
            "inventory_sync_interval": 60,
            "price_monitor_interval":  120,
        },
    }

    os.makedirs("config", exist_ok=True)
    with open("config/settings.yaml", "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print("config/settings.yaml 생성 완료", flush=True)


if __name__ == "__main__":
    main()
