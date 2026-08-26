# TagForge 重构方案：FastAPI + Jinja2 + HTMX（替代 NiceGUI）

> 状态：方案草案（v1）。决策背景见对话评估：NiceGUI 3.16.0 安装体积 28MB、venv 共 75 个包（拖入 selenium / python-socketio / aiohttp / trio 等无关依赖），对本项目（单人局域网、零数据库、纯文件系统存储）过重。
> 本方案：**业务逻辑零改动**，仅重写 UI 层；异步模型、并发、文件约定全部保留。
> ⚠️ 需同步修订 `doc/设计.md` 第 12 行的硬性要求「Web 框架：仅使用 NiceGUI」及 README 的依赖说明，否则后续开发会照旧引回 NiceGUI。

---

## 一、技术选型与理由

| 项 | 选择 | 理由 |
|---|---|---|
| Web 框架 | **FastAPI**（starlette 之上） | NiceGUI 的底层本来就是 FastAPI/starlette，迁移≈去掉一层壳；原生 async 匹配现有 `AsyncOpenAI` 并发代码 |
| 模板 | **Jinja2** | 服务端渲染 + 局部片段，无前端构建步骤 |
| 局部刷新 | **HTMX**（`htmx.min.js` ≈ 14KB，vendor 到 static） | 网格/抽屉/进度均可用 HTML 片段 swap，事件用 `HX-Trigger` 广播 |
| 高频进度通道 | **SSE**（`text/event-stream`，原生 EventSource） | 仅批量进度一路走 SSE；其余全部 HTMX，避免轮询滥用 |
| 少量交互 JS | `static/js/app.js`（≈150 行手写） | 拖拽上传、键盘快捷键、lightbox、Toast、剪贴板四项，无需框架 |
| 线程池替代 | `asyncio.to_thread` | 替代 `run.io_bound`（缩略图 / encode / 文件 IO），FastAPI 原生可用 |
| 后台任务替代 | `asyncio.create_task` | 替代 `background_tasks.create`，任务引用存入 AppState；shutdown 时取消 |

**依赖变化**：删 `nicegui`；新增 `fastapi`、`uvicorn`、`jinja2`、`python-multipart`（已有）+ vendor 的 htmx（2 个 JS 文件不进 pip）。
实际新装 ≈ 4 个包，移除 ≈ 60 个传递依赖；Docker 镜像明显缩小（nicegui 28MB + 其静态资源不再进入镜像）。

---

## 二、目标目录结构

```
TagForge/
├─ app.py                  # FastAPI 入口：路由注册、SSE、启动入口（uvicorn app:app）
├─ core.py                 # 纯业务逻辑（从 main.py 抽出，0 框架依赖，可单测）
├─ llm_client.py           # 不变
├─ templates/
│  ├─ base.html            # 页面外壳：顶栏 + 左抽屉 + 主区 + 右抽屉 + lightbox + toast 容器
│  └─ partials/            # 全部为可独立 swap 的 HTML 片段
│     ├─ projects.html          # 左抽屉：项目列表
│     ├─ settings_panel.html    # 左抽屉：模型配置 / 提示词预设 / 高级 三 Tab 内容
│     ├─ main.html              # 主区：工具栏（项目名/上传/批量/进度条）+ 网格挂载点 + 批量卡
│     ├─ grid_cards.html        # 仅卡片区（含「加载更多」按钮），分页/筛选/搜索时替换此区
│     ├─ card.html              # 单张卡片（grid_cards 内 for 循环）
│     ├─ meta_bar.html          # 统计条（总数/已标注/待标注/失败）
│     ├─ detail.html            # 右抽屉：大图 + 标签 textarea + 保存/再生/删除 + 上一张/下一张
│     ├─ batch_card.html        # 批量进度卡：进度条 + 统计 + 日志区 + 终止/重试/复制按钮
│     └─ dialogs.html           # 新建项目 / 删除项目 / 删除图片 / 帮助 四个 <dialog>
├─ static/
│  ├─ css/tf.css           # 现有 CSS 常量（main.py 内 CSS 字符串）迁移并去 Quasar 类
│  ├─ js/app.js            # 拖拽上传、快捷键、lightbox、Toast、剪贴板、SSE 装配
│  └─ vendor/
│     ├─ htmx.min.js
│     └─ htmx-sse.js       # 官方 SSE 扩展（若 app.js 手写 EventSource 则不需要）
├─ config/  datasets/  exports/    # 不变
├─ requirements.txt  Dockerfile  docker-compose.yml   # 按 §七 更新
└─ tests/                 # 保留 smoke；新增 core 单测
```

