"""main.py —— TagForge：LoRA 数据集图片打标工具（Python + NiceGUI 单页应用）。

数据约定（零数据库，全部落在文件系统）：
  datasets/<项目>/images/   原始图片
  datasets/<项目>/labels/   与图片同名的 .txt 标签
  datasets/.cache/<项目>/   缩略图缓存
  config/settings.json      全局配置（API Key / Base URL / 模型 / 提示词等）

运行（宿主机用 uv，见 README 与设计文档 11.6）：
  uv pip install -r requirements.txt
  python main.py             # 访问 http://localhost:8080
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from nicegui import app, background_tasks, events, run, ui
from PIL import Image

from llm_client import FatalAPIError, LLMClient
import core
from core import (
    DATASETS, SETTINGS_FILE, IMAGE_EXTS, PAGE_SIZE,
    MODEL_PRESETS, PROMPT_PRESETS, STATUS_TEXT, STATUS_COLOR,
    ImageEntry, state,
    load_settings, save_settings,
    scan_projects, project_images, label_file,
    read_status, read_label, write_label, apply_prefix,
    make_thumb, encode_for_api, client_ready, soft_color,
    compute_stats, filtered_entries,
)


# UI 元素引用全局表（NiceGUI 迁移期间使用；业务状态见 core.state）
UI: dict = {}  # 复用元素的引用


# ---------------- 模型客户端 ----------------


async def test_api() -> None:
    """用左侧当前填写的 Base URL / Key / 模型做连通性测试。"""
    base = (UI["base_url_input"].value or "").strip()
    key = (UI["api_key_input"].value or "").strip()
    model = (UI["model_input"].value or "").strip()
    if not base or not model:
        ui.notify("请先填写 Base URL 与模型名", type="warning")
        return
    if not key and "ollama" not in base.lower():
        ui.notify("API Key 为空（Ollama 本地可留空）", type="warning")
        return
    btn = UI.get("test_btn")
    spin = UI.get("test_spinner")
    try:
        if btn is not None:
            btn.set_enabled(False)
            btn.set_text("测试中…")
        if spin is not None:
            spin.set_visibility(True)
    except Exception:
        pass
    client = LLMClient(api_key=key, base_url=base, model_name=model)
    try:
        ok, detail = await client.ping()
        ui.notify(f"✅ API 可用：{detail}", type="positive", timeout=8000)
    except FatalAPIError as e:
        ui.notify(f"❌ {e}", type="negative", timeout=10000)
    finally:
        try:
            if btn is not None:
                btn.set_enabled(True)
                btn.set_text("测试连接")
            if spin is not None:
                spin.set_visibility(False)
        except Exception:
            pass


def ensure_client() -> bool:
    s = state.settings
    base = (s.get("base_url") or "").strip()
    model = (s.get("model") or "").strip()
    if not base or not model:
        ui.notify("请先配置 Base URL 与模型", type="warning")
        return False
    if not client_ready():
        ui.notify("API Key 为空，无法调用 API", type="warning")
        return False
    state.client = LLMClient(api_key=s.get("api_key"), base_url=base, model_name=model)
    return True


def update_tokens() -> None:
    """更新顶栏 Token 统计。"""
    c = state.client
    total = (c.total_prompt_tokens + c.total_completion_tokens) if c else 0
    el = UI.get("header_tokens")
    if el is not None:
        el.set_text(f"Tokens：{total}")


def update_header_meta() -> None:
    """顶栏：当前模型名 + Token。"""
    mdl = UI.get("header_model")
    if mdl is not None:
        mdl.set_text(f"模型：{state.settings.get('model', '')}")
    update_tokens()


# ---------------- 徽章 / 状态 ----------------


def _alive(el) -> bool:
    """元素仍可用（存在且未被删除）。"""
    return el is not None and not el.is_deleted


def ui_guard(key: str):
    """返回存活可用的 UI 元素；已删除/不存在返回 None。"""
    el = UI.get(key)
    return el if _alive(el) else None


def update_card_caption(entry: ImageEntry) -> None:
    """刷新卡片缩略图下方的标注文字（空则隐藏；元素已随网格重建删除则跳过）。"""
    if not _alive(entry.caption):
        return
    text = read_label(state.current, entry.name).strip()
    entry.caption.set_text(text)
    entry.caption.set_visibility(bool(text))


def update_card_frame(entry: ImageEntry) -> None:
    """根据状态刷新卡片描边与 ✓ 角标（P1-8）。"""
    if _alive(entry.card):
        if entry.status == "tagged":
            entry.card.style("border-color: rgba(34,197,94,.7) !important")
        elif entry.status == "failed":
            entry.card.style("border-color: rgba(239,68,68,.6) !important")
        else:
            entry.card.style("border-color: var(--tf-border) !important")
    if _alive(entry.check):
        entry.check.set_visibility(entry.status == "tagged")


def set_badge(entry: ImageEntry, status: str) -> None:
    entry.status = status
    update_stats()  # 统计条实时联动
    update_card_frame(entry)  # 状态描边/✓ 联动
    if _alive(entry.badge):
        entry.badge.set_text(STATUS_TEXT[status])
        entry.badge.style(f"background:{soft_color(STATUS_COLOR[status])}; color:{STATUS_COLOR[status]};")
        entry.badge.classes(remove="tf-badge-solid")


# ---------------- 项目 / 网格 ----------------
def refresh_project_list() -> None:
    state.projects = scan_projects()
    box = UI["project_list"]
    box.clear()
    with box:
        if not state.projects:
            ui.label("暂无项目").classes("text-sm text-gray-400")
        else:
            for name in state.projects:
                active = name == state.current
                b = ui.button(name, on_click=lambda n=name: select_project(n)) \
                    .props("dense align-left no-caps").classes("w-full justify-start rounded-lg")
                if active:
                    b.props("unelevated color=primary").classes("text-white")
                else:
                    b.props("flat ").classes("tf-muted")


def select_project(name: str) -> None:
    if state.batch and not state.batch.done():
        ui.notify("有批量标注正在进行，请先终止", type="warning")
        return
    state.current = name
    UI["toolbar_title"].set_text(name)
    state.settings["last_project"] = name  # 跨重启记住当前项目
    save_settings()
    refresh_project_list()  # 刷新选中态
    background_tasks.create(refresh_grid())


# ---------------- 网格：统计 / 筛选 / 分页 / 空态（P0-1~4） ----------------


def update_stats() -> None:
    """刷新统计条文字（批量/状态变化时调用）。"""
    el = UI.get("stats_label")
    if not _alive(el):
        return
    c = compute_stats()
    parts = [f"共 {c['all']} 张"]
    for key, icon in (("tagged", "🟢 已标注"), ("pending", "🟡 待标注"),
                      ("failed", "🔴 失败"), ("processing", "🔵 处理中")):
        if c[key]:
            parts.append(f"{icon} {c[key]}")
    el.set_text(" · ".join(parts))




def render_meta_bar() -> None:
    """构建统计 + 筛选 + 搜索条。"""
    bar = UI["meta_bar"]
    bar.clear()
    with bar:
        UI["stats_label"] = ui.label("").classes("tf-muted text-sm font-medium")
        update_stats()
        UI["filter_toggle"] = ui.toggle(
            {"all": "全部", "tagged": "🟢 已标注", "pending": "🟡 待标注", "failed": "🔴 失败"},
            value=state.view_filter, on_change=on_filter_change,
        ).props("dense")
        UI["search_input"] = ui.input(
            placeholder="🔍 搜索文件名或标签…", value=state.view_query) \
            .props("outlined dense clearable").classes("w-64") \
            .on_value_change(on_search_change)


def on_filter_change(e: events.ValueChangeEventArguments) -> None:
    state.view_filter = e.value
    state.view_page = 1
    render_grid_page()


def on_search_change(e: events.ValueChangeEventArguments) -> None:
    """搜索输入：防抖 0.3s 后重绘网格。"""
    state.view_query = (e.value or "").strip()
    state.view_page = 1
    state.search_seq += 1
    background_tasks.create(_debounced_render(state.search_seq))


async def _debounced_render(seq: int) -> None:
    await asyncio.sleep(0.3)
    if seq == state.search_seq:
        render_grid_page()


def load_more() -> None:
    state.view_page += 1
    render_grid_page()


def render_grid_page() -> None:
    """按当前筛选/分页渲染卡片；维护「加载更多」与空态。"""
    grid = UI["grid"]
    grid.clear()
    if not state.entries:
        with grid:
            ui.label("📭 该项目还没有图片，点击顶栏「上传图片」开始。").classes("tf-muted py-10")
        UI["load_more_btn"].set_visibility(False)
        return
    items = filtered_entries()
    if not items:
        with grid:
            ui.label("🔍 没有匹配的图片（试试清空筛选或搜索）。").classes("tf-muted py-10")
        UI["load_more_btn"].set_visibility(False)
        return
    shown = items[: state.view_page * PAGE_SIZE]
    with grid:
        for entry in shown:
            card = ui.card().props("flat") \
                .classes("tf-card") \
                .mark("image-card") \
                .on("click", lambda e0=entry: open_detail(state.entries.index(e0)))
            entry.card = card
            update_card_frame(entry)  # 初次渲染即应用状态描边/✓
            with card:
                entry.check = ui.label("✓").classes("tf-check").style("background:#22c55e")
                entry.check.set_visibility(entry.status == "tagged")
                if entry.thumb:
                    ui.image(entry.thumb).classes("tf-card-img")
                else:
                    ui.label("⚠️ 无法预览").classes("tf-card-img tf-img-err tf-muted")
                entry.badge = ui.label(STATUS_TEXT[entry.status]).classes("tf-badge")
                entry.badge.style(f"background:{soft_color(STATUS_COLOR[entry.status])}; color:{STATUS_COLOR[entry.status]};")
                entry.caption = ui.label(entry.label).classes("tf-card-caption")
                if not entry.label.strip():
                    entry.caption.set_visibility(False)  # 未标注不占位
                ui.label(entry.name).classes("tf-card-name")
    remain = len(items) - state.view_page * PAGE_SIZE
    if remain > 0:
        UI["load_more_btn"].set_visibility(True)
        UI["load_more_btn"].set_text(f"加载更多（还有 {remain} 张）")
    else:
        UI["load_more_btn"].set_visibility(False)


def render_api_banner() -> None:
    """API 未就绪时显示提示条。"""
    el = UI.get("api_banner")
    if el is None:
        return
    el.set_visibility(not client_ready() and bool(state.current))


async def refresh_grid() -> None:
    grid = UI["grid"]
    grid.clear()
    if not state.current:
        UI["meta_bar"].clear()
        UI["api_banner"].set_visibility(False)
        UI["load_more_btn"].set_visibility(False)
        with grid:
            ui.label("😶 请先在左侧选择或新建一个项目。").classes("tf-muted py-10")
        return

    imgs = project_images(state.current)
    entries = [
        ImageEntry(name=p.name, path=p, status=read_status(state.current, p.name),
                   label=read_label(state.current, p.name))
        for p in imgs
    ]
    if entries:
        with grid:
            ui.spinner(size="3em", color="primary")
            ui.label("正在加载图片…").classes("tf-muted text-sm")
    # 并行生成缩略图（线程池），单张失败不影响整批
    results = await asyncio.gather(
        *(run.io_bound(make_thumb, state.current, e.name) for e in entries),
        return_exceptions=True,
    )
    for e, r in zip(entries, results):
        e.thumb = r if isinstance(r, str) else ""
    state.entries = entries
    state.view_page = 1
    render_meta_bar()
    render_grid_page()
    render_api_banner()


# ---------------- 详情面板 ----------------
def open_detail(i: int) -> None:
    if not state.entries:
        return
    state.index = i
    UI["drawer"].show()
    render_detail()
    background_tasks.create(load_preview(i))  # 懒加载原图预览


def render_detail() -> None:
    drawer = UI["drawer"]
    drawer.clear()
    entry = state.entries[state.index]
    with drawer:
        with ui.column().classes("w-full gap-3 p-5"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column().classes("gap-0"):
                    ui.label(entry.name).classes("tf-text font-bold")
                    ui.label("◀ ▶ 可切换上一张 / 下一张").classes("tf-muted text-xs")
                ui.button(icon="close", on_click=drawer.hide).props("flat round color=grey-7")
            UI["preview_img"] = ui.image(entry.preview or entry.thumb) \
                .props("fit=contain") \
                .classes("w-full max-h-96 rounded-xl cursor-zoom-in") \
                .on("click", open_lightbox).tooltip("点击放大（Esc 关闭）")
            state.tagbox = ui.textarea(label="标签文本", value=read_label(state.current, entry.name)) \
                .classes("w-full").props("outlined dense") \
                .on("keydown", on_tagbox_keys)  # 文本框内 Ctrl+Enter 保存
            with ui.row().classes("w-full gap-2 items-center"):
                UI["save_btn"] = ui.button("保存", icon="save", on_click=save_tag) \
                    .props("unelevated rounded color=green-7").classes("flex-1")
                with ui.row().classes("items-center gap-1 no-wrap"):
                    UI["regenerate_btn"] = ui.button("重新生成", icon="auto_awesome",
                                                     on_click=regenerate) \
                        .props("unelevated rounded color=blue-7").classes("flex-1")
                    UI["gen_spinner"] = ui.spinner(size="sm", color="blue-7").set_visibility(False)
                UI["delete_btn"] = ui.button("删除图片", icon="delete", on_click=confirm_delete) \
                    .props("flat rounded color=red-6").classes("shrink-0")
            with ui.row().classes("w-full items-center justify-between pt-1"):
                ui.button(icon="navigate_before", on_click=prev_img).props("round outline color=primary")
                ui.label(f"{state.index + 1} / {len(state.entries)}").classes("tf-muted text-sm")
                ui.button(icon="navigate_next", on_click=next_img).props("round outline color=primary")


def save_tag() -> None:
    if not state.current or not state.entries or state.tagbox is None:
        return
    entry = state.entries[state.index]
    text = state.tagbox.value or ""
    write_label(state.current, entry.name, text)
    set_badge(entry, "tagged" if text.strip() else "pending")
    update_card_caption(entry)
    ui.notify("已保存")


def set_generating_ui(generating: bool) -> None:
    """切换「重新生成」进行中的 UI 状态：按钮置灰/文案、菊花、并锁定保存/删除。"""
    try:
        for key in ("save_btn", "delete_btn"):
            el = UI.get(key)
            if el is not None:
                el.set_enabled(not generating)
        btn = UI.get("regenerate_btn")
        if btn is not None:
            btn.set_enabled(not generating)
            btn.set_text("生成中…" if generating else "重新生成")
        sp = UI.get("gen_spinner")
        if sp is not None:
            sp.set_visibility(generating)
    except Exception:
        pass  # 详情面板可能已被关闭/重建


async def regenerate() -> None:
    if not ensure_client():
        return
    entry = state.entries[state.index]
    set_badge(entry, "processing")
    set_generating_ui(True)  # 立即给出“正在生成”反馈
    try:
        data = "data:image/jpeg;base64," + base64.b64encode(
            await run.io_bound(encode_for_api, entry.path)).decode("ascii")
        tags = await state.client.generate(
            data, state.settings.get("system_prompt", ""))
        final = apply_prefix(tags, state.settings.get("tag_prefix", ""),
                             state.settings.get("prefix_mode", "prepend"))
        write_label(state.current, entry.name, final)
        if state.tagbox is not None:
            state.tagbox.set_value(final)
        set_badge(entry, "tagged")
        update_card_caption(entry)
        update_tokens()
        ui.notify("重新生成完成")
    except FatalAPIError as e:
        set_badge(entry, "failed")
        ui.notify(str(e), type="negative")
    except Exception as e:
        set_badge(entry, "failed")
        ui.notify(f"生成失败：{e}", type="negative")
    finally:
        set_generating_ui(False)


def open_lightbox() -> None:
    """详情大图点击放大（P1-9）。"""
    if not state.entries:
        return
    entry = state.entries[state.index]
    img = UI.get("lightbox_img")
    if img is None:
        return
    img.set_source(entry.preview or entry.thumb)
    UI["lightbox_dialog"].open()


def lightbox_close() -> None:
    UI["lightbox_dialog"].close()


def on_tagbox_keys(e: events.GenericEventArguments) -> None:
    """详情文本框内的 Ctrl+Enter = 保存（全局 ui.keyboard 默认忽略 textarea 按键）。"""
    try:
        a = e.args or {}
        if a.get("key") == "Enter" and (a.get("ctrlKey") or a.get("metaKey")):
            save_tag()
    except Exception:
        pass


def handle_shortcuts(e: events.KeyboardEventArguments) -> None:
    """全局快捷键：Ctrl+Enter 保存；←/→ 切换图片；Esc 关闭（P1-9）。"""
    try:
        key = (e.key or "").lower()
        mods = e.modifiers or []
        if key == "escape":
            UI.get("lightbox_dialog").close()
            UI["drawer"].hide()
        elif key == "enter" and any(m in ("ctrl", "control", "meta") for m in mods):
            save_tag()
        elif key in ("arrowleft", "arrowright") and UI["drawer"].value:
            (prev_img if key == "arrowleft" else next_img)()
    except Exception:
        pass


async def trial_generate() -> None:
    """试生成 1 张（P1-10）：用当前配置对第一张待标注/失败图跑一次。"""
    if not state.current:
        ui.notify("请先选择项目", type="warning")
        return
    if not ensure_client():
        return
    target = next((e for e in state.entries if e.status in ("pending", "failed")), None)
    if target is None:
        ui.notify("没有待标注/失败的图片可试生成", type="info")
        return
    btn = UI.get("trial_btn")
    try:
        if btn is not None:
            btn.set_enabled(False)
        set_badge(target, "processing")
        data = "data:image/jpeg;base64," + base64.b64encode(
            await run.io_bound(encode_for_api, target.path)).decode("ascii")
        tags = await state.client.generate(
            data, state.settings.get("system_prompt", ""))
        final = apply_prefix(tags, state.settings.get("tag_prefix", ""),
                             state.settings.get("prefix_mode", "prepend"))
        write_label(state.current, target.name, final)
        set_badge(target, "tagged")
        update_card_caption(target)
        update_tokens()
        ui.notify(f"✅ 试生成成功（{target.name}）：{final[:70]}", type="positive", timeout=6000)
        background_tasks.create(refresh_grid())
    except FatalAPIError as e:
        set_badge(target, "failed")
        ui.notify(f"❌ {e}", type="negative", timeout=8000)
    except Exception as e:
        set_badge(target, "failed")
        ui.notify(f"❌ 试生成失败：{e}", type="negative", timeout=8000)
    finally:
        if btn is not None:
            btn.set_enabled(True)


async def load_preview(i: int) -> None:
    """异步加载原图（缩放后 JPEG）预览，替换缩略图。"""
    if not state.entries or i >= len(state.entries):
        return
    entry = state.entries[i]
    if entry.preview:
        return
    try:
        data = "data:image/jpeg;base64," + base64.b64encode(
            await run.io_bound(encode_for_api, entry.path)).decode("ascii")
    except Exception:
        return
    entry.preview = data
    if state.index == i and ui_guard("preview_img") is not None:
        UI["preview_img"].set_source(data)  # 详情面板仍打开且是同张图时即时替换


def prev_img() -> None:
    if state.entries:
        state.index = (state.index - 1) % len(state.entries)
        render_detail()


def next_img() -> None:
    if state.entries:
        state.index = (state.index + 1) % len(state.entries)
        render_detail()


def confirm_delete() -> None:
    dlg = UI["confirm_dialog"]
    with dlg.clear(), ui.card():
        ui.label("确定删除该图片及其标签吗？此操作不可恢复。")
        with ui.row():
            ui.button("取消", on_click=dlg.close)
            ui.button("删除", on_click=do_delete).props("color=red-7")
    dlg.open()


def do_delete() -> None:
    UI["confirm_dialog"].close()
    if not state.current or not state.entries:
        return
    entry = state.entries[state.index]
    try:
        entry.path.unlink()
        lp = label_file(state.current, entry.name)
        if lp.exists():
            lp.unlink()
    except OSError as e:
        ui.notify(f"删除失败：{e}", type="negative")
        return
    (DATASETS / ".cache" / state.current / f"{Path(entry.name).stem}.thumb.jpg").unlink(missing_ok=True)
    ui.notify(f"已删除 {entry.name}")
    UI["drawer"].hide()
    background_tasks.create(refresh_grid())


# ---------------- 上传 ----------------
# ---------------- 上传 ----------------
def on_upload_begin(e: events.UiEventArguments) -> None:
    """上传开始提示（本版 NiceGUI 无字节级进度事件，用状态提示代替）。"""
    try:
        UI["upload_status"].set_text("上传中…")
        UI["upload_status"].set_visibility(True)
    except Exception:
        pass


async def on_upload(e: events.UploadEventArguments) -> None:
    if not state.current:
        ui.notify("请先选择项目", type="warning")
        return
    dst, renamed = core.resolve_upload_destination(state.current, e.file.name or "")
    if dst is None:
        state.upload_fail += 1
        return
    if renamed:
        state.upload_renamed += 1
    try:
        await e.file.save(dst)
        state.upload_ok += 1
    except Exception:
        state.upload_fail += 1




def on_multi_upload(e: events.MultiUploadEventArguments) -> None:
    """本次选择上传完成：汇总提示 + 刷新网格（P0-5）。"""
    UI["upload_status"].set_visibility(False)
    ok, fail, ren = state.upload_ok, state.upload_fail, state.upload_renamed
    state.upload_ok = state.upload_fail = state.upload_renamed = 0
    if ok:
        msg = f"已上传 {ok} 张"
        if ren:
            msg += f"（{ren} 张同名已自动改名）"
        ui.notify(msg, type="positive", timeout=4000)
    if fail:
        ui.notify(f"{fail} 张上传失败（类型不支持或写入出错）", type="negative", timeout=5000)
    background_tasks.create(refresh_grid())


# ---------------- 批量标注（P0-6：主区进度卡） ----------------
def add_log(text: str) -> None:
    state.batch_log_lines.append(text)
    box = UI.get("batch_log")
    if _alive(box):
        with box:
            ui.label(text).classes("text-xs font-mono")


def start_batch() -> None:
    if not state.current:
        ui.notify("请先选择项目", type="warning")
        return
    if not ensure_client():
        return
    targets = [e for e in state.entries if e.status in ("pending", "failed")]
    if not targets:
        ui.notify("当前项目没有待标注或失败的图片", type="info")
        return
    launch_batch(targets)


def retry_failed() -> None:
    """一键重试所有失败图片（P0-6）。"""
    failed = [e for e in state.entries if e.status == "failed"]
    if not failed:
        ui.notify("没有可重试的失败图片", type="info")
        return
    if ensure_client():
        launch_batch(failed)


def launch_batch(targets: list) -> None:
    """启动批量任务并显示主区进度卡。"""
    state.abort_batch = False
    UI["batch_status"].set_text("处理中…")
    UI["batch_progress"].value = 0.0
    UI["batch_stats"].set_text(f"待处理 {len(targets)} 张 · ✅ 0 · ❌ 0")
    UI["batch_log"].clear()
    state.batch_log_lines.clear()
    UI["batch_log_area"].set_visibility(True)
    state.batch_log_visible = True
    UI["batch_stop_btn"].set_enabled(True)
    UI["batch_retry_btn"].set_visibility(False)
    UI["batch_card"].set_visibility(True)
    UI["main_progress"].set_visibility(True)
    state.batch = background_tasks.create(run_batch(targets))


def stop_batch() -> None:
    if state.batch and not state.batch.done():
        state.abort_batch = True
        add_log("⏹ 已请求终止…")


def collapse_batch() -> None:
    """收起进度卡（后台继续跑），进度可在顶栏看到。"""
    UI["batch_card"].set_visibility(False)


def toggle_batch_log() -> None:
    state.batch_log_visible = not state.batch_log_visible
    UI["batch_log_area"].set_visibility(state.batch_log_visible)


def clear_batch_log() -> None:
    UI["batch_log"].clear()
    state.batch_log_lines.clear()


def copy_batch_log() -> None:
    lines = "\n".join(state.batch_log_lines)
    if not lines:
        ui.notify("日志为空", type="info")
        return
    payload = json.dumps(lines)
    ui.run_javascript(f"navigator.clipboard.writeText({payload}).catch(()=>{{}});")
    ui.notify("已复制日志到剪贴板")


async def run_batch(targets: list) -> None:
    # 批量标注的 UI 包装：驱动 core.run_batch，把进度回调映射为 NiceGUI 元素更新
    def _cb(ev: dict) -> None:
        try:
            t = ev["type"]
            if t == "mark":
                entry = next((e for e in state.entries if e.name == ev["name"]), None)
                if entry is None:
                    return
                set_badge(entry, ev["status"])
                if ev["status"] == "tagged":
                    update_card_caption(entry)
            elif t == "log":
                add_log(ev["text"])
            elif t == "progress":
                bp = ui_guard("batch_progress")
                if bp is not None:
                    bp.value = ev["done"] / ev["total"]
                mp = ui_guard("main_progress")
                if mp is not None:
                    mp.value = ev["done"] / ev["total"]
                st = ui_guard("batch_stats")
                if st is not None:
                    st.set_text(
                        f"完成 {ev['done']}/{ev['total']} · ✅ {ev['ok']} · ❌ {ev['fail']} · "
                        f"⏱ {ev['elapsed']:.0f}s")
        except Exception:
            pass

    try:
        result = await core.run_batch(targets, state.current, state.settings, state.client, _cb)
        if result["aborted"]:
            UI["batch_status"].set_text(f"⏹ 已终止（本次完成 {result['done']}/{result['total']}）")
        else:
            UI["batch_status"].set_text(f"✅ 完成（成功 {result['ok']} · 失败 {result['fail']}）")
        if result["fail"] > 0:
            UI["batch_retry_btn"].set_text(f"重试失败 {result['fail']}")
            UI["batch_retry_btn"].set_visibility(True)
    except asyncio.CancelledError:
        add_log("批量已终止")
        UI["batch_status"].set_text("⏹ 已终止")
        raise
    finally:
        mp = ui_guard("main_progress")
        if mp is not None:
            mp.set_visibility(False)
    UI["batch_stop_btn"].set_enabled(False)
    state.abort_batch = False
    update_tokens()


# ---------------- 导出 ----------------
def export_zip() -> None:
    if not state.current:
        ui.notify("请先选择项目", type="warning")
        return
    try:
        zip_path = core.export_zip(state.current)
    except Exception as e:
        ui.notify(f"导出失败：{e}", type="negative")
        return
    ui.download(str(zip_path), filename=f"{zip_path.name}")
    ui.notify(f"已导出 {zip_path.name}")


# ---------------- 新建 / 删除项目 ----------------
def new_project() -> None:
    dlg = UI["project_dialog"]
    with dlg.clear(), ui.card().classes("items-center gap-2"):
        ui.label("新建项目").classes("text-lg font-bold")
        inp = ui.input("项目名称").props("outlined dense").classes("w-64")
        with ui.row():
            ui.button("取消", on_click=dlg.close)
            ui.button("创建", on_click=lambda: do_new_project(inp.value, dlg)).props("color=primary")
    dlg.open()


def do_new_project(name, dlg) -> None:
    dlg.close()
    err = core.create_project(name)
    if err:
        ui.notify(err, type="warning")
        return
    clean = core.sanitize_project_name(name)
    ui.notify(f"已创建项目 {clean}")
    refresh_project_list()
    select_project(clean)


def delete_project() -> None:
    if not state.current:
        ui.notify("请先选择项目", type="warning")
        return
    dlg = UI["confirm_delete_project"]
    with dlg.clear(), ui.card():
        ui.label(f"确定删除整个项目「{state.current}」吗？该操作不可恢复。").classes("text-red-500")
        with ui.row():
            ui.button("取消", on_click=dlg.close)
            ui.button("删除", on_click=do_delete_project).props("color=red-7")
    dlg.open()


def do_delete_project() -> None:
    UI["confirm_delete_project"].close()
    if not state.current:
        return
    name = state.current
    core.delete_project(name)
    ui.notify(f"已删除项目 {name}")
    state.current = None
    UI["toolbar_title"].set_text("（未选择项目）")
    refresh_project_list()
    background_tasks.create(refresh_grid())


# ---------------- 设置变更 ----------------
def set_setting(key: str, value) -> None:
    state.settings[key] = value
    save_settings()
    UI["batch_button"].set_enabled(client_ready())
    render_api_banner()
    update_header_meta()


def on_preset_change(e: events.ValueChangeEventArguments) -> None:
    """切换模型预设：回填 Base URL / 模型名，并应用该预设记住的 API Key。"""
    name = e.value
    preset = MODEL_PRESETS.get(name)
    if not preset:
        return
    UI["base_url_input"].value = preset["base_url"]
    UI["model_input"].value = preset["model"]
    set_setting("base_url", preset["base_url"])
    set_setting("model", preset["model"])
    saved_key = (state.settings.get("preset_keys") or {}).get(name, "")
    if saved_key:
        UI["api_key_input"].value = saved_key
        set_setting("api_key", saved_key)
    else:
        ui.notify("该预设未保存 API Key，可手动填写（会自动记住到该预设）", type="info", timeout=3000)


def on_api_key_change(e: events.ValueChangeEventArguments) -> None:
    """保存 API Key，并把当前输入的 Key 记住到当前选中的模型预设（切换预设时自动回填）。"""
    key = e.value or ""
    set_setting("api_key", key)
    sel = UI.get("model_preset_select")
    if sel is not None and sel.value in MODEL_PRESETS and key.strip():
        state.settings.setdefault("preset_keys", {})[sel.value] = key
        save_settings()


def set_prompt_text(value: str) -> None:
    """静默更新提示词文本框（不触发「视为自定义」判定）。"""
    state.suppress_prompt_sync = True
    try:
        UI["prompt_textarea"].value = value
    finally:
        state.suppress_prompt_sync = False


def on_prompt_text_change(e: events.ValueChangeEventArguments) -> None:
    """手动编辑提示词：自动切到「自定义」预设并保存。"""
    if state.suppress_prompt_sync:
        return
    if state.settings.get("prompt_preset") != "custom":
        state.settings["prompt_preset"] = "custom"
        UI["prompt_select"].value = "custom"
    set_setting("system_prompt", e.value or "")


# 角色 LoRA 预设键（注入角色名用）


def on_prompt_preset_change(e: events.ValueChangeEventArguments) -> None:
    """选择提示词预设：按「输出语言 × 格式 × 训练目标」填充对应提示词；「自定义」保留现有文本。

    预设键形如 <语言>_<格式>_<目标>，例如 en_short_character / zh_natural_style。
    """
    preset = e.value
    state.settings["prompt_preset"] = preset
    if preset == "custom":
        save_settings()
        return
    text = core.resolve_prompt_text(preset)
    set_prompt_text(text)
    state.settings["system_prompt"] = text
    save_settings()


def on_character_name_change(e: events.ValueChangeEventArguments) -> None:
    """保存角色名；若当前预设为「角色 LoRA」，实时把名称注入提示词。"""
    set_setting("character_name", e.value or "")
    preset = state.settings.get("prompt_preset", "")
    if preset in core.CHARACTER_PRESETS:
        text = core.resolve_prompt_text(preset)
        set_prompt_text(text)
        state.settings["system_prompt"] = text
        save_settings()


def restore_default_prompt() -> None:
    """恢复为「英文 · 短标签 · 通用」并填入对应默认提示词。"""
    state.settings["prompt_preset"] = "en_short_default"
    UI["prompt_select"].value = "en_short_default"
    text = core.resolve_prompt_text("en_short_default")
    set_prompt_text(text)
    state.settings["system_prompt"] = text
    save_settings()
    ui.notify("已恢复为「英文 · 短标签 · 通用」默认提示词")





# ---------------- 界面构建 ----------------
CSS = """
/* ===== TagForge 主题 ===== */
:root {
  --tf-primary: #6366f1;
  --tf-bg: #f2f3f8;
  --tf-surface: #ffffff;
  --tf-border: #e7e9f2;
  --tf-text: #1f2430;
  --tf-muted: #7b8294;
}
.body--dark {
  --tf-bg: #0e1016;
  --tf-surface: #161a24;
  --tf-border: #272c3b;
  --tf-text: #e6e8f0;
  --tf-muted: #9aa1b4;
}
html, body { background: var(--tf-bg) !important; }
body { font-family: "Inter", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", system-ui, -apple-system, sans-serif; }

.tf-text { color: var(--tf-text); }
.tf-muted { color: var(--tf-muted); }

/* 顶栏 */
.tf-header {
  background: linear-gradient(92deg, #4f46e5 0%, #7c3aed 62%, #9333ea 100%) !important;
  color: #fff;
  box-shadow: 0 2px 14px rgba(79, 70, 229, .35);
  height: 56px;
}
.tf-logo { letter-spacing: .3px; }
.tf-tagline { font-size: .72rem; opacity: .8; }

/* 抽屉 */
.tf-drawer .q-drawer__content { background: var(--tf-surface) !important; }
.tf-drawer { max-width: 560px; }
.tf-section { font-size: .72rem; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; color: var(--tf-muted); }
.tf-label { font-size: .72rem; font-weight: 600; color: var(--tf-muted); }

/* 表单圆角 */
.q-field--outlined .q-field__control { border-radius: 10px !important; }

/* 图片卡片 */
.tf-card {
  border-radius: 14px;
  background: var(--tf-surface) !important;
  border: 1px solid var(--tf-border) !important;
  box-shadow: 0 1px 2px rgba(16, 24, 40, .06);
  padding: 0 !important;
  overflow: hidden;
  position: relative;
  transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
  cursor: pointer;
}
.tf-card:hover { transform: translateY(-3px); box-shadow: 0 12px 28px rgba(16, 24, 40, .14); border-color: #c9cdf1 !important; }
.tf-card-img { width: 100%; height: 150px; object-fit: cover; display: block; background: #e9ebf3; }
.tf-img-err { display: flex; align-items: center; justify-content: center; font-size: .72rem; }
.body--dark .tf-card-img { background: #20242f; }
.tf-card-caption {
  font-size: .72rem; line-height: 1.45; color: var(--tf-muted);
  padding: .45rem .7rem 0;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden; word-break: break-all;
}
.tf-card-name { font-size: .78rem; font-weight: 600; color: var(--tf-text); padding: .35rem .7rem .55rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tf-badge {
  position: absolute; top: 8px; right: 8px;
  font-size: .66rem; font-weight: 600; padding: 3px 9px;
  border-radius: 999px; letter-spacing: .02em;
  backdrop-filter: blur(2px);
  border: 1px solid rgba(255,255,255,.35);
}
.tf-check {
  position: absolute; top: 8px; left: 8px; z-index: 2;
  width: 20px; height: 20px; border-radius: 9999px;
  color: #fff; font-size: 13px; line-height: 20px; text-align: center;
  box-shadow: 0 1px 3px rgba(0,0,0,.3);
}
.body--dark .tf-check { box-shadow: 0 0 0 2px var(--tf-surface), 0 1px 3px rgba(0,0,0,.4); }
.body--dark .tf-badge { border-color: rgba(255,255,255,.18); }
.body--dark .q-field--outlined .q-field__control { border-color: var(--tf-border); }
.cursor-zoom-in { cursor: zoom-in !important; }
.cursor-zoom-out { cursor: zoom-out !important; }

/* 工具栏 */
.tf-toolbar { border-radius: 14px; background: var(--tf-surface) !important; border: 1px solid var(--tf-border) !important; }
.tf-toolbar-title { font-size: 1.05rem; font-weight: 700; color: var(--tf-text); }
.tf-wide { width: 100%; max-width: 1440px; }

/* 进度条 / 弹窗 / 日志 */
.q-linear-progress { border-radius: 8px; }
.q-dialog .q-card { border-radius: 18px !important; }
.tf-log { background: rgba(120, 130, 160, .08); border: 1px solid var(--tf-border); border-radius: 12px; }

/* Token 角落 */
.tf-token {
  background: var(--tf-surface); border: 1px solid var(--tf-border); border-radius: 999px;
  padding: 6px 14px; font-size: .7rem; color: var(--tf-muted);
  box-shadow: 0 2px 8px rgba(16, 24, 40, .08);
}
"""


def build_ui() -> None:
    state.settings = load_settings()
    if not SETTINGS_FILE.exists():
        save_settings()  # 首次运行自动创建配置并写入默认值
    UI["dark"] = ui.dark_mode(state.settings.get("dark", False))
    ui.page_title("TagForge — LoRA 图片打标工具")

    ui.add_head_html("<style>" + CSS + "</style>")

    with ui.header().classes("tf-header items-center px-5"):
        with ui.row().classes("items-center gap-3"):
            ui.label("⚒️ TagForge").classes("tf-logo text-xl font-bold")
            ui.label("LoRA 数据集图片打标工具").classes("tf-tagline hidden sm:block")
        with ui.row().classes("ml-auto items-center gap-3"):
            with ui.column().classes("gap-0 items-end"):
                UI["header_model"] = ui.label("").classes("text-xs text-white/80")
                UI["header_tokens"] = ui.label("").classes("text-xs text-white/60")
            def toggle_dark(e):
                UI["dark"].enable() if e.value else UI["dark"].disable()
                set_setting("dark", e.value)
            UI["dark_switch"] = ui.switch("深色", value=bool(state.settings.get("dark")),
                                         on_change=toggle_dark)
            def show_help():
                with UI["help_dialog"], ui.card():
                    ui.label("TagForge — LoRA 数据集图片打标工具").classes("text-lg font-bold")
                    ui.label("左侧配置模型并新建/选择项目；上传图片后点击卡片进行标注；"
                             "「开始批量标注」会并发调用大模型为待标注/失败图片生成标签。").classes("text-sm")
                    with ui.row().classes("justify-end w-full"):
                        ui.button("知道了", on_click=UI["help_dialog"].close).props("flat")
                UI["help_dialog"].open()
            ui.button(icon="help_outline", on_click=show_help).props("flat round color=white")

    # ---- 左侧面板：项目 / 配置 / 高级（P1-7 分页折叠） ----
    with ui.left_drawer(value=True, fixed=True).props("bordered").classes("tf-drawer w-80"):
        with ui.tabs().classes("w-full") as drawer_tabs:
            tab_proj = ui.tab("项目", icon="folder")
            tab_cfg = ui.tab("配置", icon="tune")
            tab_adv = ui.tab("高级", icon="more_vert")
        with ui.tab_panels(drawer_tabs, value=tab_proj).classes("w-full grow").props("animated"):
            # ---- 项目 ----
            with ui.tab_panel(tab_proj):
                with ui.column().classes("w-full gap-3 p-3"):
                    ui.button("新建项目", icon="add", on_click=new_project) \
                        .props("unelevated rounded color=primary dense").classes("w-full")
                    with ui.column().classes("w-full h-56 overflow-y-auto gap-1"):
                        UI["project_list"] = ui.column().classes("w-full gap-1")
                    ui.button("删除项目", icon="delete_outline", on_click=delete_project) \
                        .props("flat dense rounded color=red-6").classes("w-full")
            # ---- 配置（模型 + 提示词） ----
            with ui.tab_panel(tab_cfg):
                with ui.column().classes("w-full gap-1 p-3"):
                    UI["model_preset_select"] = ui.select(list(MODEL_PRESETS.keys()),
                                                               label="模型预设", on_change=on_preset_change) \
                        .props("outlined dense").classes("w-full")
                    UI["base_url_input"] = ui.input(label="Base URL",
                                                    value=state.settings.get("base_url")) \
                        .props("outlined dense").classes("w-full") \
                        .on_value_change(lambda e: set_setting("base_url", e.value))
                    UI["model_input"] = ui.input(label="模型名", value=state.settings.get("model")) \
                        .props("outlined dense").classes("w-full") \
                        .on_value_change(lambda e: set_setting("model", e.value))
                    UI["api_key_input"] = ui.input(label="API Key", value=state.settings.get("api_key")) \
                        .props("outlined dense type=password").classes("w-full") \
                        .on_value_change(on_api_key_change)
                    with ui.row().classes("w-full items-center gap-2"):
                        UI["test_btn"] = ui.button("测试连接", icon="wifi_tethering",
                                                   on_click=test_api) \
                            .props("unelevated rounded color=primary dense").classes("flex-1")
                        UI["test_spinner"] = ui.spinner(size="sm", color="primary").set_visibility(False)

                    ui.separator()
                    ui.label("提示词预设").classes("tf-label")
                    UI["prompt_select"] = ui.select(PROMPT_PRESETS, label="选择预设",
                                                    value=state.settings.get("prompt_preset"),
                                                    on_change=on_prompt_preset_change) \
                        .props("outlined dense").classes("w-full")
                    UI["prompt_textarea"] = ui.textarea(label="提示词内容（可直接编辑）",
                                                        value=state.settings.get("system_prompt")) \
                        .classes("w-full h-36").props("outlined dense") \
                        .on_value_change(on_prompt_text_change)
                    ui.button("恢复默认", on_click=restore_default_prompt).props("flat dense")
                    ui.label("选择预设自动填充；手动编辑提示词将切为「自定义」") \
                        .classes("tf-muted text-xs")

                    ui.label("角色名（角色 LoRA）").classes("tf-label")
                    UI["character_name_input"] = ui.input(
                        label="角色名（如 mw_cyber_girl）",
                        value=state.settings.get("character_name")) \
                        .props("outlined dense").classes("w-full") \
                        .on_value_change(on_character_name_change)
                    ui.label("填写后，「角色 LoRA」预设会用该名称称呼角色并置于输出开头") \
                        .classes("tf-muted text-xs")
            # ---- 高级（前缀 / 并发 / 导出） ----
            with ui.tab_panel(tab_adv):
                with ui.column().classes("w-full gap-1 p-3"):
                    ui.label("触发词前缀").classes("tf-label")
                    ui.input(value=state.settings.get("tag_prefix")) \
                        .props("outlined dense").classes("w-full") \
                        .on_value_change(lambda e: set_setting("tag_prefix", e.value))
                    ui.radio({"prepend": "整体前置", "per_tag": "每个 tag 加前缀"},
                             value=state.settings.get("prefix_mode"),
                             on_change=lambda e: set_setting("prefix_mode", e.value)).props("dense")

                    ui.label("并发数").classes("tf-label")
                    ui.number(value=state.settings.get("concurrency"), min=1, max=32, step=1) \
                        .props("outlined dense").classes("w-full") \
                        .on_value_change(lambda e: set_setting("concurrency", int(e.value)))

                    ui.separator()
                    ui.button("打包导出", icon="archive", on_click=export_zip) \
                        .props("unelevated rounded color=teal-7").classes("w-full")

    # ---- 主区域 ----
    with ui.column().classes("w-full items-center px-6 pt-4"):
        with ui.card().props("flat").classes("tf-toolbar tf-wide px-4 py-2.5"):
            with ui.row().classes("w-full items-center gap-3 flex-wrap"):
                UI["toolbar_title"] = ui.label("（未选择项目）").classes("tf-toolbar-title")
                with ui.element("div").classes("inline-flex"):
                    UI["uploader"] = ui.upload(multiple=True, auto_upload=True,
                                               on_upload=on_upload,
                                               on_begin_upload=on_upload_begin,
                                               on_multi_upload=on_multi_upload) \
                        .classes("hidden")
                    ui.button("上传图片", icon="upload",
                              on_click=lambda: UI["uploader"].run_method("pickFiles")) \
                        .props("unelevated rounded color=indigo-6")
                UI["upload_status"] = ui.label("").classes("tf-muted text-xs").set_visibility(False)
                UI["batch_button"] = ui.button("开始批量标注", icon="auto_awesome", on_click=start_batch) \
                    .props("unelevated rounded color=primary").set_enabled(client_ready())
                UI["trial_btn"] = ui.button("试生成 1 张", icon="bolt", on_click=trial_generate) \
                    .props("outline rounded color=teal-7").set_enabled(client_ready())
                UI["main_progress"] = ui.linear_progress(value=0.0, show_value=True) \
                    .classes("w-56").set_visibility(False)

        with ui.column().classes("w-full items-center pt-3 pb-10"):
            UI["meta_bar"] = ui.row().classes("tf-wide items-center gap-3 flex-wrap my-1")
            with ui.row().classes("tf-wide mt-2") as UI["api_banner"]:
                with ui.card().props("flat").classes("tf-toolbar w-full px-4 py-2"):
                    ui.label("⚠️ 尚未配置可用的 API（需 Base URL / 模型名；Ollama 之外还需 API Key）。"
                             "配置后点「测试连接」验证可用后再批量标注。") \
                        .classes("tf-muted text-xs")
            UI["api_banner"].set_visibility(False)
            with ui.card().props("flat").classes("tf-toolbar tf-wide mt-2 p-4 gap-2 tf-batch-card") as UI["batch_card"]:
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("批量标注").classes("tf-text font-bold")
                    UI["batch_status"] = ui.label("处理中…").classes("text-sm")
                    ui.button(icon="close", on_click=collapse_batch) \
                        .props("flat round color=grey-7").tooltip("收起（后台继续，进度见顶栏）")
                UI["batch_progress"] = ui.linear_progress(value=0.0, show_value=True).classes("w-full")
                UI["batch_stats"] = ui.label("").classes("tf-muted text-sm")
                with ui.row().classes("w-full items-center justify-between flex-wrap gap-2"):
                    UI["batch_stop_btn"] = ui.button("终止", icon="stop", on_click=stop_batch) \
                        .props("unelevated rounded color=red-6")
                    with ui.row().classes("items-center gap-1"):
                        UI["batch_log_toggle"] = ui.button("日志", icon="list_alt",
                                                           on_click=toggle_batch_log) \
                            .props("flat dense rounded")
                        UI["batch_log_clear_btn"] = ui.button("清空", icon="cleaning_services",
                                                              on_click=clear_batch_log) \
                            .props("flat dense rounded")
                        UI["batch_copy_btn"] = ui.button("复制", icon="content_copy",
                                                         on_click=copy_batch_log) \
                            .props("flat dense rounded")
                        UI["batch_retry_btn"] = ui.button("重试失败", icon="refresh",
                                                          on_click=retry_failed) \
                            .props("unelevated rounded color=amber-7").set_visibility(False)
                with ui.column().classes("w-full h-32 overflow-y-auto tf-log p-2 gap-1") as UI["batch_log_area"]:
                    UI["batch_log"] = ui.column().classes("w-full gap-1")
            UI["batch_card"].set_visibility(False)
            UI["grid"] = ui.grid(columns="repeat(auto-fill, minmax(190px, 1fr))") \
                .classes("tf-wide gap-4")
            UI["load_more_btn"] = ui.button("加载更多", icon="expand_more",
                                            on_click=load_more) \
                .props("outline rounded color=primary").classes("mt-4") \
                .set_visibility(False)

    # ---- 右侧详情面板 ----
    UI["drawer"] = ui.right_drawer(value=False, fixed=True).props("width=40% bordered") \
        .classes("tf-drawer")

    # ---- 弹窗 ----
    with ui.dialog().props("maximized") as UI["lightbox_dialog"]:
        with ui.column().classes("w-full h-full items-center justify-center bg-black") \
                .on("click", lightbox_close):
            UI["lightbox_img"] = ui.image("") \
                .props("fit=contain") \
                .classes("w-full h-full cursor-zoom-out")
            ui.label("点击任意处或按 Esc 关闭").classes("tf-muted text-xs")

    with ui.dialog() as UI["help_dialog"]:
        pass

    with ui.keyboard(on_key=handle_shortcuts):
        pass

    with ui.dialog() as UI["project_dialog"]:
        pass

    with ui.dialog() as UI["confirm_dialog"]:
        pass

    with ui.dialog() as UI["confirm_delete_project"]:
        pass

    # 初始加载：优先恢复上次项目，缺失时回退到第一个项目
    refresh_project_list()
    remembered = state.settings.get("last_project") or ""
    state.current = remembered if remembered in state.projects else (state.projects[0] if state.projects else None)
    if state.current:
        UI["toolbar_title"].set_text(state.current)
        background_tasks.create(refresh_grid())
    update_header_meta()


@ui.page('/')
def index_page() -> None:
    build_ui()

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(host="0.0.0.0", port=8080, reload=False)
