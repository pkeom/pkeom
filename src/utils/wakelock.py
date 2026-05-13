"""Termux wake-lock 헬퍼

Termux 환경(Android)에서는 termux-wake-lock / termux-wake-unlock 명령을 실행해
CPU가 절전 모드로 전환되지 않도록 한다.
비-Termux 환경(Windows/Linux/Mac)에서는 아무 동작 없이 func을 그냥 실행한다.
"""
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


def run_with_wakelock(func):
    """func을 Termux wake-lock으로 감싸 실행하고 반환값을 그대로 전달.

    - Termux가 없는 환경에서도 예외 없이 동작한다.
    - func 자체가 예외를 던지면 wake-unlock 후 그대로 재발생한다.
    """
    has_termux = shutil.which("termux-wake-lock") is not None

    if has_termux:
        try:
            subprocess.Popen(["termux-wake-lock"])
            logger.debug("termux-wake-lock 활성화")
        except Exception as e:
            logger.warning("termux-wake-lock 실행 실패 (무시): %s", e)
            has_termux = False

    try:
        return func()
    finally:
        if has_termux:
            try:
                subprocess.Popen(["termux-wake-unlock"])
                logger.debug("termux-wake-unlock 실행")
            except Exception as e:
                logger.warning("termux-wake-unlock 실행 실패 (무시): %s", e)