### 1. 文件职责

- `app.py`：路由薄层。**不含业务逻辑**，只做：参数解析 → 调 core 函数 → 渲染模板/Jinja2 片段 / 返回 JSON / FileResponse / SSE。
- `core.py`：从 `main.py` **原样搬迁**以下模块（逐行可 git 保留，杜绝行为漂移）：

| 现有代码块（main.py） | 迁至 core.py 的形态 |
|---|---|
| 常量：`ROOT/DATASETS/CONFIG/SETTINGS_FILE/IMAGE_EXTS/PAGE_SIZE` | 不变 |
| `DEFAULT_PROMPTS/TRAINING_PROMPTS/PROMPT_PRESETS/MODEL_PRESETS/DEFAULT_SETTINGS` | 不变 |
| `STATUS_TEXT/STATUS_COLOR` | 不变 |
| `ImageEntry` | **瘦身**：删除 `badge/caption/card/check`（UI 元素引用），保留 `name/path/status/thumb/preview/label` |
| `AppState` | 删除 `tagbox` 与 `UI: dict`；保留 `settings/projects/current/entries/client/batch/abort_batch/view_filter/view_query/view_page` 等；新增 `batch_queue: asyncio.Queue`（SSE 桥，见 §四.5） |
| `load_settings/save_settings` | 不变 |
| `scan_projects/images_dir/project_images/label_file/read_status/read_label/write_label/apply_prefix` | 不变 |
| `make_thumb/encode_for_api` | 不变（被 `asyncio.to_thread` 包装调用） |
| `ensure_client/client_ready` | 不变 |
| `compute_stats/filtered_entries` | 不变（供 `/partials/grid_cards` 用） |
| `export_zip` | 改为 `def export_zip(project) -> Path`（返回 zip 路径，由 app.py 用 FileResponse 下发） |
| 新/删项目、上传保存、标签写盘 | 抽为 `create_project / delete_project / save_upload(project, filename, data) -> (ok, renamed)` |
| `run_batch` | **签名改造**：`async def run_batch(targets, progress_cb) -> BatchResult`，UI 更新全部改为回调（见 §四.5） |

### 2. 状态与并发模型（不变的部分）

模块级 `state = AppState()` 单例保持不变（单人使用成立）。FastAPI 无会话态，浏览器只是渲染端。
与 NiceGUI 的最大差异：**服务器不再持有任何 DOM 引用**，因此不存在「element deleted」「已断开 client」异常 —— 断连保护问题在架构上消失。

---

## 三、页面与布局（base.html 骨架）

```html
<!doctype html>
<html lang="zh" data-theme="{{ 'dark' if settings.dark else 'light' }}">
<head>
  <meta charset="utf-8">
  <title>TagForge — LoRA 图片打标工具</title>
  <link rel="stylesheet" href="/static/css/tf.css">
  <script src="/static/vendor/htmx.min.js" defer></script>
  <script src="/static/js/app.js" defer></script>
</head>
<body>
  <!-- 顶栏 -->
  <header id="app-header">
    <span class="tf-logo">⚒️ TagForge</span>
    <span class="tf-tagline">LoRA 数据集图片打标工具</span>
    <div class="ml-auto">
      <span id="header-model">{{ settings.model }}</span>
      <span id="header-tokens">⏣ 0 / 0</span>
      <label>深色 <input type="checkbox" id="dark-switch"
             {% if settings.dark %}checked{% endif %}></label>
    </div>
  </header>
  <div id="app-body">
    <!-- 左抽屉（固定 280px）：三 Tab -->
    <aside id="app-drawer">
      <div class="tf-tabs" data-tabs>
        <button data-tab="projects" class="active">项目</button>
        <button data-tab="settings">配置</button>
        <button data-tab="advanced">高级</button>
      </div>
      <div class="tf-tabs-body">
        <section data-panel="projects" class="active">
          {% include "partials/projects.html" %}
        </section>
        <section data-panel="settings" hidden>
          {% include "partials/settings_panel.html" %}
        </section>
        <section data-panel="advanced" hidden>
          {% include "partials/settings_panel.html" %}
        </section>
      </div>
    </aside>
    <!-- 主区 -->
    <main id="main">
      {% include "partials/main.html" %}
    </main>
  </div>
  <!-- 右抽屉（详情）：初始为空，点击卡片后 hx-get 注入 -->
  <aside id="detail-drawer" class="hidden"></aside>
  <!-- 全屏 lightbox -->
  <div id="lightbox" class="hidden"></div>
  {% include "partials/dialogs.html" %}
  <div id="toast-container"></div>
</body>
</html>
```

