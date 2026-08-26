"""app.py —— TagForge FastAPI 入口（替代 NiceGUI UI 层）。

架构：FastAPI + Jinja2 + HTMX。业务逻辑全部在 core.py（0 框架依赖）。
单机/局域网单人使用：模块级 core.state 单例，无会话态。
路由一律薄：参数解析 -> core 逻辑 -> 渲染片段/JSON/文件。
"""
from __future__ import annotations

import asyncio
import json
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import core

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))

app = FastAPI(title="TagForge")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


# ---------------- 工具 ----------------
def uq(s) -> str:
    """URL 路径段编码（空格 -> %20，区别于 query 的 +）。"""
    return urllib.parse.quote(str(s or ""))


def _toast(resp: Response, msg: str, type: str = "positive") -> Response:
    resp.headers["HX-Trigger"] = json.dumps({"toast": {"msg": msg, "type": type}})
    return resp


# ---------------- 渲染上下文 ----------------
def _ctx(**kw) -> dict:
    c = {
        "settings": core.state.settings,
        "projects": core.scan_projects(),
        "current": core.state.current,
        "entries": core.state.entries,
        "model_presets": core.MODEL_PRESETS,
        "prompt_presets": core.PROMPT_PRESETS,
        "status_text": core.STATUS_TEXT,
        "status_color": core.STATUS_COLOR,
        "uq": uq,
        "client_ready": core.client_ready(),
    }
    c.update(kw)
    return c


def _grid_ctx() -> dict:
    """当前项目 + 筛选/分页下的网格渲染上下文。"""
    st = core.state
    stats = core.compute_stats()
    items = core.filtered_entries()
    shown = items[: st.view_page * core.PAGE_SIZE]
    remain = len(items) - len(shown)
    parts = [f"共 {stats['all']} 张"]
    for key, icon in (("tagged", "🟢 已标注"), ("pending", "🟡 待标注"),
                      ("failed", "🔴 失败"), ("processing", "🔵 处理中")):
        if stats[key]:
            parts.append(f"{icon} {stats[key]}")
    return {
        **_ctx(),
        "stats_line": " · ".join(parts),
        "items": items, "shown": shown, "remain": remain,
        "filter": st.view_filter, "q": st.view_query, "view_page": st.view_page,
    }


def _render_select(request: Request, toast: tuple | None = None) -> Response:
    resp = templates.TemplateResponse(request, "partials/select_result.html", _grid_ctx())
    if toast:
        return _toast(resp, toast[0], toast[1])
    return resp


# ---------------- 生命周期 ----------------
@app.on_event("startup")
async def startup() -> None:
    core.state.settings = core.load_settings()
    if not core.SETTINGS_FILE.exists():
        core.save_settings()
    projects = core.scan_projects()
    remembered = core.state.settings.get("last_project") or ""
    core.state.current = remembered if remembered in projects else (projects[0] if projects else None)
    if core.state.current:
        core.state.entries = core.scan_entries(core.state.current)


@app.on_event("shutdown")
async def shutdown() -> None:
    if core.state.batch is not None:
        core.state.abort_batch = True
        core.state.batch.cancel()


# ---------------- 页面 ----------------
@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "base.html", _ctx())


# ---------------- 项目 CRUD ----------------
@app.post("/api/projects")
async def create_project(request: Request) -> Response:
    form = await request.form()
    name = (form.get("name") or "").strip()
    err = core.create_project(name)
    if err:
        resp = templates.TemplateResponse(request, "partials/select_result.html", _grid_ctx())
        return _toast(resp, err, "warning")
    clean = core.sanitize_project_name(name)
    core.state.current = clean
    core.state.settings["last_project"] = clean
    core.save_settings()
    core.state.entries = []
    core.state.view_filter = "all"
    core.state.view_query = ""
    core.state.view_page = 1
    return _render_select(request, toast=(f"已创建项目 {clean}", "positive"))


@app.post("/api/projects/{name}/select")
async def select_project(request: Request, name: str) -> Response:
    if core.state.batch is not None and not core.state.batch.done():
        return _toast(_render_select(request), "有批量标注正在进行，请先终止", "warning")
    core.state.current = name
    core.state.settings["last_project"] = name
    core.save_settings()
    core.state.entries = core.scan_entries(name)
    core.state.view_filter = "all"
    core.state.view_query = ""
    core.state.view_page = 1
    return _render_select(request)


@app.delete("/api/projects/{name}")
async def delete_project(request: Request, name: str) -> Response:
    core.delete_project(name)
    if core.state.current == name:
        core.state.current = None
        core.state.entries = []
        core.state.settings["last_project"] = ""
        core.save_settings()
    resp = templates.TemplateResponse(request, "partials/select_result.html", _grid_ctx())
    return _toast(resp, f"已删除项目 {name}", "positive")


# ---------------- 网格 ----------------
@app.get("/partials/grid_cards")
async def grid_cards(request: Request, filter: str | None = None, q: str | None = None,
                     page: int = 0) -> Response:
    if filter is not None:
        core.state.view_filter = filter or "all"
    if q is not None:
        core.state.view_query = (q or "").strip()
    if page > 0:
        core.state.view_page = page
    return templates.TemplateResponse(request, "partials/grid_cards.html", _grid_ctx())


# ---------------- 图片（静态直连） ----------------
@app.get("/api/image/thumb/{project}/{name}")
async def thumb(project: str, name: str) -> Response:
    p = await asyncio.to_thread(core.ensure_thumb_file, project, name)
    if p is None:
        return Response(status_code=404)
    return FileResponse(p, media_type="image/jpeg")


# ---------------- 配置（通用单键保存） ----------------
@app.post("/api/settings")
async def save_setting(request: Request) -> Response:
    body = await request.json()
    for k, v in (body or {}).items():
        core.state.settings[k] = v
    core.save_settings()
    return Response(status_code=204)
