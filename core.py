"""core.py —— TagForge 纯业务逻辑层（0 框架依赖，可被 NiceGUI / FastAPI 共用）。

从原 main.py 抽出的所有非 UI 逻辑：
  - 常量与路径、提示词预设数据
  - 配置读写、文件扫描与状态判定
  - 图片处理（缩略图缓存 / API 预处理）
  - 数据结构与全局状态（AppState 单例）
  - 批量标注协程（进度以回调驱动，UI 无关）
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
from typing import Any, Optional

from PIL import Image

from llm_client import FatalAPIError, LLMClient

# ---------------- 常量与路径 ----------------
ROOT = Path(__file__).resolve().parent
DATASETS = ROOT / "datasets"
CONFIG = ROOT / "config"
SETTINGS_FILE = CONFIG / "settings.json"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
PAGE_SIZE = 60  # 网格每页加载张数（P0-3 分页）

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
            "Example (format only): 'A long-haired woman in a light dress stands under a "
            "cherry blossom tree, smiling at the camera, soft afternoon light.' "
            "Output only the sentence."
        ),
        "zh": (
            "你是一名图像打标助手。请用一句完整、带标点的中文自然语言句子描述这张图片"
            "（客观、具体，包含主体、动作、环境与光线），禁止输出没有标点的关键词罗列。"
            "参考成品示例（仅示意句式）：「一位长发少女站在樱花树下，身穿浅色连衣裙，"
            "微笑望向镜头，背景虚化，阳光透过花瓣洒落。」只输出这句话。"
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
                "English, no poetic language and no quality words. "
                "Example (format only): '<name>, a medium shot of a man in a dark police "
                "uniform with a blue shirt, standing in a kitchen with hands on hips and a "
                "stern expression, warm interior lighting.' Output only the caption."
            ),
            "zh": (
                "你是角色 LoRA 训练数据集的打标助手。用中文输出一句通顺、完整的自然语言"
                "描述句，要求：必须是一句带标点的全句话（逗号、句号齐备），按中文语法连接"
                "，禁止输出关键词列表或没有标点的标签串；句子以角色名（触发词）开头，随后"
                "按固定顺序组织：媒介/镜头、服装细节、姿势动作、表情、背景场景、光线；省略"
                "数据集中固定的身份特征（脸型、瞳色、发色、肤色、体型），让触发词吸收；"
                "每张图保持相同句式与元素顺序，简明客观，20-45 字，不用修饰性语言和质量词。"
                "句式参考（按此骨架组织，填入每张图的实际内容）："
                "「<角色名>，一张<镜头>，身穿<服装细节>，<姿势动作>，<表情>，站在<背景场景>"
                "，<光线>。」参考成品示例（仅示意句式与标点）："
                "「角色名，一张半身照，身穿蓝白女仆装，双手叉腰，微笑，站在白色影棚背景前，"
                "柔光照明。」只输出这一句话，不要解释。"
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
                "describe the person's identity. Plain factual English. "
                "Example (format only): 'She wears a long beige trench coat with a tie waist, "
                "finely textured fabric and a crisp collar, front view, blurred background.' "
                "Output only the caption."
            ),
            "zh": (
                "你是服装 LoRA 训练数据集的打标助手。用中文输出一句通顺、带标点的自然语言"
                "句子，禁止输出没有标点的关键词串。具体描述服装：种类、颜色、面料材质、"
                "版型剪裁（袖子、领口、下摆）、可见细节（纽扣、拉链、褶皱）、穿着方式与"
                "视角（正面、特写、全身）。穿着者保持泛化，不描述其身份。简明客观，"
                "20-40 字。参考成品示例（仅示意句式与标点）："
                "「她身穿一件米色长款风衣，系带收腰，面料细腻有肌理，领口袖口细节清晰，"
                "正面视角，背景虚化。」只输出这一句话。"
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
                "most. No quality terms. Plain factual English. "
                "Example (format only): 'A thick-painted illustration of a girl at a street "
                "corner, centered composition, warm tones, soft lighting.' "
                "Output only the caption."
            ),
            "zh": (
                "你是风格 LoRA 训练数据集的打标助手。用中文输出一句通顺、带标点的自然语言"
                "句子，禁止输出没有标点的关键词串。描述画面的内容（主体、场景、构图），"
                "而不是描述艺术风格；风格词最多 2-3 个；不要使用质量词。简明客观，"
                "20-40 字。参考成品示例（仅示意句式与标点）："
                "「画面以厚涂插画风格呈现，主体是少女立于街角，居中构图，暖色调，光线柔和。」"
                "只输出这一句话。"
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
    "system_prompt": DEFAULT_PROMPTS["short"]["en"],
    "tag_prefix": "",
    "prefix_mode": "prepend",
    "character_name": "",  # 角色 LoRA 的角色名（注入「角色 LoRA」预设提示词）
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
    thumb: str = ""  # 缩略图 data URL（旧 UI 用；新 UI 走 /api/image/thumb 静态直连）
    preview: str = ""  # 详情面板用的大图 data URL（旧 UI 懒加载用）
    label: str = ""  # 标签文本（卡片缩略图下方展示）
    # --- 旧 UI（NiceGUI）元素引用：迁移完成前保留；新 UI（FastAPI）不使用 ---
    badge: Optional[Any] = None
    caption: Optional[Any] = None
    card: Optional[Any] = None
    check: Optional[Any] = None


@dataclass
class AppState:
    settings: dict = field(default_factory=dict)
    projects: list = field(default_factory=list)
    current: Optional[str] = None
    entries: list = field(default_factory=list)
    client: Optional[LLMClient] = None
    batch: Optional[Any] = None
    abort_batch: bool = False
    index: int = 0
    tagbox: Optional[Any] = None  # 旧 UI 用
    suppress_prompt_sync: bool = False  # 程序性更新提示词时抑制「视为自定义」
    view_filter: str = "all"  # 状态筛选：all/tagged/pending/failed
    view_query: str = ""  # 搜索词（文件名或标签）
    view_page: int = 1  # 已加载的分页数（每页 PAGE_SIZE）
    search_seq: int = 0  # 搜索防抖序号（旧 UI 用）
    upload_ok: int = 0  # 本轮上传成功张数
    upload_fail: int = 0  # 本轮上传失败张数
    upload_renamed: int = 0  # 本轮同名自动改名张数
    batch_log_lines: list = field(default_factory=list)  # 批量日志（供复制）
    batch_log_visible: bool = True  # 批量日志区展开/收起


state = AppState()


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
    # 兼容旧配置（mode 字段已移除）：
    # - v1 键：mode / character / clothing / style / custom
    # - v2 键：short_default / natural_* 等（无语言前缀，按英文处理）
    # - 缺省：按提示词内容推断（=某格式英文默认 => en_<fmt>_default；否则 => custom）
    _v1 = {"mode", "character", "clothing", "style", "custom"}
    if "prompt_preset" in data and data["prompt_preset"] in _v1:
        old = data["prompt_preset"]
        if old == "custom":
            merged["prompt_preset"] = "custom"
        elif old == "mode":
            # 旧「随打标模式」：按提示词内容推断格式
            sp = (merged.get("system_prompt") or "").strip()
            merged["prompt_preset"] = (
                "en_natural_default" if sp == DEFAULT_PROMPTS["natural"]["en"].strip()
                else "en_short_default")
        else:  # character / clothing / style（旧版均为英文短标签风格）
            merged["prompt_preset"] = "en_short_" + old
    elif "prompt_preset" in data and not data["prompt_preset"].startswith(("en_", "zh_")):
        merged["prompt_preset"] = "en_" + data["prompt_preset"]
    elif "prompt_preset" not in data:
        sp = (merged.get("system_prompt") or "").strip()
        if sp == DEFAULT_PROMPTS["natural"]["en"].strip():
            merged["prompt_preset"] = "en_natural_default"
        elif sp == DEFAULT_PROMPTS["short"]["en"].strip():
            merged["prompt_preset"] = "en_short_default"
        else:
            merged["prompt_preset"] = "custom"
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


def apply_prefix(text: str, prefix: str, prefix_mode: str) -> str:
    """按 prefix_mode 对生成文本应用触发词前缀，并避免重复叠加。"""
    prefix = prefix.strip()
    if not prefix:
        return text
    pre = prefix.rstrip(",").strip()
    if prefix_mode == "per_tag":
        tags = [t.strip() for t in text.split(",") if t.strip()]
        return ", ".join(t if t.startswith(pre) else f"{pre}, {t}" for t in tags)
    if not text:
        return pre
    return text if text.startswith(pre) else f"{pre}, {text}"


# ---------------- 图片处理（缩略图缓存 / API 预处理） ----------------
def thumb_cache_path(project: str, name: str) -> Path:
    return DATASETS / ".cache" / project / f"{Path(name).stem}.thumb.jpg"


def ensure_thumb_file(project: str, name: str, max_side: int = 360) -> Optional[Path]:
    """确保缩略图缓存存在（按 mtime/尺寸校验）并返回其路径；失败返回 None。"""
    try:
        src = images_dir(project) / name
        cache = thumb_cache_path(project, name)
        if (cache.is_file() and cache.stat().st_size > 0
                and cache.stat().st_mtime >= src.stat().st_mtime):
            return cache
        with Image.open(src) as im:
            im.thumbnail((max_side, max_side))
            if im.mode != "RGB":
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
            data = buf.getvalue()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
        return cache
    except Exception:
        return None


def make_thumb(project: str, name: str, max_side: int = 360) -> str:
    """生成（并缓存到 datasets/.cache/<项目>/ ）缩略图，返回 data URL；失败返回空串。"""
    p = ensure_thumb_file(project, name, max_side)
    if p is None:
        return ""
    try:
        return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode("ascii")
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


async def encode_for_api_async(path: Path) -> str:
    """预处理图片并转 data URL（线程池执行）。"""
    data = await asyncio.to_thread(encode_for_api, path)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


# ---------------- 模型客户端 ----------------
def client_ready() -> bool:
    s = state.settings
    base = (s.get("base_url") or "").strip()
    key = (s.get("api_key") or "").strip()
    return bool(base) and (bool(key) or "ollama" in base.lower())


def soft_color(hex_color: str, alpha: float = 0.14) -> str:
    """把 #rrggbb 转成低透明度 rgba，用于浅色徽章底色。"""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def build_client(settings: dict | None = None) -> LLMClient:
    """按配置构造客户端（batch / 单张生成 / 试生成共用）。"""
    s = settings or state.settings
    return LLMClient(api_key=s.get("api_key"), base_url=s.get("base_url"),
                     model_name=s.get("model"))


