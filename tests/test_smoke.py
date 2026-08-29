"""应用级冒烟测试：FastAPI + HTMX（替代原 NiceGUI UI 冒烟）。

覆盖：首页渲染、项目创建/选择/删除、上传、网格（缩略图/筛选）、标签保存、
图片删除、批量前置校验、导出、模型预设管理、模型列表拉取。
"""
import asyncio
import io
import zipfile
from pathlib import Path
from urllib.parse import quote

from PIL import Image

import core

TF_CSS = Path(__file__).resolve().parent.parent / "static" / "css" / "tf.css"


def _png_bytes(size: int = 32, color=(120, 30, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


def test_css_hidden_guard():
    """回归：作者 display 规则不得覆盖 hidden 属性（否则 lightbox/批量卡等常显导致黑屏）。"""
    css = TF_CSS.read_text(encoding="utf-8")
    assert "[hidden] { display: none !important; }" in css


def test_home_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "TagForge" in r.text
    assert "新建项目" in r.text
    assert "开始批量标注" in r.text


def test_project_crud_and_grid_flow(client):
    # 创建项目（自动选中）
    r = client.post("/api/projects", data={"name": "pytest_tmp"})
    assert r.status_code == 200
    assert "pytest_tmp" in r.text
    # 上传两张图
    r = client.post("/api/upload", files=[
        ("files", ("a.png", _png_bytes(), "image/png")),
        ("files", ("a.png", _png_bytes(16), "image/png")),  # 同名 -> 自动改名
        ("files", ("bad.gif", b"GIF89a", "image/gif")),     # 不支持扩展名
    ])
    assert r.status_code == 200
    j = r.json()
    assert j == {"ok": 2, "fail": 1, "renamed": 1}, j
    # 网格出现两张（一原一同名改名）
    r = client.get("/partials/grid_cards?filter=all&q=")
    assert "a.png" in r.text and "a (1).png" in r.text
    # 回归：缩略图 URL 必须带项目名（防止 project 变量缺失导致 // 404）
    assert 'src="/api/image/thumb/pytest_tmp/a.png"' in r.text
    # 缩略图可用
    r = client.get("/api/image/thumb/pytest_tmp/a.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    # 保存标签 -> 状态变为已标注
    r = client.post("/api/label/pytest_tmp/a.png", data={"text": "girl, red dress"})
    assert r.status_code == 204
    assert "girl, red dress" == core.read_label("pytest_tmp", "a.png")
    r = client.get("/partials/grid_cards?filter=tagged&q=")
    assert 'data-name="a.png"' in r.text
    r = client.get("/partials/grid_cards?filter=pending&q=")
    assert 'data-name="a.png"' not in r.text
    # 详情面板
    r = client.get("/partials/detail?project=pytest_tmp&name=a.png")
    assert r.status_code == 200 and "重新生成" in r.text
    # 删除图片（触发 gridChanged + detailClosed）
    r = client.delete("/api/image/pytest_tmp/a (1).png")
    assert r.status_code == 204
    assert "a (1).png" not in [e.name for e in core.state.entries]
    # 导出 ZIP
    r = client.get("/api/export/pytest_tmp")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = set(z.namelist())
    assert "images/a.png" in names and "labels/a.txt" in names
    # 删除项目
    r = client.delete("/api/projects/pytest_tmp")
    assert r.status_code == 200


def test_batch_guard_blocks_when_no_targets(client):
    client.post("/api/projects", data={"name": "pytest_tmp"})
    r = client.post("/api/batch/start")
    assert r.status_code == 400  # 无待标注图片


def test_settings_roundtrip(client):
    r = client.post("/api/settings", json={"concurrency": 7})
    assert r.status_code == 204
    assert core.state.settings["concurrency"] == 7


def test_upload_sanitizes_path_traversal():
    """回归：上传文件名含路径组件（/、\\、..）须剥离，防写出 images/ 目录。"""
    dst, ren = core.resolve_upload_destination("pytest_tmp", "../../evil.png")
    assert ren is False
    assert dst == core.images_dir("pytest_tmp") / "evil.png"
    assert not (core.DATASETS / "evil.png").exists()
    dst2, _ = core.resolve_upload_destination("pytest_tmp", "sub/dir/x.png")
    assert dst2 == core.images_dir("pytest_tmp") / "x.png"
    dst3, _ = core.resolve_upload_destination("pytest_tmp", "..\\y.png")
    assert dst3 == core.images_dir("pytest_tmp") / "y.png"


def test_preset_crud(client):
    """自定义预设：新增 / 查询 / preset_info 回填 / 删除（含持久化文件）。"""
    r = client.post("/api/settings/presets", json={
        "name": "My Provider", "base_url": "https://myx/v1/", "model": "vision-x"})
    assert r.status_code == 200
    eff = client.get("/api/settings/presets").json()["presets"]
    assert eff["My Provider"] == {"base_url": "https://myx/v1", "model": "vision-x"}
    assert core.PRESETS_FILE.is_file()
    # preset_info 对自定义预设生效（含已记忆的 Key）
    client.post("/api/settings", json={"preset_keys": {"My Provider": "sk-mine"}})
    j = client.get("/api/settings/preset?name=" + quote("My Provider")).json()
    assert j["base_url"] == "https://myx/v1" and j["model"] == "vision-x" and j["api_key"] == "sk-mine"
    # 空名校验
    r = client.post("/api/settings/presets", json={"name": "", "base_url": "", "model": ""})
    assert r.status_code == 400
    # 删除自定义预设 -> 一并清理 preset_keys
    r = client.delete("/api/settings/presets?name=" + quote("My Provider"))
    assert r.status_code == 200
    assert "My Provider" not in client.get("/api/settings/presets").json()["presets"]
    assert "My Provider" not in core.state.settings["preset_keys"]


def test_preset_overrides_and_deletes_builtin(client):
    """统一处理：内置预设可覆盖（同名写入自定义）；纯内置可删除（移入 removed 标记）。"""
    name = "OpenCode (Zen/Go)"
    r = client.post("/api/settings/presets", json={
        "name": name, "base_url": "http://127.0.0.1:11434/v1", "model": "my-model"})
    assert r.status_code == 200
    assert client.get("/api/settings/presets").json()["presets"][name]["model"] == "my-model"
    # 删除被覆盖的内置 -> 仅移除覆盖，回到默认
    client.delete("/api/settings/presets?name=" + quote(name))
    assert client.get("/api/settings/presets").json()["presets"][name]["model"] == "deepseek-v4-flash-vision-exp"
    # 删除纯内置 -> 从生效列表消失，且不误删其它
    client.delete("/api/settings/presets?name=" + quote(name))
    eff = client.get("/api/settings/presets").json()["presets"]
    assert name not in eff and "DeepSeek (官方)" in eff


def test_preset_delete_with_slash_in_name(client):
    """回归：预设名含 `/`（%2F 拆段）时，名称须走 query 而非路径段。"""
    name = "My/中转（自建）"
    r = client.post("/api/settings/presets", json={
        "name": name, "base_url": "http://x/v1", "model": "m"})
    assert r.status_code == 200
    j = client.get("/api/settings/preset?name=" + quote(name, safe="")).json()
    assert j["name"] == name and j["base_url"] == "http://x/v1"
    r = client.delete("/api/settings/presets?name=" + quote(name, safe=""))
    assert r.status_code == 200
    assert name not in client.get("/api/settings/presets").json()["presets"]


def test_preset_rename_migrates_key(client):
    """改名：自定义预设被移除，preset_keys 迁移到新名。"""
    client.post("/api/settings/presets", json={
        "name": "Old", "base_url": "https://x/v1", "model": "m1"})
    client.post("/api/settings", json={"preset_keys": {"Old": "sk-old"}})
    r = client.post("/api/settings/presets", json={
        "orig_name": "Old", "name": "New", "base_url": "https://x/v1", "model": "m2"})
    assert r.status_code == 200
    eff = client.get("/api/settings/presets").json()["presets"]
    assert "Old" not in eff and eff["New"]["model"] == "m2"
    keys = core.state.settings["preset_keys"]
    assert "Old" not in keys and keys["New"] == "sk-old"


def test_models_endpoint(client, monkeypatch):
    """POST /api/models：按 base_url/api_key 返回提供商模型列表；缺 Base URL 返回 400。"""
    class FakeClient:
        def __init__(self, api_key="", base_url="", model_name=""):
            self.api_key, self.base_url, self.model_name = api_key, base_url, model_name
        async def list_models(self):
            return ["m1", "m2"]

    r = client.post("/api/models", json={"base_url": "", "api_key": ""})
    assert r.status_code == 400

    monkeypatch.setattr("app.LLMClient", FakeClient)
    r = client.post("/api/models", json={"base_url": "https://x/v1", "api_key": "sk-x"})
    assert r.status_code == 200
    assert r.json() == {"models": ["m1", "m2"]}


def test_llm_client_list_models():
    """llm_client.list_models：去重排序；网关 404（不支持列表）返回空。"""
    from httpx import Request, Response
    from openai import APIStatusError

    from llm_client import LLMClient

    client = LLMClient(api_key="sk-x", base_url="http://localhost/v1", model_name="m")

    class _Model:
        def __init__(self, id): self.id = id
    class _Resp:
        data = [_Model("b"), _Model("a"), _Model("a")]

    async def fake_ok():
        return _Resp
    client.client.models.list = fake_ok
    assert asyncio.run(client.list_models()) == ["a", "b"]

    def _notfound():
        raise APIStatusError("Not Found", response=Response(404, request=Request("GET", "http://x/models")), body=None)
    client.client.models.list = _notfound
    assert asyncio.run(client.list_models()) == []