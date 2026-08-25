"""UI 冒烟测试：验证网格渲染、点击卡片打开详情面板（回归测试 drawer.show/hide 修复）。

运行：.venv/bin/python -m pytest tests -q
"""
import shutil
from pathlib import Path

import pytest
from nicegui.testing import User

import main  # noqa: F401  导入以注册 @ui.page('/')

DATASETS = main.DATASETS
PROJ = "_smoketest"


@pytest.fixture(autouse=True)
def temp_project():
    img_dir = DATASETS / PROJ / "images"
    lab_dir = DATASETS / PROJ / "labels"
    img_dir.mkdir(parents=True, exist_ok=True)
    lab_dir.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.new("RGB", (256, 256), (200, 30, 30)).save(img_dir / "demo.png")
    # 隔离全局状态
    main.state.current = None
    main.state.entries = []
    yield
    shutil.rmtree(DATASETS / PROJ, ignore_errors=True)


@pytest.mark.anyio
async def test_card_opens_detail_drawer(user: User):
    await user.open("/")
    # 项目列表应包含 _smoketest（按字典序它会被自动选中为当前项目）
    await user.should_see(PROJ)
    # 网格出现图片卡片
    await user.should_see("demo.png")
    # 点击卡片 -> 详情面板出现「保存/重新生成/删除图片」（此前 drawer.open 会抛 AttributeError）
    # 注：模拟器不冒泡，故用 mark 定位卡片自身再触发其 click 监听
    user.find(marker="image-card").click()
    await user.should_see("保存")
    await user.should_see("重新生成")
    await user.should_see("删除图片")
