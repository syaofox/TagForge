"""app.py —— TagForge FastAPI 入口（替代 NiceGUI UI 层）。

架构：FastAPI + Jinja2 + HTMX。业务逻辑全部在 core.py（0 框架依赖）。
单机/局域网单人使用：模块级 core.state 单例，无会话态。
路由一律薄：参数解析 -> core 逻辑 -> 渲染片段/JSON/文件。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import core

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))

app = FastAPI(title="TagForge")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


# ---------------- 渲染上下文 ----------------
def _ctx(**kw) -> dict:
    c = {
        "settings": core.state.settings,
        "projects": core.scan_projects(),
        "current": core.state.current,
        "model_presets": core.MODEL_PRESETS,
        "prompt_presets": core.PROMPT_PRESETS,
        "client_ready": core.client_ready(),
    }
    c.update(kw)
    return c


# ---------------- 生命周期 ----------------
@app.on_event("startup")
async def startup() -> None:
    core.state.settings = core.load_settings()
    if not core.SETTINGS_FILE.exists():
        core.save_settings()


@app.on_event("shutdown")
async def shutdown() -> None:
    if core.state.batch is not None:
        core.state.abort_batch = True
        core.state.batch.cancel()


# ---------------- 页面 ----------------
@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "base.html", _ctx())


# ---------------- 配置（通用单键保存） ----------------
@app.post("/api/settings")
async def save_setting(request: Request) -> Response:
    body = await request.json()
    for k, v in (body or {}).items():
        core.state.settings[k] = v
    core.save_settings()
    return Response(status_code=204)