要点：
- 三 Tab 用 app.js 切换（`data-tabs`/`data-panel`），内容随页面渲染，无需懒加载（配置表单量小）。若日后要懒加载，切 Tab 时 `hx-get="/partials/settings_panel"` 注入即可，接口已预留。
- 顶栏 tokens 由批量完成事件触发局部刷新，不在每次渲染时重查。

---

## 四、接口清单（REST 设计）

### 页面与静态

| Method | Path | 说明 |
|---|---|---|
| GET | `/` | base.html 整页（Jinja2） |
| GET | `/static/*` | `StaticFiles`（挂 `/static`） |

### 项目

| Method | Path | 请求 | 响应 |
|---|---|---|---|
| GET | `/partials/projects` | — | `projects.html` 片段（进入页面/新建/删除后刷新左抽屉） |
| POST | `/api/projects` | 表单 `name` | 创建目录（名称白名单）→ 返回 `HX-Trigger: {"projectChanged": true}` + 新项目网格片段 |
| DELETE | `/api/projects/{name}` | — | `shutil.rmtree`（含 `.cache/{name}`）→ 返回 `HX-Trigger: projectChanged` |
| POST | `/api/projects/{name}/select` | — | 设 `state.current`、重建 `entries`（异步缩略图，`asyncio.to_thread` 并发）→ 返回 `main.html`（含网格/统计/顶栏标题） |

### 网格

| Method | Path | 请求 | 响应 |
|---|---|---|---|
| GET | `/partials/grid_cards` | `?project=&filter=all\|tagged\|pending\|failed&q=&page=` | 卡片区 `<div id="grid-cards">`（内含「加载更多」按钮，`hx-get` 同参数 `page+1`，`hx-swap="beforeend"`） |
| GET | `/partials/meta_bar` | 同上 | 统计条（总数/已标注/待标注/失败） |

交互接线：
- 筛选下拉/搜索框：`hx-get="/partials/grid_cards" hx-target="#grid-cards" hx-trigger="change, input changed delay:300ms"`（**debounce 交给 HTMX**，替代原 `search_seq` 防抖，core 中该字段可删）。
- 卡片点击：`hx-get="/partials/detail?name=..." hx-target="#detail-drawer" hx-swap="innerHTML"`，app.js 在 `htmx:afterSwap` 后给抽屉加 `open` class（滑入动画）。

