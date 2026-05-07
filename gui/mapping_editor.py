"""상품 매핑 설정 탭 (data/mappings.json 기반 CRUD)
가격 변동 알림 건수를 상품 행 옆에 표시
"""
import tkinter as tk
from tkinter import ttk, messagebox
from src.core.mapping_repository import MappingRepository, extract_supplier_id
from src.core.price_alert_repository import PriceAlertRepository

_repo       = MappingRepository()
_alert_repo = PriceAlertRepository()


class MappingEditorFrame:
    def __init__(self, parent):
        self.frame = ttk.Frame(parent)
        self._build()
        self._load()

    # ── UI 구성 ──────────────────────────────────────────────

    def _build(self):
        frame = ttk.Frame(self.frame)
        frame.pack(fill="both", expand=True, padx=10, pady=6)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        cols   = ("ID", "스마트스토어 상품ID", "옵션ID", "도매처", "도매처 상품ID",
                  "마진율", "활성", "가격알림")
        widths = (36, 160, 90, 100, 130, 64, 44, 64)
        centers = {"ID", "마진율", "활성", "가격알림"}

        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=20)
        for col, w in zip(cols, widths):
            self.tree.heading(col, text=col, anchor="w")
            self.tree.column(col, width=w, minwidth=24,
                             anchor="center" if col in centers else "w")

        self.tree.tag_configure("has_alert", foreground="#CC0000", font=("", 10, "bold"))

        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(    row=0, column=1, sticky="ns")
        hsb.grid(    row=1, column=0, sticky="ew")

        btn_frame = ttk.Frame(self.frame)
        btn_frame.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Button(btn_frame, text="추가",      command=self._add_dialog).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="삭제",      command=self._delete_selected).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="활성 토글", command=self._toggle_active).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="⟳ 새로고침", command=self._load).pack(side="right", padx=4)

    # ── 데이터 로드 ──────────────────────────────────────────

    def _load(self):
        self.tree.delete(*self.tree.get_children())
        # 미읽음 알림을 상품별로 집계
        all_alerts = _alert_repo.all()
        unread_by_product: dict[str, int] = {}
        for a in all_alerts:
            if not a.get("is_read", False):
                pid = a.get("ss_product_id", "")
                unread_by_product[pid] = unread_by_product.get(pid, 0) + 1

        for m in _repo.all():
            pid   = m["ss_product_id"]
            cnt   = unread_by_product.get(pid, 0)
            tags  = ("has_alert",) if cnt > 0 else ()
            self.tree.insert("", "end", iid=str(m["id"]), values=(
                m["id"],
                pid,
                m.get("ss_option_id", ""),
                m["supplier"],
                m["supplier_product_id"],
                m.get("price_margin_rate", 1.3),
                "O" if m.get("is_active", True) else "X",
                str(cnt) if cnt > 0 else "",
            ), tags=tags)

    # ── 추가 다이얼로그 ──────────────────────────────────────

    def _add_dialog(self):
        win = tk.Toplevel(self.frame)
        win.title("매핑 추가")
        win.resizable(False, False)
        win.grab_set()

        fields = [
            ("스마트스토어 상품ID *",          ""),
            ("스마트스토어 옵션ID",             ""),
            ("도매처 (domaekkuk/domaemae)",     "domaekkuk"),
        ]
        entries: dict[str, ttk.Entry] = {}
        for i, (label, default) in enumerate(fields):
            ttk.Label(win, text=label).grid(row=i, column=0, padx=12, pady=5, sticky="w")
            e = ttk.Entry(win, width=40)
            e.insert(0, default)
            e.grid(row=i, column=1, columnspan=2, padx=8, pady=5, sticky="w")
            entries[label] = e

        # 도매꾹 URL/ID 입력 + 실시간 ID 추출
        row_url = len(fields)
        ttk.Label(win, text="도매꾹 상품 URL 또는 ID *").grid(
            row=row_url, column=0, padx=12, pady=5, sticky="w")
        url_var = tk.StringVar()
        ttk.Entry(win, textvariable=url_var, width=40).grid(
            row=row_url, column=1, padx=8, pady=5, sticky="w")
        extracted_var = tk.StringVar()
        ttk.Label(win, textvariable=extracted_var, foreground="#0066cc",
                  font=("", 9)).grid(row=row_url, column=2, padx=4, sticky="w")

        def on_url_change(*_):
            raw = url_var.get().strip()
            extracted_var.set(f"→ ID: {extract_supplier_id(raw)}" if raw else "")

        url_var.trace_add("write", on_url_change)

        rest_fields = [
            ("도매처 옵션ID",       ""),
            ("마진율 (예: 1.3) *",  "1.3"),
            ("메모",                ""),
        ]
        for j, (label, default) in enumerate(rest_fields):
            r = row_url + 1 + j
            ttk.Label(win, text=label).grid(row=r, column=0, padx=12, pady=5, sticky="w")
            e = ttk.Entry(win, width=40)
            e.insert(0, default)
            e.grid(row=r, column=1, columnspan=2, padx=8, pady=5, sticky="w")
            entries[label] = e

        def save():
            ss_id    = entries["스마트스토어 상품ID *"].get().strip()
            url_raw  = url_var.get().strip()
            supplier = entries["도매처 (domaekkuk/domaemae)"].get().strip()
            if not ss_id:
                messagebox.showwarning("입력 오류", "스마트스토어 상품ID를 입력해 주세요.", parent=win)
                return
            if not url_raw:
                messagebox.showwarning("입력 오류", "도매꾹 상품 URL 또는 ID를 입력해 주세요.", parent=win)
                return
            try:
                margin = float(entries["마진율 (예: 1.3) *"].get() or 1.3)
            except ValueError:
                messagebox.showwarning("입력 오류", "마진율은 숫자로 입력해 주세요. (예: 1.3)", parent=win)
                return
            _repo.add(
                ss_product_id=ss_id,
                supplier=supplier,
                supplier_url_or_id=url_raw,
                ss_option_id=entries["스마트스토어 옵션ID"].get().strip(),
                supplier_option_id=entries["도매처 옵션ID"].get().strip(),
                price_margin_rate=margin,
                memo=entries["메모"].get().strip(),
            )
            win.destroy()
            self._load()

        save_row = row_url + 1 + len(rest_fields)
        ttk.Button(win, text="저장", command=save, width=16).grid(
            row=save_row, column=0, columnspan=3, pady=12)

    # ── 삭제 / 토글 ──────────────────────────────────────────

    def _delete_selected(self):
        selected = self.tree.selection()
        if not selected:
            return
        if not messagebox.askyesno("삭제 확인", f"{len(selected)}개 매핑을 삭제하시겠습니까?"):
            return
        for iid in selected:
            _repo.remove(int(iid))
        self._load()

    def _toggle_active(self):
        selected = self.tree.selection()
        if not selected:
            return
        for iid in selected:
            vals = self.tree.item(iid, "values")
            current_active = vals[6] == "O"
            _repo.set_active(int(iid), not current_active)
        self._load()
