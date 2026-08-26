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

# 通用默认提示词（格式 × 输出语言）
DEFAULT_PROMPTS = {
    "short": {
        "en": (
            "You are an image captioning assistant. "
            "Generate 5-10 comma-separated tags (danbooru style, lowercase) for the image. "
            "Output only the tags."
        ),
        "zh": (
            "你是一名图像打标助手。请为这张图片生成 5-10 个以中文逗号分隔的关键词标签"
            "（简洁、具体，如 白色连衣裙、长发、微笑）。只输出标签本身。"
        ),
    },
    "natural": {
        "en": (
            "You are an image captioning assistant. "
            "Describe the image in one detailed natural-language sentence. "
            "Output only the sentence."
        ),
        "zh": (
            "你是一名图像打标助手。请用一句完整的中文自然语言描述这张图片"
            "（客观、具体，包含主体、动作、环境与光线）。只输出这句话。"
        ),
    },
}

# 训练场景预设提示词（训练目标 × 格式 × 输出语言）
# 依据 LoRA 数据集打标规范整理（见设计文档 11.3 与参考来源）：
# - 角色：省略固定身份特征（脸/瞳/发/体型，交给触发词吸收），保留服装/姿势/表情/镜头/背景/光线，元素顺序一致；
# - 服装：服装是主体，具体描述款式/颜色/面料/版型/细节/褶皱，穿者泛化；
# - 风格：打「内容」不打「风格」，风格词至多 2-3 个稳定词，禁质量词。
TRAINING_PROMPTS = {
    "character": {
        "short": {
            "en": (
                "You are a captioning assistant for CHARACTER LoRA training datasets. "
                "Generate 5-12 comma-separated danbooru-style tags (lowercase, no underscores). "
                "Rule: identity must be absorbed by the trigger token, so OMIT features that are "
                "fixed across the dataset (face shape, eye/hair color, skin, body type). "
                "INCLUDE: clothing and its details, pose/action, expression, shot type "
                "(full_body, close-up, ...), background/setting, lighting, and media type "
                "(1girl, solo, ...). Keep the element order consistent across every image; "
                "consistency matters more than exhaustive detail. Output only the tags."
            ),
            "zh": (
                "你是角色 LoRA 训练数据集的打标助手。请生成 5-12 个以中文逗号分隔的关键词标签"
                "。规则：身份特征要交给触发词吸收，因此省略数据集中固定的特征（脸型、瞳色、"
                "发色、肤色、体型）；必须包含：服装及细节、姿势/动作、表情、镜头类型（全身、"
                "特写等）、背景/场景、光线、媒介类型（单人、1girl 等）。每张图的标签顺序"
                "保持一致，一致性比详尽更重要。只输出标签。"
            ),
        },
        "natural": {
            "en": (
                "You are a captioning assistant for CHARACTER LoRA training datasets. "
                "Write ONE natural-language caption (15-35 words) with this fixed element order: "
                "trigger token first, then media type, shot type of a man/woman, clothing, "
                "pose/action, expression, background/setting, lighting. OMIT identity features "
                "fixed across the dataset (face, eye/hair color, skin, body type) so the trigger "
                "absorbs them. Keep the same element order in every caption; plain factual "
                "English, no poetic language and no quality words. Output only the caption."
            ),
            "zh": (
                "你是角色 LoRA 训练数据集的打标助手。请用一句 15-35 字的中文自然语言描述"
                "，按固定顺序组织：触发词在前，然后是媒介类型、人物镜头类型、服装细节、"
                "姿势/动作、表情、背景/场景、光线。省略数据集中固定的身份特征（脸、瞳色、"
                "发色、肤色、体型），让触发词吸收它们。每张图保持相同的元素顺序，简明客观"
                "，不用修饰性语言和质量词。只输出这句话。"
            ),
        },
    },
    "clothing": {
        "short": {
            "en": (
                "You are a captioning assistant for CLOTHING LoRA training datasets. "
                "Generate 5-12 comma-separated danbooru-style tags (lowercase, no underscores). "
                "The garment is the subject: ALWAYS include garment type, color, material/fabric, "
                "fit and cut (sleeve_length, collar, hem), visible details (buttons, zippers, "
                "ribbons, embroidery), folds/texture when visible, and how it is worn "
                "(zipped, tucked, ...). Keep the wearer generic - never describe the person's "
                "identity. Add view tags when recognizable (front_view, side_view, back_view, "
                "full_body, close-up, flat_lay). Order: garment, material/color, fit details, "
                "wearer context, view. Output only the tags."
            ),
            "zh": (
                "你是服装 LoRA 训练数据集的打标助手。请生成 5-12 个以中文逗号分隔的关键词标签"
                "。服装是主体：必须包含服装种类（连衣裙、卫衣、皮夹克等）、颜色、面料材质、"
                "版型剪裁（袖长、领型、下摆）、可见细节（纽扣、拉链、缎带、刺绣）、可见时的"
                "褶皱/肌理，以及穿着方式（拉上拉链、塞进裤腰等）。穿着者保持泛化，不要描述"
                "其身份。可辨认时补充视角词（正面、背面、全身、特写、平铺）。顺序：服装→材质"
                "颜色→版型细节→穿着者场景→视角。只输出标签。"
            ),
        },
        "natural": {
            "en": (
                "You are a captioning assistant for CLOTHING LoRA training datasets. "
                "Write ONE natural-language caption (10-25 words) describing the garment "
                "specifically: garment type, color, material/fabric, fit and cut (sleeves, "
                "collar, hem), visible details (buttons, zippers, folds), how it is worn, and "
                "the view (front view, close-up, full body). Keep the wearer generic - never "
                "describe the person's identity. Plain factual English. Output only the caption."
            ),
            "zh": (
                "你是服装 LoRA 训练数据集的打标助手。请用一句 10-25 字的中文自然语言描述"
                "，具体描述服装：种类、颜色、面料材质、版型剪裁（袖子、领口、下摆）、可见"
                "细节（纽扣、拉链、褶皱）、穿着方式与视角（正面、特写、全身）。穿着者保持"
                "泛化，不描述其身份。简明客观。只输出这句话。"
            ),
        },
    },
    "style": {
        "short": {
            "en": (
                "You are a captioning assistant for STYLE / art-style LoRA training datasets. "
                "Generate 5-12 comma-separated danbooru-style tags (lowercase, no underscores). "
                "Rule: caption the CONTENT, not the style - describe subjects, scene and "
                "composition so content never becomes bound to the style. Add at most 2-3 STABLE "
                "style cues (e.g. lineart, cel_shading, watercolor, rough_sketch, thick_outlines, "
                "grainy, muted_colors). Avoid generic quality tags (masterpiece, best_quality, 4k). "
                "Output only the tags."
            ),
            "zh": (
                "你是风格 LoRA 训练数据集的打标助手。请生成 5-12 个以中文逗号分隔的关键词标签"
                "。规则：打「内容」不打「风格」——描述画面中的主体、场景与构图，避免内容被"
                "绑定到风格上。风格词最多 2-3 个稳定词（线稿、赛璐璐上色、水彩、厚涂、粗描边"
                "、颗粒感、低饱和）。避免使用质量词（杰作、最优质量、4k）。只输出标签。"
            ),
        },
        "natural": {
            "en": (
                "You are a captioning assistant for STYLE LoRA training datasets. "
                "Write ONE natural-language caption describing the CONTENT of the image "
                "(subjects, scene, composition), not the art style. Keep style words to 2-3 at "
                "most. No quality terms. Plain factual English. Output only the caption."
            ),
            "zh": (
                "你是风格 LoRA 训练数据集的打标助手。请用一句中文自然语言描述画面的内容"
                "（主体、场景、构图），而不是描述艺术风格。风格词最多 2-3 个。不要使用质量词"
                "。简明客观。只输出这句话。"
            ),
        },
    },
}

