"""스마트스토어 위탁판매 자동화 — 웹 대시보드 (Flask, 포트 2713)

실행:
    python dashboard.py          # 단독 실행
    main.py 에서 스레드로 자동 시작
"""
import json
import logging
import sys
from datetime import datetime, date
from pathlib import Path

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, str(Path(__file__).parent))

from src.utils.config_loader import load_config
from src.core.order_repository import OrderRepository
from src.core.mapping_repository import MappingRepository, extract_supplier_id
from src.core.budget_manager import BudgetManager
from src.core.price_alert_repository import PriceAlertRepository
from src.core.stock_pending_repository import StockPendingRepository
from src.api.smartstore import SmartstoreAPI
from src.api.domaekkuk import DomaekkukAPI
from src.api.domaemae import DomaemaeClient

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# ── 전역 인스턴스 ─────────────────────────────────────────────────
_order_repo    = OrderRepository()
_mapping_repo  = MappingRepository()
_price_alerts  = PriceAlertRepository()
_stock_pending = StockPendingRepository()
_cfg     = None
_ss_api  = None
_dk_api  = None
_dm_cli  = None
_budget  = None


def _init():
    global _cfg, _ss_api, _dk_api, _dm_cli, _budget
    try:
        _cfg = load_config()
        ss   = _cfg["smartstore"]
        _ss_api = SmartstoreAPI(
            client_id     = ss["client_id"],
            client_secret = ss["client_secret"],
            account_type  = ss.get("account_type", "SELF"),
        )
        _dk_api = DomaekkukAPI(**_cfg["domaekkuk"])
        _dm     = _cfg.get("domaemae", {})
        _dm_cli = DomaemaeClient(
            api_key  = _dm.get("api_key") or _cfg["domaekkuk"].get("api_key", ""),
            user_id  = _dm.get("user_id", ""),
            password = _dm.get("password", ""),
        )
        ba      = _cfg.get("budget", 0)
        _budget = BudgetManager(initial_balance=ba if ba > 0 else 0)
    except Exception as e:
        logger.warning("[dashboard] 초기화 오류: %s", e)


_init()


# ── 헬퍼 ─────────────────────────────────────────────────────────

def _read_json(path: str | Path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _is_today(dt_str: str) -> bool:
    try:
        return datetime.fromisoformat(dt_str).date() == date.today()
    except Exception:
        return False


def _is_running() -> bool:
    """main.py 프로세스가 실행 중인지 확인 (Windows / Linux / Android 공통)"""
    try:
        import psutil
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                name    = p.info.get("name") or ""
                cmdline = p.info.get("cmdline") or []
                if "main.py" in name:
                    return True
                if any("main.py" in c for c in cmdline):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess, OSError):
                continue
    except Exception:
        pass
    return False


# ── 라우트 ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── 요약 ──────────────────────────────────────────────────────────

@app.route("/api/summary")
def api_summary():
    orders = _order_repo.all()
    today  = [o for o in orders if _is_today(o.get("collected_at", ""))]
    counts = _order_repo.count_by_status()

    returns_data = _read_json("data/returns.json", {"returns": []})
    stock_cache  = _read_json("data/stock_cache.json", {"status": {}})
    out_of_stock = sum(1 for v in stock_cache.get("status", {}).values() if not v)

    return jsonify({
        "balance":           _budget.get_balance() if _budget else 0,
        "today_orders":      len(today),
        "ordered_count":     counts.get("ORDERED", 0),
        "pending_count":     counts.get("PENDING", 0) + counts.get("STOCK_PENDING", 0),
        "out_of_stock":      out_of_stock,
        "return_count":      len(returns_data.get("returns", [])),
        "price_alert_count": _price_alerts.count_unread(),
        "system_running":    _is_running(),
        "order_counts":      counts,
    })


# ── 주문 ──────────────────────────────────────────────────────────

@app.route("/api/orders")
def api_orders():
    status = request.args.get("status", "")
    orders = _order_repo.all()
    if status:
        orders = [o for o in orders if o.get("status") == status]
    return jsonify(orders)


# ── 매핑 ──────────────────────────────────────────────────────────

@app.route("/api/mappings")
def api_mappings():
    return jsonify(_mapping_repo.all())


