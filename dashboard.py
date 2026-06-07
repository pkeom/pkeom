"""스마트스토어 위탁판매 자동화 — 웹 대시보드 (Flask, 포트 2713)

실행:
    python dashboard.py          # 단독 실행
    main.py 에서 스레드로 자동 시작
"""
import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, date
from pathlib import Path

import requests as _req
from flask import Flask, jsonify, render_template, request, send_from_directory

sys.path.insert(0, str(Path(__file__).parent))

# ── .env 로드 (python-dotenv 없이 직접 파싱) ─────────────────────────
def _load_dotenv(path: Path) -> None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

_load_dotenv(Path(__file__).parent / ".env")
_TABLET_API = os.getenv("TABLET_API_URL", "").rstrip("/")

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

# ── 태블릿 API 프록시 ─────────────────────────────────────────────────
# TABLET_API_URL이 .env에 설정된 경우: GET /api/* 요청을 태블릿으로 전달
# 설정 없으면: 로컬 orders.json / 각 엔드포인트 직접 처리
@app.before_request
def _tablet_proxy():
    if not _TABLET_API:
        return None
    if not request.path.startswith("/api/"):
        return None
    if request.method != "GET":
        return None
    # 매핑은 git으로 동기화 — 로컬 파일 직접 읽기
    if request.path.startswith("/api/mappings"):
        return None
    url = f"{_TABLET_API}{request.path}"
    try:
        r = _req.get(url, params=request.args, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        logger.warning("태블릿 API 프록시 실패 (%s): %s", url, e)
        return jsonify({"error": f"태블릿 연결 실패: {e}"}), 502


def _git_push_mappings() -> None:
    """mappings.json 변경을 백그라운드 스레드에서 git add/commit/push."""
    threading.Thread(target=_do_git_push, daemon=True).start()


def _do_git_push() -> None:
    import subprocess
    from datetime import datetime as _dt
    repo = str(_BASE_DIR)
    try:
        subprocess.run(["git", "add", "data/mappings.json"],
                       cwd=repo, check=True, capture_output=True, timeout=30)
        # 스테이징 변경 없으면 커밋·푸시 스킵
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"],
                              cwd=repo, capture_output=True, timeout=10)
        if diff.returncode == 0:
            logger.info("mappings.json: 변경 없음, 푸시 스킵")
            return
        msg = f"chore: 매핑 업데이트 {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}"
        subprocess.run(["git", "commit", "-m", msg],
                       cwd=repo, check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "push"],
                       cwd=repo, check=True, capture_output=True, timeout=60)
        logger.info("mappings.json GitHub 푸시 완료")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="replace")
        stdout = (e.stdout or b"").decode("utf-8", errors="replace")
        logger.warning("mappings.json 푸시 실패: %s%s", stderr, stdout)
    except Exception as e:
        logger.warning("mappings.json 푸시 오류: %s", e)

