"""가격 알림 탭: 도매처 가격 변동 내역 확인 및 읽음 처리"""
import tkinter as tk
from tkinter import ttk, messagebox
from src.core.price_alert_repository import PriceAlertRepository

_UNREAD_BG = "#FFFDE7"  # 미읽음 행 배경 (연노랑)


def _fmt_price(val) -> str:
    if val is None:
        return "-"
    return f"{int(val):,}원"


def _fmt_rate(rate) -> str:
    if rate is None:
        return "-"
    pct = rate * 100
    if pct > 0:
        return f"▲ +{pct:.1f}%"
    if pct < 0:
        return f"▼ {pct:.1f}%"
    return "—"


class PriceAlertFrame:
    def __init__(self, parent, on_read=None):
        self.frame    = ttk.Frame(parent)
        self._repo    = PriceAlertRepository()
        self._on_read = on_read          # 읽음 처리 후 메인윈도우 뱃지 갱신용 콜백
        self._unread_only = tk.BooleanVar(value=False)
        self._build()
        self._load()

    # ── UI 구성 ──────────────────────────────────────────────

    def _build(self):
        self._build_toolbar()
        self._build_table()
        self._build_bottom()

    def _build_toolbar(self):
        bar = ttk.Frame(self.frame)
        bar.pack(fill="x", padx=10, pady=(8, 2))

        ttk.Checkbutton(
            bar, text="미읽음만 보기",
            variable=self._unread_only,
            command=self._load,
        ).pack(side="left", padx=4)

        ttk.Button(bar, text="⟳ 새로고침",    command=self._load).pack(side="right", padx=4)
        ttk.Button(bar, text="전체 읽음처리", command=self._mark_all_read).pack(side="right", padx=4)

    def _build_table(self):
        frame = ttk.Frame(self.frame)
        frame.pack(fill="both", expand=True, padx=10, pady=4)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        cols   = ("읽음", "상품ID", "상품명", "도매처", "기존가격", "변동가격", "변동률", "감지시각")
        widths = (40,    120,     200,     90,      90,       90,       90,      140)
        centers = {"읽음", "도매처", "기존가격", "변동가격", "변동률"}

        self.tree = ttk.Treeview(frame, columns=cols, show="headings",
                                 height=22, selectmode="extended")
        for col, w in zip(cols, widths):
            self.tree.heading(col, text=col, anchor="w")
            self.tree.column(col, width=w, minwidth=30,
                             anchor="center" if col in centers else "w")

        self.tree.tag_configure("unread", background=_UNREAD_BG, font=("", 10, "bold"))
        self.tree.tag_configure("read",   background="",          font=("", 10))

        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(    row=0, column=1, sticky="ns")
        hsb.grid(    row=1, column=0, sticky="ew")

    def _build_bottom(self):
        bar = ttk.Frame(self.frame)
        bar.pack(fill="x", padx=10, pady=(2, 8))

        self._lbl_count = ttk.Label(bar, text="", foreground="#555")
        self._lbl_count.pack(side="left", padx=6)

        ttk.Button(bar, text="선택 읽음처리",
                   command=self._mark_selected_read).pack(side="right", padx=4)

    # ── 데이터 로드 ──────────────────────────────────────────

    def _load(self):
        alerts = (self._repo.unread() if self._unread_only.get()
                  else self._repo.all())

        self.tree.delete(*self.tree.get_children())
        for a in alerts:
            is_read  = a.get("is_read", False)
            tag      = "read" if is_read else "unread"
            self.tree.insert("", "end", iid=a["id"], values=(
                "○" if is_read else "●",
                a.get("ss_product_id", ""),
                a.get("product_name", "")[:30],
                a.get("supplier", ""),
                _fmt_price(a.get("old_price")),
                _fmt_price(a.get("new_price")),
                _fmt_rate(a.get("change_rate")),
                a.get("detected_at", "")[:16],
            ), tags=(tag,))

        unread_count = self._repo.count_unread()
        total        = len(self._repo.all())
        self._lbl_count.config(
            text=f"전체 {total}건 / 미읽음 {unread_count}건"
        )

        self.frame.after(60_000, self._load)

    # ── 읽음 처리 ────────────────────────────────────────────

    def _mark_selected_read(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("알림", "읽음 처리할 항목을 선택해 주세요.")
            return
        for iid in selected:
            self._repo.mark_read(iid)
        self._load()
        if self._on_read:
            self._on_read()

    def _mark_all_read(self):
        if not messagebox.askyesno("전체 읽음처리", "모든 가격 알림을 읽음으로 처리하시겠습니까?"):
            return
        self._repo.mark_all_read()
        self._load()
        if self._on_read:
            self._on_read()
