# TagForge

LoRA 数据集图片打标工具：Web 端批量标注，单机 / 局域网单人使用。
技术栈：**Python + FastAPI + Jinja2 + HTMX**，零数据库（文件系统存储），无前端构建步骤。
历史：早期为 NiceGUI，2025-08 已迁移至 FastAPI/HTMX，**不得再引回 NiceGUI**。

## 架构

职责严格分层，业务逻辑全部在 `core.py`：

- `app.py` — FastAPI 路由薄层：参数解析 → 调 core → 渲染 partial/JSON/文件，**不含业务逻辑**。
- `core.py` — 纯业务逻辑（0 框架依赖，可单测）。`core.state` 为模块级单例（单人使用，无会话态），承载配置、当前项目、entries、批量任务等。
- `llm_client.py` — OpenAI 兼容大模型异步封装：指数退避重试（最多 3 次）、Token 累计、`FatalAPIError`（401/403/重试耗尽 → 中止整批）。
- `templates/` `static/` — Jinja2 服务端渲染 + 局部片段 + HTMX；`static/vendor/htmx.min.js` 单文件 vendor，无构建。

## 数据约定（零数据库）

- 图片：`datasets/<项目>/images/`；标签：`datasets/<项目>/labels/<同名>.txt`，`.txt` 非空即「已标注」，否则「待标注」。
- 缩略图缓存：`datasets/.cache/<项目>/`（按 mtime/尺寸校验），失败图片状态重启后回到「待标注」。
- 全局配置：`config/settings.json`（含 API Key，已 gitignore）；导出：`exports/<项目>.zip`。

## 关键约定

- **路由一律薄**：不在 `app.py` 写业务逻辑；UI 相关逻辑进 core 的纯函数。
- **异步**：所有大模型调用必须 `async/await`；并发用 `asyncio.Semaphore`（默认 5），禁止阻塞事件循环；缩略图/编码/文件 IO 用 `asyncio.to_thread`。
- **交互**：局部刷新走 HTMX，事件用 `HX-Trigger` 广播（`gridChanged` / `detailReload` / `detailClosed` / `batchStarted` / `tokensUpdated`，契约见 `doc/设计.md` §五）；仅批量进度走 SSE（`/api/batch/events`）。
- **批量任务**：存 `core.state.batch`（asyncio task），shutdown 时取消；进度回调 `_enqueue` 快照式入队，保证最新事件不丢。
- **提示词预设**：三维组合 `语言 × 格式 × 训练目标`，定义在 `core.PROMPT_PRESETS` / `DEFAULT_PROMPTS` / `TRAINING_PROMPTS`，预设校验与兼容迁移逻辑在 `core.load_settings`。
- **依赖唯一来源**：`requirements.txt`（Docker 共用）。注释 / 文档用中文。

## 开发与测试

```bash
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt      # 新增依赖：改 requirements.txt 后重装
python -m uvicorn app:app --host 0.0.0.0 --port 8080
pytest                                   # testpaths = tests
```

- 测试用 `pytest_tmp` 项目；`tests/conftest.py` 自动保护 `config/settings.json`（测试前后原样还原）。
- 已知回归点：CSS 中 `[hidden] { display: none !important; }` 守卫不可删（防 author display 规则覆盖 hidden 导致黑屏）；HTMX swap 后需重挂事件监听。

## 详细文档（见 doc/，本文件仅索引）

- `doc/设计.md` — 设计文档（当前实现，权威参考）：数据约定、REST 接口清单、HX-Trigger 事件总线、提示词预设规范、settings.json schema、API 兼容边界（Claude 需中转、Ollama 需 /v1 等）。