# ⚒️ TagForge

LoRA 数据集图片打标工具：Web 端批量标注（Python + FastAPI + Jinja2 + HTMX，零数据库，文件系统存储）。
单机 / 局域网单人使用。

## 一、目录结构

```
TagForge/
├─ app.py                # FastAPI 入口（路由 + 渲染，业务逻辑在 core.py）
├─ core.py               # 纯业务逻辑（0 框架依赖，可单测）
├─ llm_client.py         # OpenAI 兼容大模型异步封装
├─ pyproject.toml        # 依赖主清单（uv sync，生成 uv.lock）
├─ requirements.txt      # Docker/兼容导出（uv export 生成，与 pyproject 同步）
├─ templates/  static/   # Jinja2 模板 + CSS/JS/htmx（服务端渲染，无前端构建）
├─ Dockerfile
├─ docker-compose.yml
├─ doc/设计.md           # 设计文档（当前实现）
└─ datasets/             # 运行时生成；按项目存放图片与标签
└─ config/settings.json  # 运行时生成；API Key、Base URL、模型等配置
```

数据约定：`datasets/<项目>/images/` 存图片，`datasets/<项目>/labels/` 存同名 `.txt` 标签。

## 二、宿主机运行（推荐：使用 uv 构建虚拟环境）

要求 Python 3.12+。

```bash
# 1) 安装 uv（未安装时）
curl -LsSf https://astral.sh/uv/install.sh | sh     # 或：pip install uv

# 2) 创建虚拟环境并安装依赖
cd TagForge
uv sync
source .venv/bin/activate        # Windows: .venv\Scripts\activate
# 或：uv venv && uv pip install -r requirements.txt

# 3) 启动
uv run python -m uvicorn app:app --host 0.0.0.0 --port 8080   # 打开 http://localhost:8080
```

新增依赖：`uv add <pkg>` 或改 `pyproject.toml` 后 `uv sync`，同步导出 `uv export --format requirements-txt --no-hashes > requirements.txt`。

> 架构说明：UI 层为 FastAPI + Jinja2 服务端渲染 + HTMX 局部刷新（见 `doc/设计.md`）；业务逻辑全部在 `core.py`，不依赖任何 Web 框架。

## 三、Docker 部署

```bash
mkdir -p datasets config exports   # 避免 root 创建导致权限问题
docker compose up -d
# 打开 http://<主机IP>:8080
```

停止：`docker compose down`。数据挂载在宿主机的 `./datasets`、`./config`、`./exports`。

## 四、配置 API Key / 模型

打开界面左侧面板：
- 选择「模型预设」，或手动填 Base URL 与模型名；预设支持**新增 / 编辑 / 删除**（内置与自定义统一，保存于 `config/presets.json`）；
- 模型名输入框旁点「⟳」或切换预设时，会**自动从提供商拉取可用模型列表**供选择（`GET /v1/models`）；网关不支持时提示手动输入；
- 填写 API Key（本地 Ollama 可留空，Base URL 填 `http://<host>:11434/v1`）；
- 「提示词预设」下拉框（取代原「打标模式」单选），**输出语言 × 格式 × 训练目标**三维组合共 17 项（例：**英文 · 自然语言 · 角色 LoRA**、**中文 · 短标签 · 服装 LoRA**、**中文 · 自然语言 · 风格 LoRA**、**自定义**）。每个预设各有英文/中文输出版，选预设自动填充下方文本框（可再手动微调，微调后自动视为「自定义」）；
- 可设置**角色名**（角色 LoRA 用）：填写后「角色 LoRA」预设会用该名称称呼角色并置于输出开头（如 `mw_cyber_girl`）；还可设置触发词前缀、并发数。

> 兼容性说明：Claude 官方 API 非 OpenAI 兼容，需中转站；DeepSeek-VL 需自建 /v1 端点；Ollama 端口须带 /v1。

## 五、使用流程

1. 左侧「新建项目」→ 选择项目；
2. 顶栏「上传图片」（支持多选/拖拽）；
3. 点击图片卡片进入右侧详情：编辑标签 / 重新生成 / 删除；
4. 顶栏「开始批量标注」：为待标注/失败图片并发生成标签，主区进度卡实时显示进度、统计与日志；
5. 左侧「打包导出」：导出图片 + 标签为 ZIP。

## 六、支持的图片格式

`jpg / jpeg / png / webp / bmp`（暂不支持 gif / avif）。

## 七、常见问题

- 批量不可用：未配置 Base URL/模型，或 API Key 为空（Ollama 除外）时，主区顶部会显示配置提示横幅，点「开始批量标注」也会提示先配置。
- 图片无法上传：确认扩展名在白名单内。
- 失败图片重启后会回到「待标注」，再次批量会自动重跑。