# 提示词预设下拉：value -> 显示名（输出语言 × 格式 × 训练目标，取代原「打标模式」单选）
PROMPT_PRESETS = {
    "en_short_default": "英文 · 短标签 · 通用",
    "en_natural_default": "英文 · 自然语言 · 通用",
    "en_short_character": "英文 · 短标签 · 角色 LoRA",
    "en_natural_character": "英文 · 自然语言 · 角色 LoRA",
    "en_short_clothing": "英文 · 短标签 · 服装 LoRA",
    "en_natural_clothing": "英文 · 自然语言 · 服装 LoRA",
    "en_short_style": "英文 · 短标签 · 风格 LoRA",
    "en_natural_style": "英文 · 自然语言 · 风格 LoRA",
    "zh_short_default": "中文 · 短标签 · 通用",
    "zh_natural_default": "中文 · 自然语言 · 通用",
    "zh_short_character": "中文 · 短标签 · 角色 LoRA",
    "zh_natural_character": "中文 · 自然语言 · 角色 LoRA",
    "zh_short_clothing": "中文 · 短标签 · 服装 LoRA",
    "zh_natural_clothing": "中文 · 自然语言 · 服装 LoRA",
    "zh_short_style": "中文 · 短标签 · 风格 LoRA",
    "zh_natural_style": "中文 · 自然语言 · 风格 LoRA",
    "custom": "自定义",
}

