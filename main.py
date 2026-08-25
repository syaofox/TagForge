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

# ---------------- 常量与路径 ----------------
ROOT = Path(__file__).resolve().parent
DATASETS = ROOT / "datasets"
CONFIG = ROOT / "config"
SETTINGS_FILE = CONFIG / "settings.json"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

DEFAULT_SETTINGS = {
    "api_key": "",
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "mode": "short",
    "system_prompt": (
        "You are an image captioning assistant. "
        "Generate 5-10 comma-separated tags (danbooru style, lowercase) for the image. "
        "Output only the tags."
    ),
    "tag_prefix": "",
    "prefix_mode": "prepend",
    "dark": False,
    "concurrency": 5,
}

# 模型预设（名称 -> Base URL / 默认模型）。Claude 需中转站、DeepSeek-VL 需自建端点、Ollama 需 /v1。
MODEL_PRESETS = {
    "DeepSeek (官方)": {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash-vision-exp"},
    "OpenAI (GPT-4o)": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "Claude 3.5（中转站）": {"base_url": "https://api.anthropic.com/v1", "model": "claude-3-5-sonnet-latest"},
    "DeepSeek-VL（自建/中转）": {"base_url": "", "model": "deepseek-vl2"},
    "Ollama（本地）": {"base_url": "http://localhost:11434/v1", "model": "llava"},
}

STATUS_TEXT = {
    "tagged": "🟢 已标注",
    "pending": "🟡 待标注",
    "processing": "🔵 处理中",
    "failed": "🔴 失败",
}
STATUS_COLOR = {
    "tagged": "#22c55e",
    "pending": "#eab308",
    "processing": "#3b82f6",
    "failed": "#ef4444",
}

# ---------------- 应用状态 ----------------
@dataclass
class ImageEntry:
    name: str
    path: Path
    status: str = "pending"
    thumb: str = ""
    badge: Optional[ui.element] = None


@dataclass
class AppState:
    settings: dict = field(default_factory=dict)
    projects: list = field(default_factory=list)
    current: Optional[str] = None
    entries: list = field(default_factory=list)
    client: Optional[LLMClient] = None
    batch: Optional[asyncio.Task] = None
    abort_batch: bool = False
    index: int = 0
    tagbox: Optional[ui.textarea] = None


state = AppState()
UI: dict = {}  # 复用元素的引用


# ---------------- 配置读写 ----------------
def load_settings() -> dict:
    if not SETTINGS_FILE.is_file():
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    return merged


def save_settings() -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(state.settings, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 文件扫描 / 状态判定 ----------------
def scan_projects() -> list:
    if not DATASETS.is_dir():
        return []
    # 排除隐藏目录（如缩略图缓存 .cache）与以点开头的目录
    return sorted(p.name for p in DATASETS.iterdir() if p.is_dir() and not p.name.startswith("."))


def images_dir(project: str) -> Path:
    return DATASETS / project / "images"


def project_images(project: str) -> list:
    d = images_dir(project)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXTS)


def label_file(project: str, image_name: str) -> Path:
    return DATASETS / project / "labels" / f"{Path(image_name).stem}.txt"


def read_status(project: str, image_name: str) -> str:
    lp = label_file(project, image_name)
    if lp.is_file() and lp.read_text(encoding="utf-8").strip():
        return "tagged"
    return "pending"


def read_label(project: str, image_name: str) -> str:
    lp = label_file(project, image_name)
    return lp.read_text(encoding="utf-8") if lp.is_file() else ""


def write_label(project: str, image_name: str, text: str) -> None:
    d = DATASETS / project / "labels"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{Path(image_name).stem}.txt").write_text(text, encoding="utf-8")


def apply_prefix(text: str, prefix: str, mode: str) -> str:
    """按 prefix_mode 对生成文本应用触发词前缀，并避免重复叠加。"""
    prefix = prefix.strip()
    if not prefix:
        return text
    pre = prefix.rstrip(",").strip()
    if mode == "per_tag":
        tags = [t.strip() for t in text.split(",") if t.strip()]
        return ", ".join(t if t.startswith(pre) else f"{pre}, {t}" for t in tags)
    if not text:
        return pre
    return text if text.startswith(pre) else f"{pre}, {text}"


# ---------------- 图片处理（供 run.cpu_bound 的进程池调用，须为模块级函数） ----------------
def make_thumb(project: str, name: str, max_side: int = 360) -> str:
    """生成（并缓存到 datasets/.cache/<项目>/ ）缩略图，返回 data URL。"""
    src = images_dir(project) / name
    cache_dir = DATASETS / ".cache" / project
    cache = cache_dir / f"{Path(name).stem}.thumb.jpg"
    if cache.is_file() and cache.stat().st_mtime >= src.stat().st_mtime:
        data = cache.read_bytes()
    else:
        with Image.open(src) as im:
            im.thumbnail((max_side, max_side))
            if im.mode != "RGB":
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
            data = buf.getvalue()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def encode_for_api(path: Path) -> bytes:
    """发送给大模型前的预处理：缩放 + JPEG 压缩，返回 JPEG bytes。"""
    with Image.open(path) as im:
        im.thumbnail((1280, 1280))
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        return buf.getvalue()


# ---------------- 模型客户端 ----------------
def client_ready() -> bool:
    s = state.settings
    base = (s.get("base_url") or "").strip()
    key = (s.get("api_key") or "").strip()
    return bool(base) and (bool(key) or "ollama" in base.lower())


def ensure_client() -> bool:
    s = state.settings
    base = (s.get("base_url") or "").strip()
    model = (s.get("model") or "").strip()
    if not base or not model:
        ui.notify("请先配置 Base URL 与模型", type="warning")
        return False
    if not (s.get("api_key") or "").strip() and "ollama" not in base.lower():
        ui.notify("API Key 为空，无法调用 API", type="warning")
        return False
    state.client = LLMClient(api_key=s.get("api_key"), base_url=base, model_name=model)
    return True


def update_tokens() -> None:
    c = state.client
    if c is None:
        return
    inp, out = c.total_prompt_tokens, c.total_completion_tokens
    UI["tokens"].set_text(f"Tokens：输入 {inp} / 输出 {out}（累计 {inp + out}）")


# ---------------- 徽章 / 状态 ----------------
def set_badge(entry: ImageEntry, status: str) -> None:
    entry.status = status
    if entry.badge is not None:
        entry.badge.set_text(STATUS_TEXT[status])
        entry.badge.style(f"background-color:{STATUS_COLOR[status]}")


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
                ui.button(name, on_click=lambda n=name: select_project(n)) \
                    .props("flat dense align-left").classes("w-full justify-start")


def select_project(name: str) -> None:
    if state.batch and not state.batch.done():
        ui.notify("有批量标注正在进行，请先终止", type="warning")
        return
    state.current = name
    UI["toolbar_title"].set_text(name)
    background_tasks.create(refresh_grid())


async def refresh_grid() -> None:
    grid = UI["grid"]
    grid.clear()
    if not state.current:
        with grid:
            ui.label("请先在左侧选择或新建一个项目。").classes("text-gray-400")
        return

    imgs = project_images(state.current)
    entries = [
        ImageEntry(name=p.name, path=p, status=read_status(state.current, p.name))
        for p in imgs
    ]
    # 并行生成缩略图（进程池），避免阻塞事件循环
    thumbs = await asyncio.gather(
        *(run.io_bound(make_thumb, state.current, e.name) for e in entries)
    )
    for e, t in zip(entries, thumbs):
        e.thumb = t
    state.entries = entries

    with grid:
        for i, entry in enumerate(entries):
            card = ui.card().classes("w-full h-60 cursor-pointer overflow-hidden") \
                .mark("image-card") \
                .on("click", lambda i=i: open_detail(i))
            with card:
                ui.image(entry.thumb).classes("w-full h-40 object-cover")
                ui.label(entry.name).classes("text-xs text-gray-500 truncate w-full")
                entry.badge = ui.label(STATUS_TEXT[entry.status]).classes(
                    "px-2 py-0.5 rounded text-white text-xs")
                entry.badge.style(f"background-color:{STATUS_COLOR[entry.status]}")


# ---------------- 详情面板 ----------------
def open_detail(i: int) -> None:
    if not state.entries:
        return
    state.index = i
    UI["drawer"].show()
    render_detail()


def render_detail() -> None:
    drawer = UI["drawer"]
    drawer.clear()
    entry = state.entries[state.index]
    with drawer:
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(entry.name).classes("font-bold")
            ui.button(icon="close", on_click=drawer.hide).props("flat dense")
        ui.image(entry.thumb).classes("w-full max-h-96 object-contain")
        state.tagbox = ui.textarea(label="标签文本", value=read_label(state.current, entry.name)) \
            .classes("w-full").props("outlined dense")
        with ui.row():
            ui.button("保存", on_click=save_tag).props("color=green-7")
            ui.button("重新生成", on_click=regenerate).props("color=blue-7")
            ui.button("删除图片", on_click=confirm_delete).props("color=red-7")
        with ui.row().classes("w-full items-center justify-between"):
            ui.button(icon="navigate_before", on_click=prev_img).props("flat round")
            ui.label(f"{state.index + 1} / {len(state.entries)}").classes("self-center")
            ui.button(icon="navigate_next", on_click=next_img).props("flat round")


def save_tag() -> None:
    if not state.current or not state.entries or state.tagbox is None:
        return
    entry = state.entries[state.index]
    text = state.tagbox.value or ""
    write_label(state.current, entry.name, text)
    set_badge(entry, "tagged" if text.strip() else "pending")
    ui.notify("已保存")


async def regenerate() -> None:
    if not ensure_client():
        return
    entry = state.entries[state.index]
    set_badge(entry, "processing")
    try:
        data = "data:image/jpeg;base64," + base64.b64encode(
            await run.io_bound(encode_for_api, entry.path)).decode("ascii")
        tags = await state.client.generate(
            data, state.settings.get("system_prompt", ""), state.settings.get("mode", "short"))
        final = apply_prefix(tags, state.settings.get("tag_prefix", ""),
                             state.settings.get("prefix_mode", "prepend"))
        write_label(state.current, entry.name, final)
        if state.tagbox is not None:
            state.tagbox.set_value(final)
        set_badge(entry, "tagged")
        update_tokens()
        ui.notify("重新生成完成")
    except FatalAPIError as e:
        set_badge(entry, "failed")
        ui.notify(str(e), type="negative")
    except Exception as e:
        set_badge(entry, "failed")
        ui.notify(f"生成失败：{e}", type="negative")


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
async def on_upload(e: events.UploadEventArguments) -> None:
    if not state.current:
        ui.notify("请先选择项目", type="warning")
        return
    file = e.file
    name = file.name or ""
    if Path(name).suffix.lower() not in IMAGE_EXTS:
        ui.notify(f"不支持的文件类型：{name}", type="negative")
        return
    dst = images_dir(state.current) / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    await file.save(dst)
    # 同名覆盖策略：清除旧同名 .txt 标签
    lp = label_file(state.current, name)
    if lp.exists():
        lp.unlink()
    ui.notify(f"已上传 {name}")
    await refresh_grid()


# ---------------- 批量标注 ----------------
def add_log(text: str) -> None:
    box = UI["batch_log"]
    if box is not None:
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
    state.abort_batch = False
    UI["batch_total"].set_text(f"待处理 {len(targets)} 张")
    UI["batch_progress"].value = 0.0
    UI["batch_log"].clear()
    UI["main_progress"].set_visibility(True)
    UI["batch_modal"].open()
    state.batch = background_tasks.create(run_batch(targets))


def stop_batch() -> None:
    if state.batch and not state.batch.done():
        state.abort_batch = True
        add_log("⏹ 已请求终止…")


def close_batch_modal() -> None:
    UI["batch_modal"].close()


def run_in_background() -> None:
    close_batch_modal()
    ui.notify("已在后台运行，可在顶栏查看进度")


async def run_batch(targets: list) -> None:
    sem = asyncio.Semaphore(int(state.settings.get("concurrency") or 5))
    total = len(targets)
    done = 0
    prop = state.settings.get("system_prompt", "")
    mode = state.settings.get("mode", "short")
    prefix = state.settings.get("tag_prefix", "")
    prefix_mode = state.settings.get("prefix_mode", "prepend")

    async def process(entry: ImageEntry) -> None:
        nonlocal done
        if state.abort_batch:
            return
        async with sem:
            if state.abort_batch:
                return
            set_badge(entry, "processing")
            t0 = time.perf_counter()
            try:
                data = "data:image/jpeg;base64," + base64.b64encode(
                    await run.io_bound(encode_for_api, entry.path)).decode("ascii")
                tags = await state.client.generate(data, prop, mode)
                final = apply_prefix(tags, prefix, prefix_mode)
                write_label(state.current, entry.name, final)
                set_badge(entry, "tagged")
                add_log(f"{entry.name} ✅ 成功 ({time.perf_counter() - t0:.1f}s)")
            except FatalAPIError as e:
                set_badge(entry, "failed")
                add_log(f"{entry.name} ⚠️ {e}")
                state.abort_batch = True
            except Exception as e:
                set_badge(entry, "failed")
                add_log(f"{entry.name} ❌ 失败：{e}")
            finally:
                done += 1
                UI["batch_progress"].value = done / total
                UI["main_progress"].value = done / total

    try:
        results = await asyncio.gather(*(process(t) for t in targets), return_exceptions=True)
    except asyncio.CancelledError:
        add_log("批量已终止")
        for t in targets:
            if t.status == "processing":
                set_badge(t, "pending")
        raise
    finally:
        UI["main_progress"].set_visibility(False)

    if any(isinstance(r, FatalAPIError) for r in results):
        add_log("⚠️ 批量中止：API Key/权限错误或重试后仍失败")
        for t in targets:
            if t.status == "processing":
                set_badge(t, "pending")
    else:
        ok = sum(1 for t in targets if t.status == "tagged")
        add_log(f"🎉 完成：成功 {ok} / {total}")

    state.abort_batch = False
    update_tokens()


# ---------------- 导出 ----------------
def export_zip() -> None:
    if not state.current:
        ui.notify("请先选择项目", type="warning")
        return
    proj = state.current
    zip_dir = ROOT / "exports"
    zip_dir.mkdir(parents=True, exist_ok=True)
    zip_path = zip_dir / f"{proj}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in project_images(proj):
            z.write(p, arcname=f"images/{p.name}")
        labels = DATASETS / proj / "labels"
        if labels.is_dir():
            for lp in sorted(labels.iterdir()):
                if lp.suffix.lower() == ".txt":
                    z.write(lp, arcname=f"labels/{lp.name}")
    ui.download(str(zip_path), filename=f"{proj}.zip")
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
    name = "".join(ch for ch in (name or "").strip() if ch.isalnum() or ch in "_- ").strip()
    dlg.close()
    if not name:
        ui.notify("项目名不能为空", type="warning")
        return
    proj_dir = DATASETS / name
    (proj_dir / "images").mkdir(parents=True, exist_ok=True)
    (proj_dir / "labels").mkdir(parents=True, exist_ok=True)
    ui.notify(f"已创建项目 {name}")
    refresh_project_list()
    select_project(name)


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
    shutil.rmtree(DATASETS / name, ignore_errors=True)
    shutil.rmtree(DATASETS / ".cache" / name, ignore_errors=True)
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


def on_preset_change(e: events.ValueChangeEventArguments) -> None:
    preset = MODEL_PRESETS.get(e.value)
    if not preset:
        return
    UI["base_url_input"].value = preset["base_url"]
    UI["model_input"].value = preset["model"]
    set_setting("base_url", preset["base_url"])
    set_setting("model", preset["model"])


def restore_default_prompt() -> None:
    UI["prompt_textarea"].value = DEFAULT_SETTINGS["system_prompt"]
    set_setting("system_prompt", DEFAULT_SETTINGS["system_prompt"])
    ui.notify("已恢复默认提示词")


# ---------------- 界面构建 ----------------
def build_ui() -> None:
    state.settings = load_settings()
    if not SETTINGS_FILE.exists():
        save_settings()  # 首次运行自动创建配置并写入默认值
    UI["dark"] = ui.dark_mode(state.settings.get("dark", False))

    with ui.header().classes("items-center px-4"):
        with ui.row().classes("items-center gap-2"):
            ui.label("⚒️ TagForge").classes("text-xl font-bold")
        with ui.row().classes("ml-auto items-center gap-3"):
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
            ui.button(icon="help_outline", on_click=show_help).props("flat round")

    # ---- 左侧面板：项目管理 + 模型配置 ----
    with ui.left_drawer(value=True, fixed=True).props("bordered").classes("w-80"):
        ui.label("项目").classes("font-bold text-lg")
        with ui.row():
            ui.button("新建项目", on_click=new_project).props("color=primary dense")
            ui.button("删除项目", on_click=delete_project).props("color=red-6 dense")
        with ui.column().classes("w-full h-40 overflow-y-auto my-1 gap-1"):
            UI["project_list"] = ui.column().classes("w-full gap-1")

        ui.separator()
        ui.label("模型配置").classes("font-bold text-lg")
        with ui.column().classes("w-full gap-1"):
            ui.select(list(MODEL_PRESETS.keys()), label="模型预设", on_change=on_preset_change) \
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
                .on_value_change(lambda e: set_setting("api_key", e.value))

            ui.separator()
            ui.label("打标模式").classes("font-bold")
            ui.radio({"short": "短标签（逗号分隔）", "natural": "自然语言描述"},
                     value=state.settings.get("mode"), on_change=lambda e: set_setting("mode", e.value)) \
                .props("dense")

            ui.label("System Prompt").classes("font-bold")
            UI["prompt_textarea"] = ui.textarea(value=state.settings.get("system_prompt")) \
                .classes("w-full h-32").props("outlined dense") \
                .on_value_change(lambda e: set_setting("system_prompt", e.value))
            ui.button("恢复默认", on_click=restore_default_prompt).props("flat dense")

            ui.label("触发词前缀").classes("font-bold")
            ui.input(value=state.settings.get("tag_prefix")) \
                .props("outlined dense").classes("w-full") \
                .on_value_change(lambda e: set_setting("tag_prefix", e.value))
            ui.radio({"prepend": "整体前置", "per_tag": "每个 tag 加前缀"},
                     value=state.settings.get("prefix_mode"),
                     on_change=lambda e: set_setting("prefix_mode", e.value)).props("dense")

            ui.label("并发数").classes("font-bold")
            ui.number(value=state.settings.get("concurrency"), min=1, max=32, step=1) \
                .props("outlined dense").classes("w-full") \
                .on_value_change(lambda e: set_setting("concurrency", int(e.value)))

            ui.separator()
            ui.button("打包导出", on_click=export_zip).props("color=teal-7").classes("w-full")

    # ---- 主区域 ----
    with ui.column().classes("w-full px-4 gap-2"):
        with ui.row().classes("w-full items-center gap-3"):
            UI["toolbar_title"] = ui.label("（未选择项目）").classes("text-lg font-bold")
            ui.upload(multiple=True, auto_upload=True, on_upload=on_upload) \
                .props("label=上传图片 flat bordered").classes("w-48")
            UI["batch_button"] = ui.button("开始批量标注", on_click=start_batch) \
                .props("color=blue-7").set_enabled(client_ready())
            UI["main_progress"] = ui.linear_progress(value=0.0, show_value=True) \
                .classes("w-64").set_visibility(False)

        UI["grid"] = ui.grid(columns="repeat(auto-fill, minmax(170px, 1fr))").classes("w-full gap-3")

    # ---- 右侧详情面板 ----
    UI["drawer"] = ui.right_drawer(value=False, fixed=True).props("width=40% bordered")

    # ---- 弹窗 ----
    with ui.dialog() as UI["batch_modal"]:
        with ui.card().classes("w-120"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("批量标注").classes("text-lg font-bold")
                ui.button(icon="close", on_click=close_batch_modal).props("flat dense")
            UI["batch_total"] = ui.label("")
            UI["batch_progress"] = ui.linear_progress(value=0.0, show_value=True).classes("w-full")
            with ui.column().classes("w-full h-64 overflow-y-auto border rounded-lg p-2"):
                UI["batch_log"] = ui.column().classes("w-full")
            with ui.row().classes("w-full items-center justify-between"):
                ui.button("后台运行", on_click=run_in_background).props("color=primary")
                ui.button("终止", on_click=stop_batch).props("color=red")

    with ui.dialog() as UI["help_dialog"]:
        pass

    with ui.dialog() as UI["project_dialog"]:
        pass

    with ui.dialog() as UI["confirm_dialog"]:
        pass

    with ui.dialog() as UI["confirm_delete_project"]:
        pass

    # ---- 右下角 Token 统计 ----
    with ui.column().classes("fixed bottom-2 right-2"):
        UI["tokens"] = ui.label("Tokens：—").classes("text-xs text-gray-400")

    # 初始加载：页面函数运行于活跃 client 与运行中的事件循环内，可直接填充网格
    refresh_project_list()
    if state.projects:
        state.current = state.projects[0]
        UI["toolbar_title"].set_text(state.current)
        background_tasks.create(refresh_grid())


@ui.page('/')
def index_page() -> None:
    build_ui()

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(host="0.0.0.0", port=8080, reload=False)
