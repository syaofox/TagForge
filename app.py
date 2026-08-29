"""app.py —— TagForge FastAPI 入口（替代 NiceGUI UI 层）。

架构：FastAPI + Jinja2 + HTMX。业务逻辑全部在 core.py（0 框架依赖）。
单机/局域网单人使用：模块级 core.state 单例，无会话态。
路由一律薄：参数解析 -> core 逻辑 -> 渲染片段/JSON/文件。
"""
from __future__ import annotations

import asyncio
import json
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import core
from llm_client import FatalAPIError, LLMClient

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    core.state.settings = core.load_settings()
    core.load_presets()
    if not core.SETTINGS_FILE.exists():
        core.save_settings()
    projects = core.scan_projects()
    remembered = core.state.settings.get("last_project") or ""
    core.state.current = remembered if remembered in projects else (projects[0] if projects else None)
    if core.state.current:
        core.state.entries = core.scan_entries(core.state.current)
    yield
    if core.state.batch is not None:
        core.state.abort_batch = True
        core.state.batch.cancel()


app = FastAPI(title="TagForge", lifespan=lifespan)
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
    client = core.build_client()
    core.state.client = client  # 挂到 state，供 Tokens 统计等读取
    return client, None


def _find_entry(name: str):
    return next((e for e in core.state.entries if e.name == name), None)


