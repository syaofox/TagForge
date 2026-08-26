pytest_plugins = ["nicegui.testing.user_plugin"]


import pytest


@pytest.fixture(scope="session")
def anyio_backend():
    """只跑 asyncio 后端，避免 trio 环境报错。"""
    return "asyncio"