### 图片（直连静态语义，不经 Base64）

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/image/{project}/{name}` | `FileResponse(原图)` —— 详情大图 / lightbox 的 `<img src>` |
| GET | `/api/image/thumb/{project}/{name}.jpg` | `FileResponse(缓存缩略图)`，miss 时现场生成（`asyncio.to_thread(make_thumb)`） |

> 原实现把缩略图/大图做成 data URL 注入 DOM；改为 `<img src="/api/image/...">` 后 DOM 更小、浏览器原生缓存，**是本次重构的额外收益**，`ImageEntry.thumb/preview` 可改为仅存文件名或直接删除。

### 详情 / 标签

| Method | Path | 请求 | 响应 |
|---|---|---|---|
| GET | `/partials/detail` | `?project=&name=` + `index=`（供上/下一张） | `detail.html`：大图 + `<textarea>` + 保存/再生/删除 + ◀▶ 导航 |
| POST | `/api/label/{project}/{name}` | 表单 `text` | 写盘 → 返回 `HX-Trigger: {"cardUpdated": {"name": ...}}`（前端局部刷新该卡片） |
| POST | `/api/regenerate` | `project, name` | `asyncio.create_task` 单张生成；完成后 `HX-Trigger: regenerated` → 刷新卡片 + 抽屉 |
| DELETE | `/api/image/{project}/{name}` | — | 删原图 + 同名 `.txt` + 缓存缩略图 → `HX-Trigger: gridChanged` |
| POST | `/api/trial` | `project, name, prompt`（可选测试图） | 试生成，返回 JSON `{text, elapsed}` → app.js 填入抽屉 textarea（不做 DOM 替换，避免打断输入） |

交互接线：
- 文本域即时保存：`hx-post="/api/label/..." hx-trigger="change, keyup changed delay:800ms"`，与现行为一致（微调即视为自定义）。
- Ctrl+Enter 保存：app.js 侦听 `keydown`，找到 `data-save-shortcut` 元素触发其表单提交。
- ◀▶ 导航：app.js 维护 `state.index`，`hx-get="/partials/detail?index=i±1"`。

### 批量标注（SSE 通道）

| Method | Path | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/batch/start` | —（取 pending/failed） | 校验 client/项目 → `asyncio.create_task(core.run_batch(targets, progress_cb))`，返回 202 |
| POST | `/api/batch/stop` | — | `state.abort_batch = True` → 204 |
| POST | `/api/batch/retry` | — | 取 failed 重新启动 → 202 |
| GET | `/api/batch/events` | SSE | 事件流（见下） |
| GET | `/api/export/{project}` | — | `export_zip()` → `FileResponse(..., media_type="application/zip", filename=f"{project}.zip")` |

**SSE 协议**（`text/event-stream`）：

```
event: progress
data: {"done": 12, "total": 30, "ok": 11, "fail": 1, "elapsed": 45.2, "log": "girl_01.jpg ✅ 成功 (1.2s)"}
event: done
data: {"ok": 28, "fail": 2, "aborted": false}
```

实现要点：
- `core.run_batch` 改造：`progress_cb(dict)` 在里程碑处被调用（每张完成/日志追加/终止/完成）。`progress_cb` 内部 `queue.put_nowait(payload)`，失败即丢（**快照式**，保证不阻塞批次）。
- `AppState.batch_queue = asyncio.Queue(maxsize=1)`：SSE 端点循环 `queue.get()` → yield sse；无消费者时队列自然空转不报错 —— **天然实现 NiceGUI 的“断连不 crash”语义，且无需 try/except 包裹 UI 更新**。
- 防串批：SSE 端点启动时记录 `batch_id = id(state.batch)`，收到不匹配的进度事件直接跳过。
- 前端：`new EventSource('/api/batch/events')`，`onmessage` 更新进度条/统计/日志区；`addEventListener('done')` 后 `close()` 并触发 `refresh-grid` 与 `refresh-tokens`。
- 批量完成后服务器不再持有 DOM，无需原 `ui_guard/_alive` 防御，相关函数整体删除。

### 配置

| Method | Path | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/settings` | JSON `{"key": value}` | `set_setting` + `save_settings` → 204（表单控件均失焦/变更即发，含深色开关、并发数、角色名、触发词） |
| POST | `/api/settings/prompt` | `{prompt_preset}` | 按预设填充 `system_prompt`（等价原 `on_prompt_preset_change`，含 `resolve_prompt_text`）→ 返回 `{system_prompt}`（前端填回 textarea）；选「自定义」不做填充 |
| POST | `/api/settings/prompt/default` | — | 恢复默认短标签英文 → 返回 `{system_prompt}` |
| POST | `/api/test-connection` | 当前 settings | `await client.ping()` → JSON `{ok, detail}`（Toast 展示）。逻辑移入 core：`def make_client(settings) -> LLMClient` 便于路由直接构造 |

> 提示词微调、角色名等「变更即视为自定义」的联动逻辑（`suppress_prompt_sync`）保留在 core 的 `resolve_prompt_text`，路由层只做转发。

### 上传

| Method | Path | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/upload` | multipart `files[]`（python-multipart） | 逐文件校验扩展名白名单、同名自动改名 `name (1).ext`、写盘 → JSON `{ok, fail, renamed}` → app.js 汇总 Toast + 网格刷新 |

- **拖拽上传**：app.js 在 `#main` 上挂 `dragover/drop` 监听，把 `dataTransfer.files` 塞进表单；原生 `<input type=file multiple>` 兜底。
- 单文件进度：用 `XMLHttpRequest`（有 `upload.onprogress`）而非 fetch，逐文件显示百分比。

