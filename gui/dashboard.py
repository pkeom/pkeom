"""현황 대시보드 탭"""
import tkinter as tk
from tkinter import ttk
from src.core.order_repository import OrderRepository

_repo = OrderRepository()

_STATUS_META = [
    ("NEW",      "신규 주문", "#FFF9C4"),
    ("ORDERED",  "발주 완료", "#E3F2FD"),
    ("INVOICED", "송장 등록", "#E8F5E9"),
    ("DONE",     "처리 완료", "#EDE7F6"),
    ("ERROR",    "오 류",     "#FFEBEE"),
]


class DashboardFrame:
    def __init__(self, parent):
        self.frame = ttk.Frame(parent)
        self._vars: dict[str, tk.StringVar] = {}
        self._build()
        self._refresh()

    # ── UI 구성 ──────────────────────────────────────────────

    def _build(self):
        self._build_cards()
        self._build_table()

    def _build_cards(self):
        top = tk.Frame(self.frame)
        top.pack(fill="x", padx=10, pady=(10, 2))

        for status, label, color in _STATUS_META:
            var = tk.StringVar(value="0")
            self._vars[status] = var
            card = tk.Frame(top, bg=color, relief="groove", bd=1)
            card.pack(side="left", padx=6, pady=2, ipadx=16, ipady=8)
            tk.Label(card, text=label, bg=color, font=("", 9),    fg="#555").pack()
            tk.Label(card, textvariable=var, bg=color,
                     font=("", 22, "bold"), fg="#222").pack()

        ttk.Button(top, text="⟳ 새로고침", command=self._refresh).pack(
            side="right", padx=6, pady=2)

    def _build_table(self):
        frame = ttk.Frame(self.frame)
        frame.pack(fill="both", expand=True, padx=10, pady=6)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        cols    = ("주문ID", "상품명", "상태", "수령인", "수량", "도매처", "발주번호", "수집시각")
        widths  = (170, 220, 80, 90, 50, 90, 120, 140)
        centers = {"상태", "수량"}

        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=22)
        for col, w in zip(cols, widths):
            self.tree.heading(col, text=col, anchor="w")
            self.tree.column(col, width=w, minwidth=30,
                             anchor="center" if col in centers else "w")

        for status, _, color in _STATUS_META:
            self.tree.tag_configure(status, background=color)

        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(    row=0, column=1, sticky="ns")
        hsb.grid(    row=1, column=0, sticky="ew")

    # ── 데이터 갱신 ──────────────────────────────────────────

    def _refresh(self):
        counts = _repo.count_by_status()
        for status, var in self._vars.items():
            var.set(str(counts.get(status, 0)))

        self.tree.delete(*self.tree.get_children())
        for o in _repo.all()[:300]:
            status = o.get("status", "NEW")
            self.tree.insert("", "end", values=(
                o["order_id"],
                o.get("product_name", "")[:28],
                status,
                o.get("receiver_name", ""),
                o.get("quantity", ""),
                o.get("supplier", ""),
                o.get("supplier_order_no", ""),
                o.get("collected_at", "")[:16],
            ), tags=(status,))

        self.frame.after(60_000, self._refresh)
