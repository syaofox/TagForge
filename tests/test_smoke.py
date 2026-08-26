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
    # 隔离全局状态（不依赖磁盘配置的残留 last_project）
    original_last = main.state.settings.get("last_project") or ""
    main.state.current = None
    main.state.entries = []
    main.state.settings["last_project"] = "p1vis_never"  # 占位：测试中 auto-select 不会命中
    yield
    # 恢复原 last_project，避免测试点击项目时 save_settings() 污染磁盘配置
    main.state.settings["last_project"] = original_last
    main.save_settings()
    shutil.rmtree(DATASETS / PROJ, ignore_errors=True)


@pytest.mark.anyio
async def test_card_opens_detail_drawer(user: User):
    await user.open("/")
    # 项目列表应包含 _smoketest（按字典序它会被自动选中为当前项目）
    await user.should_see(PROJ)
    user.find(PROJ).click()  # 显式选择测试项目（不依赖磁盘 last_project；click 为本版同步方法）
    # 网格出现图片卡片（切换项目为异步刷新，放宽重试）
    await user.should_see("demo.png", retries=30)
    user.find(marker="image-card").click()
    await user.should_see("保存")
    await user.should_see("重新生成")
    await user.should_see("删除图片")