---

## 五、shared 事件总线（HX-Trigger 约定）

| 事件名 | 触发方 | 监听方动作 |
|---|---|---|
| `projectChanged` | 新建/删除/选择项目 | 刷新左抽屉 `#projects` + 主区 `#main` |
| `gridChanged` | 上传/删除图片/批量完成 | 刷新 `#grid-cards` + 统计条 |
| `cardUpdated` | 标签保存/再生完成 | 仅替换对应卡片（`hx-target` 用卡片 `data-name` 定位）|
| `tokensUpdated` | 批量完成/试生成 | 刷新顶栏 tokens |
| `toast` | 任意 API（`HX-Trigger` 携带） | app.js 弹提示（自实现，无依赖）|

---

## 六、app.js 职责清单（≈150 行，不含 htmx）

1. **Toast**：`showToast(msg, type)`，挂到 `#toast-container`，3s 自动消失；监听 `htmx:responseError` 显示错误。
2. **三 Tab 切换**：`data-tabs` 点击 → 切 `active`/`hidden`。
3. **右侧抽屉**：`htmx:afterSwap` 命中 `#detail-drawer` 时加 `open` class；关闭按钮/Esc 移除。
4. **lightbox**：点击详情大图 → 在 `#lightbox` 内放全屏 `<img src="/api/image/...">`；点击/Esc 关闭。
5. **键盘快捷键**：`keydown`：`Escape`（关 lightbox/抽屉）、`Ctrl+Enter`（保存当前标签）、`←/→`（详情导航）。
6. **拖拽上传 + 逐文件进度**。
7. **批量 SSE**：组装 EventSource、渲染进度/日志、done 后清理。
8. **剪贴板**：复制日志按钮 `navigator.clipboard.writeText`。
9. **深色主题**：checkbox 切换 `document.documentElement.dataset.theme` + `POST /api/settings`。

---

## 七、requirements / Docker 变更

```diff
# requirements.txt
- nicegui==3.16.0
+ fastapi
+ uvicorn
+ jinja2
  openai>=1.40,<2
  httpx>=0.27,<1
  pillow>=10.3,<12
  python-multipart>=0.0.12
```

