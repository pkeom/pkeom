"""이메일 알림 (오류·가격변동·품절 등)

dedup/쿨다운:
  send(..., dedup_key=..., cooldown_hours=24) 로 호출하면 같은 키의 알림은
  쿨다운 내 1통만 발송한다. 상태는 data/email_dedup.json 에 영속 저장되어
  스케줄 실행마다 프로세스가 새로 떠도 유지된다.
  dedup_key 없이 호출하면(품절·재고부족 등) 종전과 동일하게 매번 발송한다.
"""
import json
import smtplib
import logging
import threading
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DEDUP_PATH = Path("data/email_dedup.json")
_dedup_lock = threading.Lock()


class EmailNotifier:
    def __init__(self, smtp_host: str, smtp_port: int, sender: str, password: str,
                 recipients: list, dedup_path: Path | str | None = None):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender = sender
        self.password = password
        self.recipients = recipients
        self._dedup_path = Path(dedup_path) if dedup_path else _DEFAULT_DEDUP_PATH

    # ── dedup 영속 상태 ───────────────────────────────────────

    def _load_dedup(self) -> dict:
        try:
            if self._dedup_path.exists():
                return json.loads(self._dedup_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("이메일 dedup 로드 실패 (무시): %s", e)
        return {}

    def _save_dedup(self, data: dict):
        try:
            self._dedup_path.parent.mkdir(parents=True, exist_ok=True)
            self._dedup_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("이메일 dedup 저장 실패: %s", e)

    def _is_suppressed(self, dedup_key: str, cooldown_hours: float) -> bool:
        """쿨다운 내 같은 키가 이미 발송됐으면 True."""
        data = self._load_dedup()
        last_str = data.get(dedup_key)
        if not last_str:
            return False
        try:
            last = datetime.fromisoformat(last_str)
        except ValueError:
            return False
        return datetime.now(timezone.utc) - last < timedelta(hours=cooldown_hours)

    def _mark_sent(self, dedup_key: str):
        """발송 시각 기록 + 7일 지난 항목 정리."""
        now = datetime.now(timezone.utc)
        data = self._load_dedup()
        data[dedup_key] = now.isoformat()
        cutoff = now - timedelta(days=7)
        pruned = {}
        for k, v in data.items():
            try:
                if datetime.fromisoformat(v) >= cutoff:
                    pruned[k] = v
            except (ValueError, TypeError):
                continue
        self._save_dedup(pruned)

    # ── 발송 ─────────────────────────────────────────────────

    def send(self, subject: str, body: str, *,
             dedup_key: str | None = None, cooldown_hours: float = 24):
        # dedup 키가 지정된 경우만 쿨다운 검사 (다른 알림은 영향 없음)
        if dedup_key:
            with _dedup_lock:
                if self._is_suppressed(dedup_key, cooldown_hours):
                    logger.info(
                        "이메일 알림 쿨다운 스킵 (%sh, key=%s): %s",
                        cooldown_hours, dedup_key, subject,
                    )
                    return

        msg = MIMEMultipart()
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.ehlo()
                server.starttls()
                server.login(self.sender, self.password)
                server.sendmail(self.sender, self.recipients, msg.as_string())
            logger.info("이메일 전송 완료: %s", subject)
            # 발송 성공한 경우에만 dedup 기록 (실패 시 다음 실행에서 재시도 가능)
            if dedup_key:
                with _dedup_lock:
                    self._mark_sent(dedup_key)
        except Exception as e:
            logger.error("이메일 전송 실패: %s", e, exc_info=True)
