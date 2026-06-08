"""상품 자동 등록 탭 GUI"""
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from src.utils.config_loader import load_config
from src.api.smartstore import SmartstoreAPI
from src.api.domaemae import DomaemaeClient
from src.core.product_register import (
    fetch_product_info,
    calculate_selling_price,
    map_category,
    register_product,
)
from src.core.mapping_repository import MappingRepository

_mapping_repo = MappingRepository()


def _build_clients():
    cfg = load_config()
    ss_cfg  = {k: v for k, v in cfg["smartstore"].items()
               if k in ("client_id", "client_secret", "account_type")}
    ss_api  = SmartstoreAPI(**ss_cfg)
    dm_cfg  = cfg.get("domaemae", {})
    client  = DomaemaeClient(
        api_key  = dm_cfg.get("api_key") or cfg["domaekkuk"].get("api_key", ""),
        user_id  = dm_cfg.get("user_id", ""),
        password = dm_cfg.get("password", ""),
    )
    return ss_api, client, cfg


class ProductRegisterFrame:
    def __init__(self, parent):
        self.frame = ttk.Frame(parent)
        self._info: dict | None = None  # 미리보기로 수집한 상품 정보
        self._build()

    # ── UI 구성 ──────────────────────────────────────────────────────

    def _build(self):
        self._build_input_panel()
        self._build_preview_panel()
        self._build_status_bar()

    def _build_input_panel(self):
        pane = ttk.LabelFrame(self.frame, text="상품 등록 설정", padding=10)
        pane.pack(fill="x", padx=10, pady=(10, 4))

        # URL 입력
        ttk.Label(pane, text="도매매 / 도매꾹 상품 링크:").grid(
            row=0, column=0, sticky="w", pady=3)
        self._url_var = tk.StringVar()
        url_entry = ttk.Entry(pane, textvariable=self._url_var, width=70)
        url_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=3)

        # 마진율
        ttk.Label(pane, text="마진율 (%):").grid(row=1, column=0, sticky="w", pady=3)
        cfg = self._safe_load_cfg()
        default_margin = int(cfg.get("default_margin", 0.3) * 100)
        self._margin_var = tk.IntVar(value=default_margin)
        ttk.Spinbox(pane, from_=1, to=90, textvariable=self._margin_var,
                    width=8).grid(row=1, column=1, sticky="w", padx=(6, 0), pady=3)
        ttk.Label(pane, text="  (기본 30% — 원가 ÷ (1-마진율) = 판매가, 100원 단위 올림)",
                  foreground="#666").grid(row=1, column=2, sticky="w", pady=3)

        # 카테고리 ID (선택)
        ttk.Label(pane, text="스마트스토어 카테고리 ID:").grid(
            row=2, column=0, sticky="w", pady=3)
        self._cat_var = tk.StringVar(value=cfg.get("default_category_id", ""))
        ttk.Entry(pane, textvariable=self._cat_var, width=20).grid(
            row=2, column=1, sticky="w", padx=(6, 0), pady=3)
        ttk.Label(pane, text="  (비워두면 카테고리 자동 감지)", foreground="#666").grid(
            row=2, column=2, sticky="w", pady=3)

        pane.columnconfigure(1, weight=1)

        # 버튼
        btn_row = ttk.Frame(pane)
        btn_row.grid(row=3, column=0, columnspan=4, pady=(8, 2), sticky="w")
        ttk.Button(btn_row, text="미리보기", command=self._on_preview, width=14).pack(
            side="left", padx=(0, 8))
        self._register_btn = ttk.Button(
            btn_row, text="스마트스토어 등록", command=self._on_register,
            width=18, state="disabled")
        self._register_btn.pack(side="left")

    def _build_preview_panel(self):
        pane = ttk.LabelFrame(self.frame, text="상품 미리보기", padding=10)
        pane.pack(fill="both", expand=True, padx=10, pady=4)

        cols  = ("항목", "내용")
        self._tree = ttk.Treeview(pane, columns=cols, show="headings", height=14)
        self._tree.heading("항목", text="항목", anchor="w")
        self._tree.heading("내용", text="내용", anchor="w")
        self._tree.column("항목", width=150, minwidth=100)
        self._tree.column("내용", width=700, minwidth=200)

        vsb = ttk.Scrollbar(pane, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)

        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def _build_status_bar(self):
        bar = ttk.Frame(self.frame)
        bar.pack(fill="x", padx=10, pady=(0, 8))
        self._status_var = tk.StringVar(value="URL을 입력하고 [미리보기]를 클릭하세요.")
        ttk.Label(bar, textvariable=self._status_var, foreground="#444",
                  font=("", 9), anchor="w").pack(fill="x")

    # ── 이벤트 핸들러 ─────────────────────────────────────────────────

    def _on_preview(self):
        url = self._url_var.get().strip()
        if not url:
            messagebox.showwarning("입력 오류", "도매매/도매꾹 링크를 입력해주세요.")
            return
        self._set_status("상품 정보 수집 중...")
        self._register_btn.config(state="disabled")
        threading.Thread(target=self._do_preview, args=(url,), daemon=True).start()

    def _do_preview(self, url: str):
        try:
            _, client, _ = _build_clients()
            info = fetch_product_info(url, client)
            self._info = info
            margin = self._margin_var.get() / 100
            selling_price = calculate_selling_price(
                info["supply_price"], margin=margin) if info["supply_price"] else 0

            self.frame.after(0, lambda: self._show_preview(info, selling_price))
        except Exception as e:
            self.frame.after(0, lambda: self._set_status(f"오류: {e}", error=True))

    def _show_preview(self, info: dict, selling_price: int):
        self._tree.delete(*self._tree.get_children())
        margin = self._margin_var.get() / 100

        rows = [
            ("공급사",      info.get("supplier", "")),
            ("상품명",      info.get("title", "")),
            ("공급가",      f"{info.get('supply_price', 0):,}원"),
            ("원가 (공급가+배송비)", f"{info.get('supply_price', 0) + 3000:,}원"),
            ("판매가",      f"{selling_price:,}원  (마진율 {int(margin*100)}%)"),
            ("재고",        f"{info.get('stock', 0):,}개"),
            ("원산지",      info.get("origin", "")),
            ("모델명",      info.get("model", "")),
            ("카테고리",    info.get("category_name", "")),
            ("대표이미지",  info.get("main_image", "(없음)")),
            ("추가이미지",  f"{len(info.get('sub_images', []))}장"),
            ("상세이미지",  f"{len(info.get('detail_images', []))}장"),
            ("옵션 수",     str(len(info.get("options", [])))),
        ]

        rows.append(("KC인증번호", info.get("kc_cert_no", "(없음)")))

        # 옵션 목록
        for i, opt in enumerate(info.get("options", [])[:20], 1):
            rows.append((f"  옵션 {i}", opt["name"]))

        for item, val in rows:
            self._tree.insert("", "end", values=(item, val))

        if info.get("title") and info.get("supply_price"):
            self._register_btn.config(state="normal")
            self._set_status("미리보기 완료. [스마트스토어 등록] 버튼을 클릭하세요.")
        else:
            self._set_status("상품명 또는 공급가를 가져오지 못했습니다.", error=True)

    def _on_register(self):
        if self._info is None:
            messagebox.showwarning("오류", "먼저 [미리보기]를 실행해주세요.")
            return

        title = self._info.get("title", "")
        price_info = (f"{self._info.get('supply_price', 0) + 3000:,}원 → "
                      f"{calculate_selling_price(self._info['supply_price'], margin=self._margin_var.get()/100):,}원")
        if not messagebox.askyesno(
            "등록 확인",
            f"다음 상품을 스마트스토어에 등록하시겠습니까?\n\n"
            f"상품명: {title}\n"
            f"판매가: {price_info}\n\n"
            f"※ 실제 API 호출이 발생합니다."
        ):
            return

        self._register_btn.config(state="disabled")
        self._set_status("스마트스토어 등록 중...")
        margin    = self._margin_var.get() / 100
        cat_id    = self._cat_var.get().strip()
        info_snap = self._info.copy()
        threading.Thread(
            target=self._do_register,
            args=(info_snap, margin, cat_id),
            daemon=True,
        ).start()

    def _do_register(self, info: dict, margin: float, category_id: str):
        try:
            ss_api, client, cfg = _build_clients()
            url = info["supplier_url"]
            selling_price = calculate_selling_price(
                info["supply_price"], margin=margin
            )
            result = register_product(
                url             = url,
                selling_price   = selling_price,
                smartstore_api  = ss_api,
                supplier_client = client,
                settings        = cfg,
                mapping_repo    = _mapping_repo,
                category_id     = category_id,
            )
            self.frame.after(0, lambda: self._on_register_done(result))
        except Exception as e:
            self.frame.after(0, lambda: self._set_status(f"등록 오류: {e}", error=True))
            self.frame.after(0, lambda: self._register_btn.config(state="normal"))

    def _on_register_done(self, result: dict):
        if result.get("success"):
            pid   = result.get("product_id", "")
            price = result.get("selling_price", 0)
            messagebox.showinfo(
                "등록 완료",
                f"스마트스토어 상품 등록 완료!\n\n"
                f"상품 ID: {pid}\n"
                f"판매가: {price:,}원\n"
                f"매핑이 자동 저장되었습니다."
            )
            self._set_status(f"등록 완료 — 상품 ID: {pid}, 판매가: {price:,}원")
            self._info = None
            self._register_btn.config(state="disabled")
        else:
            err = result.get("error", "알 수 없는 오류")
            messagebox.showerror("등록 실패", f"스마트스토어 등록 실패:\n\n{err}")
            self._set_status(f"등록 실패: {err}", error=True)
            self._register_btn.config(state="normal")

    # ── 유틸 ─────────────────────────────────────────────────────────

    def _set_status(self, msg: str, error: bool = False):
        self._status_var.set(msg)

    @staticmethod
    def _safe_load_cfg() -> dict:
        try:
            return load_config()
        except Exception:
            return {}