```diff
# Dockerfile
- CMD ["python", "main.py"]
+ CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

镜像变化：不再包含 nicegui 的 Quasar/Vue 静态资源与前端代码，`python:3.12-slim` 下预计可缩小 100–200MB。

---

## 八、迁移步骤（每步可运行、可回退）

| 步骤 | 内容 | 验收 |
|---|---|---|
| **1** | 从 `main.py` 抽出 `core.py`（§二.1 表格全部函数，含 `run_batch` 回调化）；`main.py` 暂时 import core 保持可跑 | 现有 `pytest` 通过；原功能不变 |
| **2** | 搭 `app.py` + `base.html` + `tf.css` 骨架，`GET /` 渲染顶栏/左抽屉/空主区 | 页面打开，深色切换生效 |
| **3** | 项目 CRUD + 网格（`/partials/projects`、`/partials/grid_cards`、筛选/搜索/分页、缩略图静态化、卡片点击） | 核心浏览链路可用，移除 NiceGUI |
| **4** | 详情抽屉 / 标签保存 / 再生 / 删除 / lightbox / 上传（含拖拽） | 标注主链路全通 |
| **5** | 批量 SSE 进度卡 + 导出 + 顶栏 tokens + 测试连接（收尾） | 与现 UI 功能对齐；清理 `.shots/`；更新 `doc/设计.md` 硬性要求与 README |

每步结束：`pytest -q` + 手动冒烟清单（见附录 B）。

---

## 九、风险与对策

| 风险 | 对策 |
|---|---|
| SSE 无内置标签 | app.js 原生 `EventSource`（首选）；`htmx-sse.js` 备选 |
| 批量进行中手动编辑标签被批量结果覆盖 | 与现状同（NiceGUI 也是直写）。可选 P2：`write_label` 前比较 mtime |
| 进度队列阻塞批次 | `put_nowait` + `maxsize=1`（快照式），丢事件不丢正确性 |
| 旧 SSE 连接读到新批次进度 | SSE 端点带 `batch_id` 过滤 |
| 长任务与 uvicorn 生命周期 | `app.on_event("shutdown")` 中 cancel `state.batch` |
| 配置写入竞态（settings.json） | 单用户断言成立；如需 `save_settings` 加 `asyncio.Lock` |
| 丢 Quasar 组件样式 | 现有 `tf.css` 以自定义 CSS 为主，迁移时逐组件换原生样式（见附录 A） |

---

## 附录 A：组件替换对照表（NiceGUI → 原生/HTMX）

| NiceGUI | 替代 |
|---|---|
| `ui.card/row/column` | `<div>` + flex/grid CSS |
| `ui.tabs/tab/tab_panels` | `data-tabs` + JS 切换（§三） |
| `ui.button` | `<button>` |
| `ui.input/select/radio/number/switch` | 原生控件 + tf.css |
| `ui.textarea` | `<textarea>` + `hx-post` 防抖保存 |
| `ui.upload` | `<input type=file multiple>` + 拖拽 |
| `ui.dialog` | `<dialog>` 元素（原生 `showModal()`）|
| `ui.download` | `FileResponse` + `<a download>` |
| `ui.notify` | 自实现 Toast（§六.1）|
| `ui.linear_progress` | `<progress>` 或 div 宽度 |
| `ui.image`（data URL） | `<img src="/api/image/...">` 静态直连 |
| `ui.keyboard` | `document.keydown` |
| `ui.run_javascript` | app.js 直接执行 |
| `run.io_bound` | `asyncio.to_thread` |
| `background_tasks.create` | `asyncio.create_task` |
| `app.storage`（未使用） | 模块级单例 `state`（沿用）|



---

## 附录 C：实施状态（2025-08-26 完成）

| 步骤 | 状态 | 关键产出 |
|---|---|---|
| 1 抽 core.py | ✅ | `core.py`（0 框架依赖）；`main.py` 阶段委托并保持原 NiceGUI 冒烟通过后删除 |
| 2 FastAPI 骨架 | ✅ | `app.py` + `templates/base.html` + `static/`（tf.css / app.js / vendor htmx.min.js） |
| 3 项目 CRUD + 网格 | ✅ | select/create/delete、`/partials/grid_cards`（筛选/搜索/分页）、缩略图静态直连 |
| 4 详情/标注/上传 | ✅ | `/partials/detail`、`/api/label`、`/api/regenerate`、`/api/image`（GET/DELETE）、multipart 上传（XHR 逐文件进度 + 拖拽） |
| 5 批量 SSE + 收尾 | ✅ | `/api/batch/start|stop|retry` + `/api/batch/events`（SSE 快照队列，done 不丢）、`/api/export`、`/api/trial`、`/api/test-connection`、`/api/status/tokens`、提示词预设/恢复默认、配置面板全绑定；删除 `main.py`、重写 tests、更新 requirements/Dockerfile/README/设计.md |

运行：`uvicorn app:app --host 0.0.0.0 --port 8080`；测试：`python -m pytest tests -q`（4 passed）。

**实现要点**：
- SSE 队列采用「新事件替换旧事件」的快照语义（`_enqueue`），客户端晚接入也不会丢 `done`；
- 上传/删除后由服务端重扫 `state.entries`，保证网格与磁盘一致；
- 断连保护天然消失：服务器不持有 DOM 引用，SSE 无消费者时事件自然丢弃；
- 全部 HX-Trigger 约定（gridChanged / detailReload / detailClosed / batchStarted / toast / tokensUpdated）在 app.js 中统一监听。


## 附录 B：每步冒烟清单

1. 新建「test」项目 → 上传 3 张图（1 张不支持的扩展名）→ 同名再传 1 张 → 确认改名与汇总 Toast。
2. 筛选/搜索/加载更多；点击卡片 → 抽屉滑出 → 改标签 Ctrl+Enter → 卡片徽章与缩略图下方文本更新。
3. 配置模型(Ollama llava) → 测试连接 → 单张试生成。
4. 批量标注 2 张（并发 2）→ 进度条/日志实时 → 中途点终止 → 重试失败。
5. 删除一张图（确认弹窗）→ 删除项目 → 导出 ZIP（含 images/ 与 labels/）。
6. 深色切换 → 刷新页面状态保持；无 NiceGUI 依赖报错；`pytest -q` 绿。

> ✅ 上述清单已随 5 步实施逐项通过（含 stub LLM 端到端批量验证）。