# ---------------- 统计 / 筛选（给定 state，纯函数） ----------------
def compute_stats() -> dict:
    """统计各状态图片数。"""
    counts = {"all": 0, "tagged": 0, "pending": 0, "failed": 0, "processing": 0}
    for e in state.entries:
        counts[e.status] = counts.get(e.status, 0) + 1
        counts["all"] += 1
    return counts


def filtered_entries() -> list:
    """按状态筛选 + 文本搜索（文件名或标签）后的条目列表。"""
    items = state.entries
    f = state.view_filter
    if f != "all":
        items = [e for e in items if e.status == f]
    q = state.view_query.lower()
    if q:
        items = [e for e in items if q in e.name.lower() or q in e.label.lower()]
    return items


def scan_entries(project: str) -> list:
    """扫描项目图片，返回 ImageEntry 列表（不预生成缩略图 data URL）。"""
    return [ImageEntry(name=p.name, path=p, status=read_status(project, p.name),
                       label=read_label(project, p.name))
            for p in project_images(project)]


# ---------------- 提示词（预设 + 角色名注入） ----------------
# 角色 LoRA 预设键（注入角色名用）
CHARACTER_PRESETS = {
    "en_short_character", "en_natural_character",
    "zh_short_character", "zh_natural_character",
}


def resolve_prompt_text(preset: str | None = None) -> str:
    """生成实际提示词文本 = 预设模板 + 角色名注入（仅「角色 LoRA」预设生效）。"""
    preset = preset or state.settings.get("prompt_preset", "en_short_default")
    if preset == "custom":
        return state.settings.get("system_prompt", "")
    lang, fmt, target = preset.split("_", 2)
    text = (DEFAULT_PROMPTS[fmt][lang] if target == "default"
            else TRAINING_PROMPTS[target][fmt][lang])
    name = (state.settings.get("character_name") or "").strip()
    if target == "character" and name:
        if lang == "en":
            text += (f" The character is named '{name}'; always refer to the character as "
                     f"'{name}' and start every output with it.")
        else:
            text += f" 角色的名称为「{name}」，句首必须以「{name}」称呼角色，整句只用这一个名字。"
    return text


