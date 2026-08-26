"""应用级冒烟测试：FastAPI + HTMX（替代原 NiceGUI UI 冒烟）。

覆盖：首页渲染、项目创建/选择/删除、上传、网格（缩略图/筛选）、标签保存、
图片删除、批量前置校验、导出。
"""
import io
import zipfile
from pathlib import Path

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