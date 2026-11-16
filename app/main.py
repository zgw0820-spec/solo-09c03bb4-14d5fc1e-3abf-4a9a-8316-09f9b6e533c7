"""FastAPI 入口：网页（上传 / 校时 / 按日查询 / 处理状态）+ JSON API。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import services
from .calibration import RecalcWorker
from .config import Settings
from .csvparse import parse_csv_text
from .db import Database
from .services import ServiceError
from .timeutil import parse_utc

logging.basicConfig(level=logging.INFO)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

MAX_UPLOAD_BYTES = 2_000_000  # 2000 点远小于此，超限直接拒绝


def create_app() -> FastAPI:
    cfg = Settings.from_env()
    db = Database(cfg.db_path)
    worker = RecalcWorker(db, delay=cfg.recalc_delay)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if cfg.worker_enabled:
            worker.start()
        worker.trigger()  # 重启恢复：若有未重算的变更，启动后立即补算
        yield
        worker.stop()
        db.close()

    app = FastAPI(title="冷链记录仪校时平台", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.db = db
    app.state.worker = worker

    # ------------------------------------------------------------ 公共

    def render(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request=request, name=name, context=ctx)

    def trigger_recalc():
        """启用后台线程时异步触发；否则同步重算（便于测试与小规模部署）。"""
        if cfg.worker_enabled:
            worker.trigger()
        else:
            worker.run_until_quiet()

    def error_json(exc: ServiceError) -> JSONResponse:
        body = {"detail": exc.message, "code": exc.code}
        if exc.errors:
            body["errors"] = exc.errors
        if exc.current_version is not None or exc.code == "VERSION_CONFLICT":
            body["current_version"] = exc.current_version
        return JSONResponse(status_code=exc.status, content=body)

    async def handle_upload(file: UploadFile) -> tuple[int, int]:
        raw = await file.read()
        if len(raw) > MAX_UPLOAD_BYTES:
            raise ServiceError("文件过大（上限 2MB）", code="FILE_TOO_LARGE")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ServiceError("文件编码须为 UTF-8", code="BAD_ENCODING")
        rows, errors = parse_csv_text(text, cfg.max_points)
        if errors:
            raise ServiceError(f"CSV 校验失败（{len(errors)} 处），整批未入库",
                               code="CSV_INVALID", errors=errors)
        batch_id = services.add_batch(db, cfg, file.filename or "", rows)
        trigger_recalc()
        return batch_id, len(rows)

    def redirect(path: str, msg: str, ok: bool = True) -> RedirectResponse:
        return RedirectResponse(
            url=f"{path}?msg={quote(msg)}&ok={1 if ok else 0}", status_code=303
        )

    # ------------------------------------------------------------ 网页

    @app.get("/", response_class=HTMLResponse)
    def status_page(request: Request, msg: str = "", ok: int = 1):
        st = services.get_status(db, cfg)
        return render(request, "status.html", st=st, msg=msg, ok=ok,
                      refresh=st["calc"]["status"] == "running")

    @app.post("/recalc")
    def recalc_html():
        trigger_recalc()
        return redirect("/", "已触发重算")

    @app.get("/upload", response_class=HTMLResponse)
    def upload_page(request: Request, msg: str = "", ok: int = 1):
        return render(request, "upload.html", cfg=cfg, msg=msg, ok=ok)

    @app.post("/upload", response_class=HTMLResponse)
    async def upload_do(request: Request, file: UploadFile = File(...)):
        try:
            batch_id, n = await handle_upload(file)
        except ServiceError as exc:
            return render(request, "upload.html", cfg=cfg, error=exc, msg="", ok=1)
        return redirect("/upload", f"批次 {batch_id} 已接纳 {n} 行")

    @app.get("/anchors", response_class=HTMLResponse)
    def anchors_page(request: Request, msg: str = "", ok: int = 1,
                     segment: int | None = None):
        anchors = services.list_anchors(db)
        prefill = None
        if segment is not None:
            prefill = next((a for a in anchors if a["segment"] == segment), None)
        return render(request, "anchors.html", anchors=anchors, prefill=prefill,
                      msg=msg, ok=ok)

    @app.post("/anchors", response_class=HTMLResponse)
    async def anchors_do(request: Request):
        form = await request.form()
        try:
            seg = int(str(form.get("segment", "")).strip())
            local1 = int(str(form.get("local1", "")).strip())
            local2 = int(str(form.get("local2", "")).strip())
            utc1 = parse_utc(form.get("utc1"))
            utc2 = parse_utc(form.get("utc2"))
            v_raw = str(form.get("version", "")).strip()
            version = int(v_raw) if v_raw else None
            new_version = services.set_anchors(db, seg, local1, utc1, local2, utc2, version)
        except ValueError as exc:
            return render(request, "anchors.html", anchors=services.list_anchors(db),
                          prefill=None, error=str(exc), msg="", ok=1)
        except ServiceError as exc:
            return render(request, "anchors.html", anchors=services.list_anchors(db),
                          prefill=None, error=exc.message, msg="", ok=1)
        trigger_recalc()
        return redirect("/anchors", f"段 {seg} 锚点已保存，当前版本 v{new_version}")

    @app.get("/query", response_class=HTMLResponse)
    def query_page(request: Request, date: str = ""):
        result, error = None, None
        if date.strip():
            try:
                result = services.query_by_date(db, date)
            except ServiceError as exc:
                error = exc.message
        pending = services.pending_readings(db)
        return render(request, "query.html", date=date, result=result, error=error,
                      pending=pending[:200], pending_count=len(pending),
                      calc=services.calc_info(db), msg="", ok=1)

    # ------------------------------------------------------------ JSON API

    @app.get("/api/status")
    def api_status():
        return services.get_status(db, cfg)

    @app.post("/api/recalc")
    def api_recalc():
        trigger_recalc()
        return {"triggered": True, "calc": services.calc_info(db)}

    @app.post("/api/uploads", status_code=200)
    async def api_upload(file: UploadFile = File(...)):
        try:
            batch_id, n = await handle_upload(file)
        except ServiceError as exc:
            return error_json(exc)
        return {"batch_id": batch_id, "accepted": n}

    @app.post("/api/anchors")
    async def api_anchors(request: Request):
        try:
            body = await request.json()
        except Exception:
            return error_json(ServiceError("请求体须为 JSON", code="BAD_JSON"))
        try:
            seg = int(body["segment"])
            local1 = int(body["local1"])
            local2 = int(body["local2"])
            utc1 = parse_utc(body["utc1"])
            utc2 = parse_utc(body["utc2"])
        except KeyError as exc:
            return error_json(ServiceError(f"缺少字段 {exc}", code="MISSING_FIELD"))
        except ValueError as exc:
            return error_json(ServiceError(str(exc), code="BAD_VALUE"))
        version = body.get("version")
        if version is not None:
            try:
                version = int(version)
            except (TypeError, ValueError):
                return error_json(ServiceError("version 须为整数", code="BAD_VALUE"))
        try:
            new_version = services.set_anchors(db, seg, local1, utc1, local2, utc2, version)
        except ServiceError as exc:
            return error_json(exc)
        trigger_recalc()
        return {"segment": seg, "version": new_version}

    @app.get("/api/readings")
    def api_readings(date: str):
        try:
            return services.query_by_date(db, date)
        except ServiceError as exc:
            return error_json(exc)

    @app.get("/api/pending")
    def api_pending():
        pend = services.pending_readings(db)
        return {"count": len(pend), "pending": pend}

    return app


app = create_app()