DEFAULT_SETTINGS = {
    "api_key": "",
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "mode": "short",
    "system_prompt": DEFAULT_PROMPTS["short"],
    "tag_prefix": "",
    "prefix_mode": "prepend",
    "dark": False,
    "concurrency": 5,
    "last_project": "",
    "prompt_preset": "en_short_default",
    "preset_keys": {},  # 模型预设 -> API Key（明文，仅存于 gitignore 的 settings.json）
}

# 模型预设（名称 -> Base URL / 默认模型）。Claude 需中转站、DeepSeek-VL 需自建端点、Ollama 需 /v1。
MODEL_PRESETS = {
    "OpenCode (Zen/Go)": {"base_url": "https://opencode.ai/zen/go/v1", "model": "deepseek-v4-flash-vision-exp"},
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
    preview: str = ""  # 详情面板用的大图 data URL（懒加载，非缩略图）
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
    suppress_prompt_sync: bool = False  # 程序性更新提示词时抑制「视为自定义」


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
    # 兼容旧配置：旧值（mode/character/clothing/style/custom）映射到新的「格式 × 目标」键；
    # 兼容旧配置：
    # - v1 键：mode / character / clothing / style / custom
    # - v2 键：short_default / natural_* 等（无语言前缀，按英文处理）
    # - 缺省：按提示词内容推断（=某格式英文默认 => en_<fmt>_default；否则 => custom）
    _v1 = {"mode", "character", "clothing", "style", "custom"}
    if "prompt_preset" in data and data["prompt_preset"] in _v1:
        old = data["prompt_preset"]
        if old == "custom":
            merged["prompt_preset"] = "custom"
        elif old == "mode":
            merged["prompt_preset"] = "en_" + merged.get("mode", "short") + "_default"
        else:  # character / clothing / style（旧版均为英文短标签风格）
            merged["prompt_preset"] = "en_short_" + old
    elif "prompt_preset" in data and not data["prompt_preset"].startswith(("en_", "zh_")):
        merged["prompt_preset"] = "en_" + data["prompt_preset"]
    elif "prompt_preset" not in data:
        mode = merged.get("mode", "short")
        sp = (merged.get("system_prompt") or "").strip()
        merged["prompt_preset"] = (
            "en_" + mode + "_default"
            if sp == DEFAULT_PROMPTS[mode]["en"].strip() else "custom")
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
    """生成（并缓存到 datasets/.cache/<项目>/ ）缩略图，返回 data URL；失败返回空串。"""
    try:
        src = images_dir(project) / name
        cache_dir = DATASETS / ".cache" / project
        cache = cache_dir / f"{Path(name).stem}.thumb.jpg"
        if (cache.is_file() and cache.stat().st_size > 0
                and cache.stat().st_mtime >= src.stat().st_mtime):
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
    except Exception:
        return ""


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
def soft_color(hex_color: str, alpha: float = 0.14) -> str:
    """把 #rrggbb 转成低透明度 rgba，用于浅色徽章底色。"""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def set_badge(entry: ImageEntry, status: str) -> None:
    entry.status = status
    if entry.badge is not None:
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
                    .props("dense align-left").classes("w-full justify-start rounded-lg")
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


async def refresh_grid() -> None:
    grid = UI["grid"]
    grid.clear()
    if not state.current:
        with grid:
            ui.label("😶 请先在左侧选择或新建一个项目。").classes("tf-muted py-10")
        return

    imgs = project_images(state.current)
    entries = [
        ImageEntry(name=p.name, path=p, status=read_status(state.current, p.name))
        for p in imgs
    ]
    # 并行生成缩略图（线程池），单张失败不影响整批
    results = await asyncio.gather(
        *(run.io_bound(make_thumb, state.current, e.name) for e in entries),
        return_exceptions=True,
    )
    for e, r in zip(entries, results):
        e.thumb = r if isinstance(r, str) else ""
    state.entries = entries

    with grid:
        for i, entry in enumerate(entries):
            card = ui.card().props("flat") \
                .classes("tf-card") \
                .mark("image-card") \
                .on("click", lambda i=i: open_detail(i))
            with card:
                if entry.thumb:
                    ui.image(entry.thumb).classes("tf-card-img")
                else:
                    ui.label("⚠️ 无法预览").classes("tf-card-img tf-img-err tf-muted")
                entry.badge = ui.label(STATUS_TEXT[entry.status]).classes("tf-badge")
                entry.badge.style(f"background:{soft_color(STATUS_COLOR[entry.status])}; color:{STATUS_COLOR[entry.status]};")
                ui.label(entry.name).classes("tf-card-name")


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
                .classes("w-full max-h-96 object-contain rounded-xl")
            state.tagbox = ui.textarea(label="标签文本", value=read_label(state.current, entry.name)) \
                .classes("w-full").props("outlined dense")
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
    finally:
        set_generating_ui(False)


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
    if state.index == i and UI.get("preview_img") is not None:
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


def on_prompt_preset_change(e: events.ValueChangeEventArguments) -> None:
    """选择提示词预设：按「输出语言 × 格式 × 训练目标」填充对应提示词；「自定义」保留现有文本。

    预设键形如 <语言>_<格式>_<目标>，例如 en_short_character / zh_natural_style。
    """
    preset = e.value
    state.settings["prompt_preset"] = preset
    if preset == "custom":
        save_settings()
        return
    lang, fmt, target = preset.split("_", 2)
    if target == "default":
        text = DEFAULT_PROMPTS[fmt][lang]
    else:
        text = TRAINING_PROMPTS[target][fmt][lang]
    state.settings["mode"] = fmt
    set_prompt_text(text)
    state.settings["system_prompt"] = text
    save_settings()


def restore_default_prompt() -> None:
    """恢复为「英文 · 短标签 · 通用」并填入对应默认提示词。"""
    state.settings["prompt_preset"] = "en_short_default"
    UI["prompt_select"].value = "en_short_default"
    set_prompt_text(DEFAULT_PROMPTS["short"]["en"])
    state.settings["system_prompt"] = DEFAULT_PROMPTS["short"]["en"]
    state.settings["mode"] = "short"
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
.tf-card-name { font-size: .78rem; font-weight: 600; color: var(--tf-text); padding: .5rem .7rem .55rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tf-badge {
  position: absolute; top: 8px; right: 8px;
  font-size: .66rem; font-weight: 600; padding: 3px 9px;
  border-radius: 999px; letter-spacing: .02em;
  backdrop-filter: blur(2px);
  border: 1px solid rgba(255,255,255,.35);
}

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
        with ui.row().classes("ml-auto items-center gap-2"):
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

    # ---- 左侧面板：项目管理 + 模型配置 ----
    with ui.left_drawer(value=True, fixed=True).props("bordered").classes("tf-drawer w-80"):
        with ui.column().classes("w-full gap-3 p-4"):
            ui.label("项目管理").classes("tf-section")
            ui.button("新建项目", icon="add", on_click=new_project) \
                .props("unelevated rounded color=primary dense").classes("w-full")
            with ui.column().classes("w-full h-44 overflow-y-auto gap-1"):
                UI["project_list"] = ui.column().classes("w-full gap-1")
            ui.button("删除项目", icon="delete_outline", on_click=delete_project) \
                .props("flat dense rounded color=red-6").classes("w-full")

        ui.separator()
        ui.label("模型配置").classes("tf-section")
        with ui.column().classes("w-full gap-1"):
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
            ui.label("提示词预设（格式 × 训练目标）").classes("tf-label")
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
                    UI["uploader"] = ui.upload(multiple=True, auto_upload=True, on_upload=on_upload) \
                        .classes("hidden")
                    ui.button("上传图片", icon="upload",
                              on_click=lambda: UI["uploader"].run_method("pickFiles")) \
                        .props("unelevated rounded color=indigo-9")
                UI["batch_button"] = ui.button("开始批量标注", icon="auto_awesome", on_click=start_batch) \
                    .props("unelevated rounded color=primary").set_enabled(client_ready())
                UI["main_progress"] = ui.linear_progress(value=0.0, show_value=True) \
                    .classes("w-56").set_visibility(False)

        with ui.column().classes("w-full items-center pt-4 pb-10"):
            UI["grid"] = ui.grid(columns="repeat(auto-fill, minmax(190px, 1fr))") \
                .classes("tf-wide gap-4")

    # ---- 右侧详情面板 ----
    UI["drawer"] = ui.right_drawer(value=False, fixed=True).props("width=40% bordered") \
        .classes("tf-drawer")

    # ---- 弹窗 ----
    with ui.dialog() as UI["batch_modal"]:
        with ui.card().props("flat").classes("tf-card gap-3 p-5") \
                .style("width: 560px; max-width: 92vw;"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("批量标注").classes("tf-text text-lg font-bold")
                ui.button(icon="close", on_click=close_batch_modal).props("flat round color=grey-7")
            UI["batch_total"] = ui.label("").classes("tf-muted text-sm")
            UI["batch_progress"] = ui.linear_progress(value=0.0, show_value=True).classes("w-full")
            with ui.column().classes("w-full h-64 overflow-y-auto tf-log p-3"):
                UI["batch_log"] = ui.column().classes("w-full gap-1")
            with ui.row().classes("w-full items-center justify-between"):
                ui.button("后台运行", icon="play_arrow", on_click=run_in_background) \
                    .props("unelevated rounded color=primary")
                ui.button("终止", icon="stop", on_click=stop_batch).props("unelevated rounded color=red-6")

    with ui.dialog() as UI["help_dialog"]:
        pass

    with ui.dialog() as UI["project_dialog"]:
        pass

    with ui.dialog() as UI["confirm_dialog"]:
        pass

    with ui.dialog() as UI["confirm_delete_project"]:
        pass

    # ---- 右下角 Token 统计 ----
    with ui.column().classes("fixed bottom-3 right-3"):
        UI["tokens"] = ui.label("Tokens：—").classes("tf-token")

    # 初始加载：优先恢复上次项目，缺失时回退到第一个项目
    refresh_project_list()
    remembered = state.settings.get("last_project") or ""
    state.current = remembered if remembered in state.projects else (state.projects[0] if state.projects else None)
    if state.current:
        UI["toolbar_title"].set_text(state.current)
        background_tasks.create(refresh_grid())


@ui.page('/')
def index_page() -> None:
    build_ui()

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(host="0.0.0.0", port=8080, reload=False)