# ---------------- 项目 CRUD / 导出 / 上传 ----------------
def sanitize_project_name(name: str) -> str:
    return "".join(ch for ch in (name or "").strip() if ch.isalnum() or ch in "_- ").strip()


def create_project(name: str) -> Optional[str]:
    """创建项目目录（images/labels）。返回错误信息或 None。"""
    name = sanitize_project_name(name)
    if not name:
        return "项目名不能为空"
    proj_dir = DATASETS / name
    (proj_dir / "images").mkdir(parents=True, exist_ok=True)
    (proj_dir / "labels").mkdir(parents=True, exist_ok=True)
    return None


def delete_project(name: str) -> None:
    shutil.rmtree(DATASETS / name, ignore_errors=True)
    shutil.rmtree(DATASETS / ".cache" / name, ignore_errors=True)


def export_zip(project: str) -> Path:
    """打包项目图片 + 标签为 ZIP，返回 zip 路径。"""
    zip_dir = ROOT / "exports"
    zip_dir.mkdir(parents=True, exist_ok=True)
    zip_path = zip_dir / f"{project}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in project_images(project):
            z.write(p, arcname=f"images/{p.name}")
        labels = DATASETS / project / "labels"
        if labels.is_dir():
            for lp in sorted(labels.iterdir()):
                if lp.suffix.lower() == ".txt":
                    z.write(lp, arcname=f"labels/{lp.name}")
    return zip_path


