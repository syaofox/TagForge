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