# ---------------- 渲染上下文 ----------------
def _ctx(**kw) -> dict:
    c = {
        "settings": core.state.settings,
        "projects": core.scan_projects(),
        "current": core.state.current,
        "project": core.state.current,  # 模板中图片 URL 等用（card.html 的 src/hx-get）
        "entries": core.state.entries,
        "model_presets": core.get_effective_presets(),
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
    if ok:
        core.state.entries = core.scan_entries(project)
        core.state.view_page = 1
    return JSONResponse({"ok": ok, "fail": fail, "renamed": renamed})


# ---------------- 批量标注（SSE） ----------------
def _start_batch(request: Request, targets: list | None = None) -> Response:
    if not core.state.current:
        return _toast(Response(status_code=400), "请先选择项目", "warning")
    if core.state.batch is not None and not core.state.batch.done():
        return _toast(Response(status_code=400), "已有批量任务在运行", "warning")
    client, err = _client_or_toast()
    if err is not None:
        return _toast(err, "API 配置无效，无法批量标注", "warning")
    targets = targets if targets is not None else         [e for e in core.state.entries if e.status in ("pending", "failed")]
    if not targets:
        return _toast(Response(status_code=400), "当前项目没有待标注或失败的图片", "info")
    core.state.abort_batch = False
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    core.state.batch_queue = queue

    def _enqueue(ev: dict) -> None:
        """快照式入队：新事件替换旧事件，保证最新（含 done）不丢。"""
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            queue.put_nowait(ev)
        except asyncio.QueueFull:
            pass

    run_task = asyncio.create_task(
        core.run_batch(targets, core.state.current, core.state.settings, client, _enqueue))

    async def _watch() -> None:
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        finally:
            await queue.put(None)  # 哨兵：通知 SSE 结束

    core.state.batch = asyncio.create_task(_watch())
    return _trigger(request, {"batchStarted": True})


@app.post("/api/batch/start")
async def batch_start(request: Request) -> Response:
    return _start_batch(request)


@app.post("/api/batch/retry")
async def batch_retry(request: Request) -> Response:
    failed = [e for e in core.state.entries if e.status == "failed"]
    if not failed:
        return _toast(Response(status_code=400), "没有可重试的失败图片", "info")
    return _start_batch(request, failed)


@app.post("/api/batch/stop")
async def batch_stop() -> Response:
    core.state.abort_batch = True
    return Response(status_code=204)


@app.get("/api/batch/events")
async def batch_events() -> StreamingResponse:
    async def gen():
        queue = core.state.batch_queue
        if queue is None:
            return
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if ev is None:
                break
            yield f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- 试生成 / 测试连接 / 导出 / Tokens ----------------
@app.post("/api/trial")
async def trial(request: Request) -> Response:
    client, err = _client_or_toast()
    if err is not None:
        return _toast(err, "API 配置无效", "warning")
    target = next((e for e in core.state.entries if e.status in ("pending", "failed")), None)
    if target is None:
        return _toast(Response(status_code=400), "没有待标注/失败的图片可试生成", "info")
    target.status = "processing"
    try:
        data = await core.encode_for_api_async(target.path)
        tags = await client.generate(data, core.state.settings.get("system_prompt", ""))
        final = core.apply_prefix(tags, core.state.settings.get("tag_prefix", ""),
                                  core.state.settings.get("prefix_mode", "prepend"))
        core.write_label(core.state.current, target.name, final)
        target.status = "tagged"
        target.label = final
        total = (client.total_prompt_tokens + client.total_completion_tokens) if client else 0
        resp = _trigger(request, {"gridChanged": True, "detailReload": True, "tokensUpdated": True,
                                  "toast": {"msg": f"✅ 试生成成功（{target.name}）：{final[:70]}",
                                            "type": "positive"}})
        return resp
    except FatalAPIError as e:
        target.status = "failed"
        return _toast(Response(status_code=500), f"❌ {e}", "negative")
    except Exception as e:
        target.status = "failed"
        return _toast(Response(status_code=500), f"❌ 试生成失败：{e}", "negative")


@app.post("/api/test-connection")
async def test_connection() -> JSONResponse:
    s = core.state.settings
    base = (s.get("base_url") or "").strip()
    model = (s.get("model") or "").strip()
    if not base or not model:
        return JSONResponse({"ok": False, "detail": "请先填写 Base URL 与模型名"})
    if not core.client_ready():
        return JSONResponse({"ok": False, "detail": "API Key 为空（Ollama 本地可留空）"})
    client = core.build_client()
    try:
        ok, detail = await client.ping()
        return JSONResponse({"ok": ok, "detail": f"✅ API 可用：{detail}"})
    except FatalAPIError as e:
        return JSONResponse({"ok": False, "detail": f"❌ {e}"})


@app.get("/api/export/{project}")
async def export_project(project: str) -> Response:
    zip_path = await asyncio.to_thread(core.export_zip, project)
    return FileResponse(zip_path, media_type="application/zip", filename=f"{project}.zip")


@app.get("/api/status/tokens")
async def tokens() -> JSONResponse:
    c = core.state.client
    total = (c.total_prompt_tokens + c.total_completion_tokens) if c else 0
    return JSONResponse({"total": total, "model": core.state.settings.get("model", "")})


# ---------------- 配置（通用单键保存） ----------------
@app.post("/api/settings")
async def save_setting(request: Request) -> Response:
    body = await request.json()
    for k, v in (body or {}).items():
        if k == "preset_keys":  # 合并，避免覆盖其它预设的 Key
            cur = core.state.settings.setdefault("preset_keys", {})
            if isinstance(v, dict):
                cur.update(v)
        else:
            core.state.settings[k] = v
    core.save_settings()
    return Response(status_code=204)


@app.get("/api/settings/preset")
async def preset_info(request: Request) -> JSONResponse:
    # 预设名经 query 传递：名称可含 `/` 等字符，放路径段会被 %2F 拆断（如「DeepSeek-VL（自建/中转）」）
    name = request.query_params.get("name") or ""
    preset = core.get_effective_presets().get(name)
    if not preset:
        return JSONResponse({"error": "未知预设"}, status_code=404)
    saved_key = (core.state.settings.get("preset_keys") or {}).get(name, "")
    return JSONResponse({"name": name, "base_url": preset["base_url"],
                         "model": preset["model"], "api_key": saved_key})


# ---------------- 模型预设管理（自定义持久化 + 内置统一处理） ----------------
@app.get("/api/settings/presets")
async def presets_list() -> JSONResponse:
    return JSONResponse({"presets": core.get_effective_presets()})


@app.post("/api/settings/presets")
async def presets_save(request: Request) -> JSONResponse:
    body = await request.json()
    body = body or {}
    err = core.upsert_preset(
        str(body.get("name") or ""),
        str(body.get("base_url") or ""),
        str(body.get("model") or ""),
        str(body.get("orig_name") or "") or None,
    )
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse({"ok": True, "presets": core.get_effective_presets()})


@app.delete("/api/settings/presets")
async def presets_delete(request: Request) -> JSONResponse:
    # 预设名经 query 传递：名称可含 `/`，放路径段会被 %2F 拆断（如「DeepSeek-VL（自建/中转）」）
    name = request.query_params.get("name") or ""
    core.delete_preset(name)
    return JSONResponse({"ok": True, "presets": core.get_effective_presets()})


# ---------------- 模型列表（从提供商拉取） ----------------
@app.post("/api/models")
async def models(request: Request) -> JSONResponse:
    body = await request.json()
    body = body or {}
    base = (str(body.get("base_url") or "")).strip()
    key = (str(body.get("api_key") or "")).strip()
    if not base:
        return JSONResponse({"error": "请先填写 Base URL"}, status_code=400)
    client = LLMClient(api_key=key, base_url=base,
                       model_name=core.state.settings.get("model", ""))
    try:
        models_list = await client.list_models()
    except FatalAPIError as e:
        status = 401 if ("无权限" in str(e) or "401" in str(e)) else 500
        return JSONResponse({"error": str(e)}, status_code=status)
    return JSONResponse({"models": models_list})


@app.post("/api/settings/prompt")
async def prompt_apply(request: Request) -> JSONResponse:
    body = await request.json()
    preset = (body or {}).get("prompt_preset") or ""
    core.state.settings["prompt_preset"] = preset
    if preset == "custom":
        core.save_settings()
        return JSONResponse({"system_prompt": core.state.settings.get("system_prompt", ""),
                             "prompt_preset": preset})
    text = core.resolve_prompt_text(preset)
    core.state.settings["system_prompt"] = text
    core.save_settings()
    return JSONResponse({"system_prompt": text, "prompt_preset": preset})


@app.post("/api/settings/prompt/default")
async def prompt_default() -> JSONResponse:
    core.state.settings["prompt_preset"] = "en_short_default"
    text = core.resolve_prompt_text("en_short_default")
    core.state.settings["system_prompt"] = text
    core.save_settings()
    return JSONResponse({"system_prompt": text, "prompt_preset": "en_short_default"})
