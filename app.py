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

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import core
from llm_client import FatalAPIError, LLMClient

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


def _trigger(request, *triggers) -> Response:
    """组装 HX-Trigger 头（toast 特例直接传 dict）。"""
    resp = Response(status_code=204)
    payload = {}
    for t in triggers:
        if isinstance(t, dict):
            payload.update(t)
        else:
            payload[t] = True
    if payload:
        resp.headers["HX-Trigger"] = json.dumps(payload)
    return resp


def _client_or_toast() -> tuple[LLMClient | None, Response | None]:
    """校验配置并返回客户端；失败时返回带提示的响应。"""
    s = core.state.settings
    base = (s.get("base_url") or "").strip()
    model = (s.get("model") or "").strip()
    if not base or not model:
        return None, _toast(Response(status_code=400), "请先配置 Base URL 与模型", "warning")
    if not core.client_ready():
        return None, _toast(Response(status_code=400), "API Key 为空，无法调用 API", "warning")
    return core.build_client(), None


def _find_entry(name: str):
    return next((e for e in core.state.entries if e.name == name), None)


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


@app.get("/api/image/{project}/{name}")
async def image(project: str, name: str) -> Response:
    p = core.images_dir(project) / name
    if not p.is_file():
        return Response(status_code=404)
    return FileResponse(p)


# ---------------- 详情 / 标签 / 再生 / 删除 ----------------
@app.get("/partials/detail")
async def detail(request: Request, project: str, name: str) -> Response:
    entry = _find_entry(name)
    if entry is None:
        resp = templates.TemplateResponse(request, "partials/detail.html",
                                          _ctx(project=project))
        return resp
    entries = core.state.entries
    idx = entries.index(entry)
    total = len(entries)

    def _nav(delta: int) -> dict | None:
        if total <= 1:
            return None
        e = entries[(idx + delta) % total]
        return {"name": e.name}

    ctx = _ctx(project=project, entry=entry, label=core.read_label(project, entry.name),
               index=idx, total=total, prev=_nav(-1), next=_nav(1))
    return templates.TemplateResponse(request, "partials/detail.html", ctx)


@app.post("/api/label/{project}/{name}")
async def save_label(request: Request, project: str, name: str) -> Response:
    form = await request.form()
    text = (form.get("text") or "").strip()
    core.write_label(project, name, text)
    entry = _find_entry(name)
    if entry is not None:
        entry.status = "tagged" if text else "pending"
        entry.label = text
    resp = _trigger(request, {"gridChanged": True})
    return resp


@app.post("/api/regenerate")
async def regenerate(request: Request) -> Response:
    form = await request.form()
    project = form.get("project") or core.state.current
    name = form.get("name") or ""
    entry = _find_entry(name)
    if entry is None:
        return _toast(Response(status_code=400), "未找到该图片", "warning")
    client, err = _client_or_toast()
    if err is not None:
        return _toast(err, "配置无效，请先检查 Base URL / Key / 模型", "warning")
    entry.status = "processing"
    try:
        data = await core.encode_for_api_async(entry.path)
        tags = await client.generate(data, core.state.settings.get("system_prompt", ""))
        final = core.apply_prefix(tags, core.state.settings.get("tag_prefix", ""),
                                  core.state.settings.get("prefix_mode", "prepend"))
        core.write_label(project, name, final)
        entry.status = "tagged"
        entry.label = final
        resp = _trigger(request, {"gridChanged": True, "detailReload": True,
                                  "toast": {"msg": "重新生成完成", "type": "positive"}})
        return resp
    except FatalAPIError as e:
        entry.status = "failed"
        return _toast(Response(status_code=500), f"生成失败：{e}", "negative")
    except Exception as e:
        entry.status = "failed"
        return _toast(Response(status_code=500), f"生成失败：{e}", "negative")


@app.delete("/api/image/{project}/{name}")
async def delete_image(request: Request, project: str, name: str) -> Response:
    entry = _find_entry(name)
    try:
        (core.images_dir(project) / name).unlink()
        lp = core.label_file(project, name)
        if lp.exists():
            lp.unlink()
    except OSError as e:
        return _toast(Response(status_code=500), f"删除失败：{e}", "negative")
    (core.DATASETS / ".cache" / project / f"{Path(name).stem}.thumb.jpg").unlink(missing_ok=True)
    if entry is not None:
        core.state.entries.remove(entry)
    resp = _trigger(request, {"gridChanged": True, "detailClosed": True,
                              "toast": {"msg": f"已删除 {name}", "type": "positive"}})
    return resp


# ---------------- 上传（multipart，前端逐文件 XHR） ----------------
@app.post("/api/upload")
async def upload(request: Request, files: list[UploadFile] = File(...)) -> JSONResponse:
    project = core.state.current
    if not project:
        return JSONResponse({"ok": 0, "fail": len(files), "renamed": 0}, status_code=400)
    ok = fail = renamed = 0
    for f in files:
        name = f.filename or ""
        dst, ren = core.resolve_upload_destination(project, name)
        if dst is None:
            fail += 1
            continue
        try:
            data = await f.read()
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            ok += 1
            if ren:
                renamed += 1
        except Exception:
            fail += 1
    return JSONResponse({"ok": ok, "fail": fail, "renamed": renamed})


# ---------------- 配置（通用单键保存） ----------------
@app.post("/api/settings")
async def save_setting(request: Request) -> Response:
    body = await request.json()
    for k, v in (body or {}).items():
        core.state.settings[k] = v
    core.save_settings()
    return Response(status_code=204)
