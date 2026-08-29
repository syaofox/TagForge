import copy
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as appmod
import core

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "config" / "settings.json"
TEST_PROJ = "pytest_tmp"


@pytest.fixture(scope="session", autouse=True)
def _protect_settings():
    """保护用户的 config/settings.json：测试前后原样还原。"""
    orig = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
    yield
    if orig is not None:
        SETTINGS_FILE.write_bytes(orig)


@pytest.fixture(autouse=True)
def protect_presets():
    """每用例前后还原预设状态：presets.json / settings.json 磁盘文件与内存态
    （core._presets 缓存、core.state.settings），避免预设 CRUD 测试相互污染。"""
    orig_presets_file = core.PRESETS_FILE.read_text(encoding="utf-8") if core.PRESETS_FILE.exists() else None
    orig_settings_file = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
    core.load_presets()  # 归一化内存缓存与磁盘一致后再快照，避免 fixture 与 lifespan 顺序导致快照漂移
    orig_presets = copy.deepcopy(core._presets)
    orig_settings = copy.deepcopy(core.state.settings)
    yield
    core._presets = orig_presets
    core._presets_loaded = False  # 下次访问强制重读磁盘（已被还原）
    core.state.settings = orig_settings
    if orig_presets_file is not None:
        core.PRESETS_FILE.write_text(orig_presets_file, encoding="utf-8")
    else:
        core.PRESETS_FILE.unlink(missing_ok=True)
    if orig_settings_file is not None:
        SETTINGS_FILE.write_bytes(orig_settings_file)
    else:
        SETTINGS_FILE.unlink(missing_ok=True)


@pytest.fixture
def client():
    with TestClient(appmod.app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_tmp_project():
    """每个测试前清理残留的测试项目。"""
    shutil.rmtree(core.DATASETS / TEST_PROJ, ignore_errors=True)
    shutil.rmtree(core.DATASETS / ".cache" / TEST_PROJ, ignore_errors=True)
    core.state.current = None
    core.state.entries = []
    yield
    shutil.rmtree(core.DATASETS / TEST_PROJ, ignore_errors=True)
    shutil.rmtree(core.DATASETS / ".cache" / TEST_PROJ, ignore_errors=True)