def resolve_upload_destination(project: str, name: str) -> tuple[Optional[Path], bool]:
    """计算上传目标路径（扩展名白名单 + 同名自动改名）。

    :return: (目标路径, 是否被改名)；扩展名不受支持时返回 (None, False)。
    """
    name = name or ""
    if Path(name).suffix.lower() not in IMAGE_EXTS:
        return None, False
    dst = images_dir(project) / name
    if not dst.exists():
        return dst, False
    stem, ext = name.rsplit(".", 1)
    i = 1
    while True:
        alt = f"{stem} ({i}).{ext}"
        if not (images_dir(project) / alt).exists():
            return images_dir(project) / alt, True
        i += 1


# ---------------- 批量标注（UI 无关，进度回调驱动） ----------------
async def run_batch(targets: list, project: str, settings: dict,
                    client: LLMClient, progress_cb=None) -> dict:
    """并发批量标注（与原 main.run_batch 行为一致，UI 更新改为回调）。

    progress_cb(event) 事件类型：
      {"type": "mark", "name", "status"}               entry 状态变化
      {"type": "log", "text"}                          批量日志行
      {"type": "progress", "done", "total", "ok", "fail", "elapsed"}
      {"type": "done", "ok", "fail", "aborted", "reason", "done", "total"}
    回调必须非阻塞（进度事件可能高频触发）。
    """
    sem = asyncio.Semaphore(int(settings.get("concurrency") or 5))
    total = len(targets)
    done = 0
    ok_count = 0
    fail_count = 0
    batch_start = time.perf_counter()
    prop = settings.get("system_prompt", "")
    prefix = settings.get("tag_prefix", "")
    prefix_mode = settings.get("prefix_mode", "prepend")

    def _emit(ev: dict) -> None:
        if progress_cb is not None:
            try:
                progress_cb(ev)
            except Exception:
                pass  # 回调异常不得影响批次

    async def process(entry: ImageEntry) -> None:
        nonlocal done, ok_count, fail_count
        if state.abort_batch:
            return
        async with sem:
            if state.abort_batch:
                return
            entry.status = "processing"
            _emit({"type": "mark", "name": entry.name, "status": "processing"})
            t0 = time.perf_counter()
            try:
                data = await encode_for_api_async(entry.path)
                tags = await client.generate(data, prop)
                final = apply_prefix(tags, prefix, prefix_mode)
                write_label(project, entry.name, final)
                entry.status = "tagged"
                _emit({"type": "mark", "name": entry.name, "status": "tagged"})
                ok_count += 1
                _emit({"type": "log", "text": f"{entry.name} ✅ 成功 ({time.perf_counter() - t0:.1f}s)"})
            except FatalAPIError as e:
                entry.status = "failed"
                fail_count += 1
                _emit({"type": "log", "text": f"{entry.name} ⚠️ {e}"})
                state.abort_batch = True  # 致命错误：中止整批
            except Exception as e:
                entry.status = "failed"
                fail_count += 1
                _emit({"type": "log", "text": f"{entry.name} ❌ 失败：{e}"})
            finally:
                done += 1
                _emit({"type": "progress", "done": done, "total": total, "ok": ok_count,
                       "fail": fail_count, "elapsed": time.perf_counter() - batch_start})

    try:
        results = await asyncio.gather(*(process(t) for t in targets), return_exceptions=True)
    except asyncio.CancelledError:
        for t in targets:
            if t.status == "processing":
                t.status = "pending"
        raise

    if state.abort_batch:
        for t in targets:
            if t.status == "processing":
                t.status = "pending"
        _emit({"type": "log", "text": f"⏹ 已终止（本次完成 {done}/{total}）"})
    else:
        _emit({"type": "log", "text": f"🎉 完成：成功 {ok_count} / {total}（失败 {fail_count}）"})

    result = {
        "ok": ok_count, "fail": fail_count, "done": done, "total": total,
        "aborted": bool(state.abort_batch),
        "reason": "aborted" if state.abort_batch else "ok",
    }
    _emit({"type": "done", **result})
    return result
