"""설정 탭: settings.yaml 편집"""
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
import yaml

CONFIG_PATH = "config/settings.yaml"


class SettingsEditorFrame:
    def __init__(self, parent):
        self.frame = ttk.Frame(parent)
        self._build()

    # ── UI 구성 ──────────────────────────────────────────────

    def _build(self):
        # 헤더
        header = ttk.Frame(self.frame)
        header.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(header, text="settings.yaml", font=("", 11, "bold")).pack(side="left")
        ttk.Label(header, text=f"  ({CONFIG_PATH})",
                  font=("", 9), foreground="#888").pack(side="left")

        # 도움말
        hint = (
            "# 수정 후 [저장] 클릭 → 백그라운드 데몬 재시작 시 적용\n"
            "# client_secret: 네이버 bcrypt 솔트 ($2a$...) 형식\n"
        )
        ttk.Label(self.frame, text=hint, font=("Consolas", 9),
                  foreground="#666", justify="left").pack(anchor="w", padx=12)

        # 텍스트 에디터
        editor_frame = ttk.Frame(self.frame)
        editor_frame.pack(fill="both", expand=True, padx=10, pady=4)
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(0, weight=1)

        self.text = tk.Text(editor_frame, font=("Consolas", 10), wrap="none",
                            undo=True, relief="solid", bd=1)
        vsb = ttk.Scrollbar(editor_frame, orient="vertical",   command=self.text.yview)
        hsb = ttk.Scrollbar(editor_frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(    row=0, column=1, sticky="ns")
        hsb.grid(    row=1, column=0, sticky="ew")

        # 버튼 바
        btn_bar = ttk.Frame(self.frame)
        btn_bar.pack(fill="x", padx=10, pady=(2, 8))
        ttk.Button(btn_bar, text="불러오기", command=self._load).pack(side="left", padx=4)
        ttk.Button(btn_bar, text="저장",     command=self._save).pack(side="left", padx=4)
        self._lbl_status = ttk.Label(btn_bar, text="", foreground="#555", font=("", 9))
        self._lbl_status.pack(side="left", padx=8)

        self._load()

    # ── 파일 I/O ─────────────────────────────────────────────

    def _load(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                content = f.read()
            self.text.delete("1.0", "end")
            self.text.insert("1.0", content)
            self.text.edit_reset()
            self._lbl_status.config(text="불러오기 완료")
        except FileNotFoundError:
            messagebox.showerror("오류", f"{CONFIG_PATH} 파일을 찾을 수 없습니다.")

    def _save(self):
        content = self.text.get("1.0", "end-1c")
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as e:
            messagebox.showerror("YAML 오류", f"YAML 형식이 잘못되었습니다.\n\n{e}")
            return
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        now = datetime.now().strftime("%H:%M:%S")
        self._lbl_status.config(text=f"저장 완료 ({now})  — 데몬 재시작 시 적용")