@app.route("/api/mappings", methods=["POST"])
def api_add_mapping():
    d = request.json or {}
    try:
        entry = _mapping_repo.add(
            ss_product_id      = d["ss_product_id"],
            supplier           = d["supplier"],
            supplier_url_or_id = d["supplier_url_or_id"],
            ss_option_id       = d.get("ss_option_id", ""),
            supplier_option_id = d.get("supplier_option_id", ""),
            price_margin_rate  = float(d.get("price_margin_rate", 1.3)),
            memo               = d.get("memo", ""),
        )
        return jsonify(entry), 201
    except KeyError as e:
        return jsonify({"error": f"필수 항목 누락: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/mappings/<int:mid>", methods=["DELETE"])
def api_delete_mapping(mid):
    return jsonify({"ok": _mapping_repo.remove(mid)})


@app.route("/api/mappings/<int:mid>/toggle", methods=["PATCH"])
def api_toggle_mapping(mid):
    _mapping_repo.set_active(mid, bool((request.json or {}).get("is_active", True)))
    return jsonify({"ok": True})


@app.route("/api/mappings/<int:mid>/memo", methods=["PATCH"])
def api_mapping_memo(mid):
    _mapping_repo.update_memo(mid, (request.json or {}).get("memo", ""))
    return jsonify({"ok": True})


# ── 예산 ──────────────────────────────────────────────────────────

@app.route("/api/budget")
def api_budget():
    if not _budget:
        return jsonify({"current_balance": 0, "total_spent": 0, "total_charged": 0, "history": []})
    return jsonify(_budget.get_data())


@app.route("/api/budget/charge", methods=["POST"])
def api_charge():
    d      = request.json or {}
    amount = int(d.get("amount", 0))
    if amount <= 0:
        return jsonify({"error": "금액을 입력하세요"}), 400
    if not _budget:
        return jsonify({"error": "예산 관리가 비활성화 상태입니다"}), 400
    balance = _budget.charge(amount, d.get("reason", "대시보드 충전"))
    return jsonify({"balance": balance})


# ── 가격 알림 ─────────────────────────────────────────────────────

@app.route("/api/price-alerts")
def api_price_alerts():
    return jsonify(_price_alerts.all())


@app.route("/api/price-alerts/<aid>/read", methods=["PATCH"])
def api_mark_read(aid):
    return jsonify({"ok": _price_alerts.mark_read(aid)})


@app.route("/api/price-alerts/read-all", methods=["POST"])
def api_mark_all_read():
    _price_alerts.mark_all_read()
    return jsonify({"ok": True})


# ── 반품 ──────────────────────────────────────────────────────────

@app.route("/api/returns")
def api_returns():
    data    = _read_json("data/returns.json", {"returns": []})
    returns = sorted(data.get("returns", []),
                     key=lambda r: r.get("detected_at", ""), reverse=True)
    return jsonify(returns)


# ── 도매처 상품 조회 ──────────────────────────────────────────────

@app.route("/api/supplier/product")
def api_supplier_product():
    url      = request.args.get("url", "").strip()
    supplier = request.args.get("supplier", "domaekkuk")
    pid      = extract_supplier_id(url)
    if not pid:
        return jsonify({"error": "상품 ID를 추출할 수 없습니다"}), 400
    try:
        if supplier == "domaekkuk":
            if not _dk_api:
                return jsonify({"error": "도매꾹 API 미초기화"}), 500
            p = _dk_api.get_product(pid)
        else:
            if not _dm_cli:
                return jsonify({"error": "도매매 API 미초기화"}), 500
            p = _dm_cli.get_product(pid)
        return jsonify({"product_id": pid, **p})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 상품 자동 등록 ───────────────────────────────────────────────

@app.route("/api/register/preview", methods=["POST"])
def api_register_preview():
    from src.core.product_register import fetch_product_info, calculate_selling_price
    d      = request.json or {}
    url    = d.get("url", "").strip()
    margin = float(d.get("margin", 0.3))
    if margin >= 1.0:
        margin = margin / 100
    margin = max(0.01, min(0.99, margin))
    if not url:
        return jsonify({"error": "URL을 입력하세요"}), 400
    if not _dm_cli:
        return jsonify({"error": "도매매/도매꾹 API 미초기화"}), 500
    try:
        info          = fetch_product_info(url, _dm_cli)
        selling_price = calculate_selling_price(info["supply_price"], margin=margin) if info["supply_price"] else 0
        return jsonify({**info, "selling_price": selling_price, "cost": info["supply_price"] + 3000})
    except Exception as e:
        import traceback
        logger.error("register preview 오류: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/register/submit", methods=["POST"])
def api_register_submit():
    from src.core.product_register import register_product
    d           = request.json or {}
    url         = d.get("url", "").strip()
    margin      = float(d.get("margin", 0.3))
    if margin >= 1.0:
        margin = margin / 100
    margin      = max(0.01, min(0.99, margin))
    category_id = d.get("category_id", "").strip()
    if not url:
        return jsonify({"error": "URL을 입력하세요"}), 400
    if not _ss_api or not _dm_cli:
        return jsonify({"error": "스마트스토어/도매처 API 미초기화"}), 500
    try:
        result = register_product(
            url             = url,
            margin          = margin,
            smartstore_api  = _ss_api,
            supplier_client = _dm_cli,
            settings        = _cfg or {},
            mapping_repo    = _mapping_repo,
            category_id     = category_id,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500


# ── 스마트스토어 상품 ──────────────────────────────────────────────

@app.route("/api/smartstore/products")
def api_ss_products():
    if not _ss_api:
        return jsonify({"error": "스마트스토어 API 미초기화"}), 500
    try:
        return jsonify(_ss_api.get_products())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/smartstore/product/<pid>")
def api_ss_product(pid):
    if not _ss_api:
        return jsonify({"error": "스마트스토어 API 미초기화"}), 500
    try:
        return jsonify(_ss_api.get_product(pid))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 진입점 ────────────────────────────────────────────────────────

def run_dashboard(host: str = "0.0.0.0", port: int = 2713):
    """대시보드 서버 실행 (별도 스레드에서 호출)"""
    wz_log = logging.getLogger("werkzeug")
    wz_log.setLevel(logging.WARNING)
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    print("대시보드: http://localhost:2713", flush=True)
    run_dashboard()
