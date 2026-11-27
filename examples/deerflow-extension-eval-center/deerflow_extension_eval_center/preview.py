"""Loopback-only preview of the real UI and offline evaluation service."""

from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from deerflow_extension_api.agent_runs import AgentRunError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .service import EvaluationService

_HOSTS = frozenset(("localhost", "localhost:2027", "127.0.0.1", "127.0.0.1:2027"))
_ACTIONS = frozenset(
    (
        "dataset_get",
        "dataset_list",
        "dataset_import",
        "experiment_compare",
        "experiment_start",
        "experiment_list",
        "experiment_get",
        "experiment_cancel",
        "report_export",
    )
)
_ASSETS = {"index.mjs": "text/javascript", "styles.css": "text/css"}


def create_app(storage_path: str | None = None) -> FastAPI:
    """Create a local preview; the process must be bound to 127.0.0.1:2027."""
    package = Path(__file__).resolve().parent
    path = Path(storage_path).resolve() if storage_path else package.parent / "local-data" / "preview.sqlite"
    service = EvaluationService(str(path))
    csrf = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    context = SimpleNamespace(principal=SimpleNamespace(user_id="local-preview"), interaction="session", agent_runs=None)

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await service.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.evaluation_service = service
    app.state.ready = False

    @app.middleware("http")
    async def loopback_only(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        origin = request.headers.get("origin")
        if host not in _HOSTS or (origin is not None and origin != f"http://{host}"):
            return JSONResponse({"detail": "Local preview origin required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'nonce-{nonce}'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/health")
    async def health():
        return {"ready": app.state.ready, "mode": "offline-preview"}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'><title>Agent 评测 · 离线预览</title>"
            f"<style nonce='{nonce}'>body{{margin:0;font:14px/1.6 system-ui,sans-serif;color:#202124;background:#fafafa}}"
            "aside{padding:12px 24px;border-bottom:1px solid #ddd;background:#fff}"
            "#app .run-controls .primary{display:none}</style></head><body>"
            "<aside>本机离线预览 · 预置响应用于验证流程与评分，不代表模型成绩。真实 Agent 仅在 DeerFlow 宿主中开放。</aside>"
            f"<div id='app'></div><script type='module' nonce='{nonce}'>"
            "import { mountEvaluation } from '/assets/index.mjs';\n"
            f"const csrf = {json.dumps(csrf)};\n"
            "const lifetime = new AbortController();\n"
            "const mounted = mountEvaluation(document.getElementById('app'), { locale:'zh-CN', "
            "settings:{preview_offline_only:true}, signal:lifetime.signal,\n"
            "async callBackend(name, payload={}) {\n"
            "const response = await fetch('/actions/' + encodeURIComponent(name), {method:'POST',\n"
            "headers:{'Content-Type':'application/json','X-Eval-Preview-CSRF':csrf},\n"
            "body:JSON.stringify(payload),signal:lifetime.signal});\n"
            "const data = await response.json();\n"
            "if(!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '操作未完成');\n"
            "return data; }});\n"
            "window.addEventListener('pagehide',()=>{lifetime.abort();mounted.dispose();},{once:true});\n"
            "</script></body></html>"
        )

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in _ASSETS:
            raise HTTPException(404, "Unknown preview asset")
        return FileResponse(package / "static" / name, media_type=_ASSETS[name])

    @app.post("/actions/{name}")
    async def action(name: str, request: Request):
        if name not in _ACTIONS:
            raise HTTPException(404, "Unknown preview action")
        supplied = request.headers.get("x-eval-preview-csrf", "")
        if not secrets.compare_digest(supplied.encode("utf-8"), csrf.encode("ascii")):
            raise HTTPException(403, "Preview CSRF token required")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "application/json required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 256 * 1024:
                raise HTTPException(413, "Preview action input is too large")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise HTTPException(422, "Invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(422, "A JSON object is required")
        if name == "experiment_start" and payload.get("mode", "offline") != "offline":
            raise HTTPException(403, "Real Agent execution is available only in the DeerFlow host")
        try:
            return await service.handler(name)(payload, context)
        except AgentRunError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    return app
