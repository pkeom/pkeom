"""메인 GUI 윈도우"""
import tkinter as tk
from tkinter import ttk
from gui.dashboard import DashboardFrame
from gui.mapping_editor import MappingEditorFrame
from gui.price_alerts import PriceAlertFrame
from gui.settings_editor import SettingsEditorFrame
from src.core.price_alert_repository import PriceAlertRepository

_alert_repo = PriceAlertRepository()

_TAB_ALERT_IDX = 2   # "가격 알림" 탭 인덱스 (0-based)


class MainWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("위탁판매 자동화 시스템")
        self.root.geometry("1200x760")
        self.root.minsize(900, 600)
        self._build_ui()

    # ── UI 구성 ──────────────────────────────────────────────

    def _build_ui(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.dashboard    = DashboardFrame(self.notebook)
        self.mapping      = MappingEditorFrame(self.notebook)
        self.price_alerts = PriceAlertFrame(self.notebook, on_read=self._update_badge)
        self.settings     = SettingsEditorFrame(self.notebook)

        self.notebook.add(self.dashboard.frame,    text="  대시보드  ")
        self.notebook.add(self.mapping.frame,      text="  상품 매핑  ")
        self.notebook.add(self.price_alerts.frame, text="  가격 알림  ")
        self.notebook.add(self.settings.frame,     text="  설정  ")

        self._update_badge()

    # ── 가격 알림 뱃지 ───────────────────────────────────────

    def _update_badge(self):
        """가격 알림 탭 제목에 미읽음 건수 뱃지 표시. 30초마다 자동 갱신."""
        try:
            count = _alert_repo.count_unread()
            label = "  가격 알림  " if count == 0 else f"  가격 알림 ({count})  "
            self.notebook.tab(_TAB_ALERT_IDX, text=label)
            self.root.after(30_000, self._update_badge)
        except tk.TclError:
            pass  # 창 닫힘 후 호출 시 무시

    # ── 실행 ─────────────────────────────────────────────────

    def run(self):
        self.root.mainloop()