# ── 전역 인스턴스 (절대경로 — CWD에 무관하게 같은 파일 참조) ─────────
_BASE_DIR      = Path(__file__).parent
_order_repo    = OrderRepository(_BASE_DIR / "data" / "orders.json")
_mapping_repo  = MappingRepository(_BASE_DIR / "data" / "mappings.json")
_price_alerts  = PriceAlertRepository(_BASE_DIR / "data" / "price_alerts.json")
_stock_pending = StockPendingRepository(_BASE_DIR / "data" / "stock_pending.json")
_cfg     = None
_ss_api  = None
_dk_api  = None
_dm_cli  = None
_budget  = None
_video_jobs: dict = {}


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

    returns_data      = _read_json("data/returns.json",       {"returns": []})
    cancellations_data = _read_json("data/cancellations.json", {"cancellations": []})
    confirm_data       = _read_json("data/confirm_log.json",   {"confirms": []})
    stock_cache        = _read_json("data/stock_cache.json",   {"status": {}})
    out_of_stock = sum(1 for v in stock_cache.get("status", {}).values() if not v)
    today_confirmed = sum(
        1 for c in confirm_data.get("confirms", [])
        if _is_today(c.get("confirmed_at", ""))
    )

    # 미처리 취소 건 (RETURN_GUIDE: 반품 안내 필요, APPROVE_FAILED: 수동 처리 필요)
    all_cancels   = cancellations_data.get("cancellations", [])
    _PENDING_CANCEL_STATES = {
        # 신규 상태 — 처리 중 또는 수동 처리 필요
        "DENY_SENT", "URGENT_3DAY", "REJECTED_WAIT_SHIP",
        "DENY_FAILED", "MANUAL_4DAY", "RACE_CONDITION", "MANUAL_REQUIRED",
        "SHIPPED_REJECT", "REJECTED",
        # 취소 철회 — 모니터링 중 또는 수동 처리 필요
        "BUYER_WITHDREW_DENY_PENDING", "BUYER_WITHDREW_DENY_MANUAL",
        "BUYER_WITHDREW_REORDER_FAILED",
        # 구버전 호환
        "RETURN_GUIDE", "APPROVE_FAILED", "SUPPLIER_CANCEL_FAILED",
    }
    pending_cancel = sum(
        1 for c in all_cancels
        if c.get("cancel_state") in _PENDING_CANCEL_STATES
        or c.get("result") in _PENDING_CANCEL_STATES
    )

    from src.core.budget_sale_guard import is_suspended as _budget_suspended
    budget_suspended = _budget_suspended()

    return jsonify({
        "balance":           _budget.get_balance() if _budget else 0,
        "budget_suspended":  budget_suspended,
        "today_orders":      len(today),
        "ordered_count":     counts.get("ORDERED", 0),
        "pending_count":     counts.get("PENDING", 0) + counts.get("STOCK_PENDING", 0),
        "out_of_stock":      out_of_stock,
        "return_count":      len(returns_data.get("returns", [])),
        "cancel_count":      len(all_cancels),
        "cancel_pending":    pending_cancel,
        "today_confirmed":   today_confirmed,
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
        _git_push_mappings()
        return jsonify(entry), 201
    except KeyError as e:
        return jsonify({"error": f"필수 항목 누락: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/mappings/<int:mid>", methods=["DELETE"])
def api_delete_mapping(mid):
    ok = _mapping_repo.remove(mid)
    _git_push_mappings()
    return jsonify({"ok": ok})


@app.route("/api/mappings/<int:mid>/toggle", methods=["PATCH"])
def api_toggle_mapping(mid):
    d = request.json or {}
    _mapping_repo.set_active(mid, bool(d.get("is_active", True)))
    _git_push_mappings()
    return jsonify({"ok": True})


@app.route("/api/mappings/<int:mid>/memo", methods=["PATCH"])
def api_mapping_memo(mid):
    d = request.json or {}
    _mapping_repo.update_memo(mid, d.get("memo", ""))
    _git_push_mappings()
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
    # 예산 충전 후 판매중지 복구
    if _ss_api and _mapping_repo:
        try:
            from src.core.budget_sale_guard import restore_all
            from src.notifications.email_notifier import EmailNotifier
            notifier = None
            try:
                email_cfg = (_cfg or {}).get("email", {})
                if email_cfg.get("smtp_host") and email_cfg.get("sender") and email_cfg.get("recipients"):
                    notifier = EmailNotifier(**email_cfg)
            except Exception:
                pass
            restore_all(_ss_api, _mapping_repo, notifier)
        except Exception as e:
            logger.warning("예산 충전 후 판매재개 처리 실패: %s", e)
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


# ── 발주확인 로그 ─────────────────────────────────────────────────

@app.route("/api/confirm-log")
def api_confirm_log():
    data  = _read_json("data/confirm_log.json", {"confirms": []})
    items = sorted(
        data.get("confirms", []),
        key=lambda c: c.get("confirmed_at", ""),
        reverse=True,
    )
    return jsonify(items[:200])


# ── 취소 요청 ─────────────────────────────────────────────────────

@app.route("/api/cancellations")
def api_cancellations():
    data  = _read_json("data/cancellations.json", {"cancellations": []})
    items = sorted(
        data.get("cancellations", []),
        key=lambda c: c.get("detected_at", ""),
        reverse=True,
    )
    return jsonify(items)


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
    from src.core.product_register import fetch_product_info
    d             = request.json or {}
    url           = d.get("url", "").strip()
    selling_price = int(d.get("selling_price", 0))
    product_name  = d.get("product_name", "").strip()
    if not url:
        return jsonify({"error": "URL을 입력하세요"}), 400
    if not _dm_cli:
        return jsonify({"error": "도매매/도매꾹 API 미초기화"}), 500
    try:
        info = fetch_product_info(url, _dm_cli)
        cost = (info["supply_price"] or 0) + 3000
        if not selling_price:
            selling_price = cost
        return jsonify({**info, "selling_price": selling_price, "cost": cost,
                        "product_name": product_name})
    except Exception as e:
        import traceback
        logger.error("register preview 오류: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/register/submit", methods=["POST"])
def api_register_submit():
    from src.core.product_register import register_product
    d             = request.json or {}
    url           = d.get("url", "").strip()
    selling_price = int(d.get("selling_price", 0))
    category_id   = d.get("category_id", "").strip()
    product_name  = d.get("product_name", "").strip()
    if not url:
        return jsonify({"error": "URL을 입력하세요"}), 400
    if not _ss_api or not _dm_cli:
        return jsonify({"error": "스마트스토어/도매처 API 미초기화"}), 500
    try:
        result = register_product(
            url             = url,
            selling_price   = selling_price,
            smartstore_api  = _ss_api,
            supplier_client = _dm_cli,
            settings        = _cfg or {},
            mapping_repo    = _mapping_repo,
            category_id     = category_id,
            product_name    = product_name,
        )
        if result.get("success"):
            _git_push_mappings()
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


# ── 영상 제작 ─────────────────────────────────────────────────────

@app.route("/api/video/generate", methods=["POST"])
def api_video_generate():
    from src.core.video_maker import generate_video, open_in_capcut

    bg_image   = request.files.get("bg_image")
    audio      = request.files.get("audio")
    script     = request.form.get("script", "").strip()
    subtitle_x = float(request.form.get("subtitle_x", 0))
    subtitle_y = float(request.form.get("subtitle_y", 1000))

    if not bg_image or not audio or not script:
        return jsonify({"error": "배경 이미지, 대본, 음악 파일이 모두 필요합니다"}), 400

    job_id     = str(uuid.uuid4())[:8]
    upload_dir = Path("data/video_uploads") / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    bg_path    = upload_dir / (bg_image.filename or "bg.jpg")
    audio_path = upload_dir / (audio.filename or "audio.mp3")
    bg_image.save(str(bg_path))
    audio.save(str(audio_path))

    _video_jobs[job_id] = {"status": "processing", "message": "Whisper AI 음악 분석 중…"}

    def _worker():
        try:
            out, capcut_dir = generate_video(
                bg_image_path  = str(bg_path),
                script_text    = script,
                audio_path     = str(audio_path),
                output_dir     = "data/video_output",
                output_filename = f"draft_{job_id}.mp4",
                subtitle_x     = subtitle_x,
                subtitle_y     = subtitle_y,
            )
            opened = open_in_capcut(capcut_dir)
            _video_jobs[job_id] = {
                "status":        "done",
                "path":          out,
                "filename":      f"draft_{job_id}.mp4",
                "opened":        opened,
                "capcut_project": capcut_dir,
            }
        except Exception as e:
            import traceback
            logger.error("영상 생성 실패: %s\n%s", e, traceback.format_exc())
            _video_jobs[job_id] = {"status": "error", "message": str(e)}

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/video/status/<job_id>")
def api_video_status(job_id):
    return jsonify(_video_jobs.get(job_id, {"status": "not_found"}))


@app.route("/api/video/download/<filename>")
def api_video_download(filename):
    return send_from_directory(
        str(Path("data/video_output").resolve()),
        filename,
        as_attachment=True,
        mimetype="video/mp4",
    )


# ── 진입점 ────────────────────────────────────────────────────────

def run_dashboard(host: str = "0.0.0.0", port: int = 2713):
    """대시보드 서버 실행 (별도 스레드에서 호출)"""
    wz_log = logging.getLogger("werkzeug")
    wz_log.setLevel(logging.WARNING)
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


def _setup_logging():
    from logging.handlers import RotatingFileHandler

    log_cfg  = (_cfg or {}).get("logging", {})
    log_dir  = Path(log_cfg.get("log_dir", "data/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    fmt   = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)

    # 파일 핸들러 (RotatingFileHandler)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fh = RotatingFileHandler(
            log_dir / "app.log",
            maxBytes    = log_cfg.get("max_bytes", 10_485_760),
            backupCount = log_cfg.get("backup_count", 5),
            encoding    = "utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # 콘솔 핸들러 (stderr) — RotatingFileHandler는 StreamHandler 서브클래스이므로 exact type 체크
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logger.info("로깅 초기화 완료 (level=%s, file=%s)", log_cfg.get("level", "INFO"), log_dir / "app.log")


if __name__ == "__main__":
    import os
    # cmd 어디서 실행하든 경로가 올바르도록 스크립트 위치로 작업 디렉토리 고정
    os.chdir(Path(__file__).parent)
    _setup_logging()
    # 경로 검증 로그 — 실제 해석 경로 확인용
    _orders_path = (_BASE_DIR / "data" / "orders.json").resolve()
    logger.info("[경로확인] orders.json 읽기 경로: %s (존재: %s)", _orders_path, _orders_path.exists())
    print(f"[경로확인] orders.json 읽기 경로: {_orders_path}", flush=True)
    if _TABLET_API:
        print(f"[태블릿 연동] GET /api/* → {_TABLET_API}", flush=True)
        logger.info("[태블릿 연동] GET /api/* → %s", _TABLET_API)
    else:
        print("[로컬 모드] TABLET_API_URL 미설정 — 로컬 데이터 사용", flush=True)
    print("대시보드 시작: http://localhost:2713  (종료: Ctrl+C)", flush=True)
    try:
        run_dashboard()
    except KeyboardInterrupt:
        print("\n대시보드 종료.", flush=True)
