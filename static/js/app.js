/* TagForge 前端脚本
 * 骨架：主题 / Tab / Toast / 通用设置保存
 * 交互：上传拖拽、详情抽屉、lightbox、快捷键、批量 SSE
 */
(function () {
  "use strict";
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const TF = window.TF = {};

  // ---------- Toast ----------
  function toast(msg, type, timeout) {
    const box = $("#toast-container");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "tf-toast " + (type === "positive" ? "tf-toast-ok" : type === "negative" ? "tf-toast-err" : "tf-toast-info");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => el.remove(), timeout || 3500);
  }
  TF.toast = toast;
  document.addEventListener("htmx:responseError", (e) => {
    toast("请求失败（HTTP " + (e.detail && e.detail.xhr && e.detail.xhr.status || "?") + "）", "negative", 5000);
  });

  // ---------- 深色主题 ----------
  TF.toggleTheme = function (on) {
    document.documentElement.dataset.theme = on ? "dark" : "light";
    fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dark: on }) });
  };
  const darkSwitch = $("#dark-switch");
  if (darkSwitch) darkSwitch.addEventListener("change", () => TF.toggleTheme(darkSwitch.checked));

  // ---------- Tab 切换 ----------
  $$("[data-tabs]").forEach((tabs) => {
    tabs.addEventListener("click", (e) => {
      const btn = e.target.closest(".tf-tab");
      if (!btn) return;
      $$(".tf-tab", tabs).forEach((b) => b.classList.toggle("active", b === btn));
      $$(".tf-tab-panel").forEach((p) => { p.hidden = p.id !== btn.dataset.tab; });
    });
  });

  // ---------- 通用设置保存（失焦/变更即存） ----------
  TF.saveSetting = async function (key, value) {
    try {
      await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [key]: value }) });
    } catch (err) { /* 静默：下个变更覆盖 */ }
  };
  TF.saveSettingDebounced = function (key, input, delay) {
    clearTimeout(input._t);
    input._t = setTimeout(() => TF.saveSetting(key, input.value), delay || 600);
  };

  // ---------- HX-Trigger 事件总线 ----------
  document.addEventListener("toast", (e) => {
    const d = e.detail || {};
    if (d.msg) TF.toast(d.msg, d.type || "info");
  });

  // ---------- Dialog 通用 ----------
  function openDialog(id) {
    const d = document.getElementById(id);
    if (d && !d.open) d.showModal();
  }
  $$("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

  // 通用确认框（Promise）
  TF.confirm = function (text) {
    return new Promise((resolve) => {
      const dlg = $("#dlg-confirm");
      if (!dlg) return resolve(false);
      let done = false;
      const finish = (v) => { if (done) return; done = true; dlg.close(); resolve(v); };
      const ok = $("#btn-confirm-ok");
      ok.onclick = () => finish(true);
      dlg.oncancel = () => finish(false);
      dlg.onclose = () => finish(false);
      $("#confirm-text").textContent = text;
      if (!dlg.open) dlg.showModal();
    });
  };

  // ---------- 项目 ----------
  const gridSwap = { target: "#grid", swap: "innerHTML" };
  $("#btn-new-project")?.addEventListener("click", () => openDialog("dlg-new-project"));
  $("#btn-confirm-new-project")?.addEventListener("click", async () => {
    const name = $("#new-project-name").value.trim();
    if (!name) { TF.toast("项目名不能为空", "negative"); return; }
    $("#dlg-new-project").close();
    await htmx.ajax("POST", "/api/projects", Object.assign({ values: { name } }, gridSwap));
    $("#new-project-name").value = "";
  });
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-project]");
    if (t) htmx.ajax("POST", "/api/projects/" + encodeURIComponent(t.dataset.project) + "/select", gridSwap);
  });
  $("#btn-delete-project")?.addEventListener("click", async () => {
    const cur = document.getElementById("toolbar-title")?.textContent;
    if (!cur || cur === "（未选择项目）") { TF.toast("请先选择项目", "warning"); return; }
    if (await TF.confirm("确定删除整个项目「" + cur + "」吗？该操作不可恢复。")) {
      htmx.ajax("DELETE", "/api/projects/" + encodeURIComponent(cur), gridSwap);
    }
  });


  // ---------- 事件总线（HX-Trigger -> 自定义事件） ----------
  let _lastGrid = 0;
  function refreshGrid() {
    const now = Date.now();
    if (now - _lastGrid < 300) return;
    _lastGrid = now;
    if ($("#grid")) htmx.ajax("GET", "/partials/grid_cards", gridSwap);
  }
  document.addEventListener("gridChanged", refreshGrid);
  document.addEventListener("detailReload", () => {
    const drawer = $("#detail-drawer");
    if (drawer && drawer.dataset.name) {
      const cur = document.getElementById("toolbar-title");
      const project = cur && cur.textContent !== "（未选择项目）" ? cur.textContent : "";
      htmx.ajax("GET", "/partials/detail?project=" + encodeURIComponent(project) + "&name=" + encodeURIComponent(drawer.dataset.name),
        { target: "#detail-drawer", swap: "innerHTML" });
    }
  });
  document.addEventListener("detailClosed", closeDetail);
  // 兜底：204 + swap:none 时部分 htmx 版本 HX-Trigger 可能未冒泡到 document，显式检查 header 补刷新（保留 HX-Trigger 主链路，不重复派发 toast）
  document.addEventListener("htmx:afterRequest", (e) => {
    const xhr = e.detail && e.detail.xhr;
    if (!xhr || !xhr.getResponseHeader) return;
    const hdr = xhr.getResponseHeader("HX-Trigger") || xhr.getResponseHeader("hx-trigger");
    if (!hdr) return;
    // 若 document 已收到 gridChanged/detailReload，则 htmx 已派发，无需兜底；通过检查 header 中是否含对应键且当前未触发来补
    // 为避免重复 toast，此处不派发 toast，仅补网格/详情刷新
    let data = null;
    try { data = JSON.parse(hdr); } catch (_) {
      if (hdr.includes("gridChanged")) refreshGrid();
      if (hdr.includes("detailReload")) document.dispatchEvent(new CustomEvent("detailReload"));
      if (hdr.includes("detailClosed")) document.dispatchEvent(new CustomEvent("detailClosed"));
      return;
    }
    // 仅当 htmx 未派发时兜底：通过临时标记避免双触发（hmtx:afterRequest 在 gridChanged 之后触发，若已刷新则 xhr 的 header 仍会进入此分支，需去重）
    // 简单去重：若 header 含 toast，说明原生已派发 toast，无需再处理；仅补网格/详情
    if (data.gridChanged) {
      // 若原生已触发，refreshGrid 已调用，此处再次调用幂等（GET 幂等），保留以覆盖未冒泡场景
      refreshGrid();
    }
    if (data.detailReload) document.dispatchEvent(new CustomEvent("detailReload"));
    if (data.detailClosed) document.dispatchEvent(new CustomEvent("detailClosed"));
  });

  // ---------- 详情抽屉 ----------
  const drawer = $("#detail-drawer");
  function closeDetail() { if (drawer) { drawer.classList.remove("open"); drawer.dataset.name = ""; } }
  // 详情关闭按钮在动态内容里，须用委托
  document.body.addEventListener("click", (e) => {
    if (e.target.closest("#btn-detail-close")) closeDetail();
  });
  document.body.addEventListener("htmx:afterSwap", (e) => {
    if (e.detail && e.detail.target && e.detail.target.id === "detail-drawer") {
      drawer.classList.add("open");
      const nameEl = drawer.querySelector(".tf-detail-title");
      drawer.dataset.name = nameEl ? nameEl.textContent : "";
      const ta = drawer.querySelector("#tag-text");
      if (ta) ta.focus({ preventScroll: true });
    }
  });

  // ---------- 删除图片 ----------
  document.body.addEventListener("click", (e) => {
    const btn = e.target.closest("#btn-delete-image");
    if (!btn) return;
    const drawerEl = $("#detail-drawer");
    const name = drawerEl && drawerEl.dataset.name;
    const project = $("#toolbar-title")?.textContent;
    if (!name || !project || project === "（未选择项目）") return;
    TF.confirm("确定删除该图片及其标签吗？此操作不可恢复。").then((yes) => {
      if (yes) htmx.ajax("DELETE", "/api/image/" + encodeURIComponent(project) + "/" + encodeURIComponent(name), gridSwap);
    });
  });

  // ---------- 清除标注（详情单张 + 卡片 hover 快捷） ----------
  document.body.addEventListener("click", (e) => {
    const btn = e.target.closest("#btn-clear-label");
    if (!btn) return;
    const name = $("#detail-drawer")?.dataset.name;
    const project = $("#toolbar-title")?.textContent;
    if (!name || !project || project === "（未选择项目）") return;
    TF.confirm("确定清除「" + name + "」的标注吗？标签文件将被删除。").then((yes) => {
      if (yes) htmx.ajax("DELETE", "/api/label/" + encodeURIComponent(project) + "/" + encodeURIComponent(name), { target: "body", swap: "none" });
    });
  });
  // 卡片 hover 快捷清除（tagged/failed 卡片右下角橡皮）—— capture 阶段拦截，防止触发卡片的 hx-get
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".tf-card-clear");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    const name = btn.dataset.clear;
    const project = $("#toolbar-title")?.textContent;
    if (!name || !project || project === "（未选择项目）") return;
    TF.confirm("确定清除「" + name + "」的标注吗？").then((yes) => {
      if (yes) htmx.ajax("DELETE", "/api/label/" + encodeURIComponent(project) + "/" + encodeURIComponent(name), { target: "body", swap: "none" });
    });
  }, true);
  // 批量清除：尊重当前筛选（meta 栏按钮，显示 clearable 数量）
  document.body.addEventListener("click", (e) => {
    const btn = e.target.closest("#btn-clear-filtered");
    if (!btn) return;
    if (btn.disabled) return;
    const chip = document.querySelector("#meta-bar .tf-chip");
    const hint = chip ? chip.textContent : "";
    // 从按钮文案提取数量，如 "清除已标注 (3)" -> 3；无则用 clearable
    let count = 0;
    const m = btn.textContent.match(/\((\d+)\)/);
    if (m) count = parseInt(m[1], 10);
    const msg = count
      ? "确定清除当前筛选结果中 " + count + " 张已标注/失败的标签吗？此操作不可恢复。"
      : "确定清除当前筛选结果中全部已标注/失败的标签吗？";
    TF.confirm(msg).then((yes) => {
      if (yes) htmx.ajax("POST", "/api/labels/clear", { target: "body", swap: "none" });
    });
  });

  // ---------- 再生按钮状态（委托：详情内容每次被 htmx 替换，直接绑定会丢失） ----------
  document.body.addEventListener("htmx:beforeRequest", (e) => {
    const btn = e.target && e.target.closest ? e.target.closest("#btn-regenerate") : null;
    if (!btn) return;
    btn.disabled = true;
    if (!btn.dataset.origHtml) btn.dataset.origHtml = btn.innerHTML;
    btn.innerHTML = '<svg class="tf-icon tf-icon--sm tf-spin" aria-hidden="true"><use href="/static/icons.svg#icon-loader"/></svg> 生成中…';
    const sp = $("#gen-spinner");
    if (sp) sp.hidden = false;
  });
  document.body.addEventListener("htmx:afterRequest", (e) => {
    const btn = e.target && e.target.closest ? e.target.closest("#btn-regenerate") : null;
    if (!btn) return;
    btn.disabled = false;
    if (btn.dataset.origHtml) { btn.innerHTML = btn.dataset.origHtml; delete btn.dataset.origHtml; }
    else btn.innerHTML = '<svg class="tf-icon tf-icon--sm" aria-hidden="true"><use href="/static/icons.svg#icon-refresh"/></svg> 重新生成';
    const sp = $("#gen-spinner");
    if (sp) sp.hidden = true;
  });

  // ---------- Lightbox ----------
  function openLightbox(src) {
    const lb = $("#lightbox");
    lb.innerHTML = '<img src="' + src + '"><div class="tf-lightbox-hint">点击任意处或按 Esc 关闭</div>';
    lb.hidden = false;
  }
  function closeLightbox() { $("#lightbox").hidden = true; }
  document.body.addEventListener("click", (e) => {
    const img = e.target.closest(".tf-detail-img[data-lightbox]");
    if (img) openLightbox(img.dataset.lightbox);
    else if (e.target.closest("#lightbox")) closeLightbox();
  });

  // ---------- 快捷键 ----------
  document.addEventListener("keydown", (e) => {
    const lb = $("#lightbox");
    if (e.key === "Escape") {
      if (lb && !lb.hidden) { closeLightbox(); return; }
      if (drawer && drawer.classList.contains("open")) { closeDetail(); return; }
    }
    if (drawer && drawer.classList.contains("open")) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target && e.target.id === "tag-text") {
        e.preventDefault();
        const ta = e.target;
        const url = ta.getAttribute("hx-post");
        if (url) htmx.ajax("POST", url, { source: ta, values: { text: ta.value }, target: "body", swap: "none" });
      } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        const dir = e.key === "ArrowLeft" ? "上一张" : "下一张";
        const btn = Array.from(drawer.querySelectorAll("button")).find((b) => b.textContent.includes(dir));
        if (btn) btn.click();
      }
    }
  });

  // ---------- 上传（点击 + 拖拽，逐文件 XHR） ----------
  const uploadInput = $("#upload-input");
  function sendUploads(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const project = $("#toolbar-title")?.textContent;
    if (!project || project === "（未选择项目）") { TF.toast("请先选择项目", "warning"); return; }
    const status = $("#upload-status");
    status.hidden = false;
    const total = files.length;
    let done = 0, ok = 0, fail = 0, renamed = 0;
    const finish = () => {
      if (done !== total) return;
      status.hidden = true;
      if (ok) TF.toast("已上传 " + ok + " 张" + (renamed ? "（" + renamed + " 张同名已自动改名）" : ""), "positive");
      if (fail) TF.toast(fail + " 张上传失败（类型不支持或写入出错）", "negative");
      refreshGrid();
    };
    files.forEach((f) => {
      const fd = new FormData();
      fd.append("files", f);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) status.textContent = "上传中… " + (done + 1) + "/" + total + "（" + Math.round((e.loaded / e.total) * 100) + "%）";
      };
      xhr.onload = () => {
        done++;
        if (xhr.status === 200) {
          try { const rr = JSON.parse(xhr.responseText); ok += rr.ok; fail += rr.fail; renamed += rr.renamed; }
          catch (_) { fail++; }
        } else { fail++; }
        finish();
      };
      xhr.onerror = () => { done++; fail++; finish(); };
      xhr.send(fd);
    });
  }
  $("#btn-upload")?.addEventListener("click", () => uploadInput && uploadInput.click());
  uploadInput?.addEventListener("change", () => { sendUploads(uploadInput.files); uploadInput.value = ""; });
  const mainEl = $("#main");
  if (mainEl) {
    ["dragover", "dragenter"].forEach((ev) => mainEl.addEventListener(ev, (e) => { e.preventDefault(); }));
    mainEl.addEventListener("drop", (e) => { e.preventDefault(); sendUploads(e.dataTransfer && e.dataTransfer.files); });
  }


  // ---------- 批量标注（SSE 进度） ----------
  let batchES = null;
  function showBatchCard(v) { const c = $("#batch-card"); if (c) c.hidden = !v; }
  function batchSetStatus(t) {
    const el = $("#batch-status");
    if (!el) return;
    // t 为纯文本或 HTML；统一按纯文本处理，自动补图标
    el.textContent = t;
  }
  function batchSetStatusHtml(html) {
    const el = $("#batch-status");
    if (!el) return;
    el.innerHTML = html;
  }
  function batchSetProgress(v) { const p = $("#batch-progress"); if (p) p.value = v; }
  function batchAppendLog(line) {
    const box = $("#batch-log");
    if (!box) return;
    box.hidden = false;
    const div = document.createElement("div");
    div.textContent = line;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }
  function batchFinish(d) {
    if (d.aborted) batchSetStatusHtml('<svg class="tf-icon tf-icon--sm" aria-hidden="true"><use href="/static/icons.svg#icon-stop"/></svg> 已终止（本次完成 ' + d.done + "/" + d.total + "）");
    else batchSetStatusHtml('<svg class="tf-icon tf-icon--sm" aria-hidden="true"><use href="/static/icons.svg#icon-check-circle"/></svg> 完成（成功 ' + d.ok + " · 失败 " + d.fail + "）");
    const stop = $("#btn-batch-stop"); if (stop) stop.disabled = true;
    const mp = $("#main-progress"); if (mp) mp.hidden = true;
    if (d.fail > 0) {
      const rb = $("#btn-retry-failed");
      if (rb) { rb.innerHTML = '<svg class="tf-icon tf-icon--sm" aria-hidden="true"><use href="/static/icons.svg#icon-refresh"/></svg> 重试失败 ' + d.fail; rb.hidden = false; }
    }
  }
  function startBatchSSE() {
    if (batchES) batchES.close();
    showBatchCard(true);
    batchSetProgress(0);
    const st = $("#batch-stats"); if (st) st.textContent = "";
    const lg = $("#batch-log"); if (lg) { lg.hidden = true; lg.innerHTML = ""; }
    const rb = $("#btn-retry-failed"); if (rb) rb.hidden = true;
    const stop = $("#btn-batch-stop"); if (stop) stop.disabled = false;
    batchSetStatusHtml('<svg class="tf-icon tf-icon--sm tf-spin" aria-hidden="true"><use href="/static/icons.svg#icon-loader"/></svg> 处理中…');
    const mp = $("#main-progress"); if (mp) { mp.hidden = false; mp.value = 0; }
    let done = false;
    batchES = new EventSource("/api/batch/events");
    batchES.addEventListener("progress", (e) => {
      const d = JSON.parse(e.data);
      batchSetProgress(d.total ? d.done / d.total : 0);
      const st = $("#batch-stats");
      if (st) st.textContent = "完成 " + d.done + "/" + d.total + " · 成功 " + d.ok + " · 失败 " + d.fail + " · " + Math.round(d.elapsed) + "s";
    });
    batchES.addEventListener("log", (e) => batchAppendLog(JSON.parse(e.data).text));
    batchES.addEventListener("done", (e) => {
      done = true;
      batchFinish(JSON.parse(e.data));
      batchES.close(); batchES = null;
      refreshGrid();
      document.dispatchEvent(new CustomEvent("tokensUpdated"));
    });
    batchES.onerror = () => {
      if (done) return;
      batchES.close(); batchES = null;
      batchFinish({ aborted: false, ok: 0, fail: 0, done: 0, total: 0 });
      refreshGrid();
      document.dispatchEvent(new CustomEvent("tokensUpdated"));
    };
  }
  document.addEventListener("batchStarted", startBatchSSE);
  $("#btn-batch")?.addEventListener("click", () => htmx.ajax("POST", "/api/batch/start", { swap: "none" }));
  $("#btn-batch-stop")?.addEventListener("click", () => fetch("/api/batch/stop", { method: "POST" }));
  $("#btn-retry-failed")?.addEventListener("click", () => htmx.ajax("POST", "/api/batch/retry", { swap: "none" }));
  $("#btn-collapse-batch")?.addEventListener("click", () => showBatchCard(false));
  $("#btn-log-toggle")?.addEventListener("click", () => { const l = $("#batch-log"); if (l) l.hidden = !l.hidden; });
  $("#btn-log-clear")?.addEventListener("click", () => { const l = $("#batch-log"); if (l) l.innerHTML = ""; });
  $("#btn-log-copy")?.addEventListener("click", () => {
    const lines = Array.from(document.querySelectorAll("#batch-log div")).map((d) => d.textContent).join("\n");
    if (!lines) { TF.toast("日志为空", "info"); return; }
    navigator.clipboard.writeText(lines).then(() => TF.toast("已复制日志到剪贴板")).catch(() => TF.toast("复制失败", "negative"));
  });

  // ---------- 导出 / 测试连接 / 试生成 ----------
  $("#btn-export")?.addEventListener("click", () => {
    const project = $("#toolbar-title")?.textContent;
    if (!project || project === "（未选择项目）") { TF.toast("请先选择项目", "warning"); return; }
    const a = document.createElement("a");
    a.href = "/api/export/" + encodeURIComponent(project);
    a.download = "";
    a.click();
  });
  $("#btn-test-conn")?.addEventListener("click", async () => {
    const btn = $("#btn-test-conn");
    const orig = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = '<svg class="tf-icon tf-icon--sm tf-spin" aria-hidden="true"><use href="/static/icons.svg#icon-loader"/></svg> 测试中…';
    TF.saveSetting("base_url", $("#base-url").value);
    TF.saveSetting("model", $("#model-name").value);
    TF.saveSetting("api_key", $("#api-key").value);
    try {
      const r = await fetch("/api/test-connection", { method: "POST" });
      const j = await r.json();
      TF.toast(j.detail || (j.ok ? "API 可用" : "连接失败"), j.ok ? "positive" : "negative", 9000);
    } catch (_) { TF.toast("测试请求失败", "negative"); }
    btn.disabled = false; btn.innerHTML = orig;
  });
  $("#btn-trial")?.addEventListener("click", () => htmx.ajax("POST", "/api/trial", { swap: "none" }));

  // ---------- 顶栏 Tokens ----------
  document.addEventListener("tokensUpdated", async () => {
    try {
      const r = await fetch("/api/status/tokens");
      const j = await r.json();
      const el = $("#header-tokens");
      if (el) el.innerHTML = '<svg class="tf-icon tf-icon--sm" aria-hidden="true"><use href="/static/icons.svg#icon-loader"/></svg> Tokens：' + (j.total || 0);
    } catch (_) {}
  });

  // ---------- 帮助 ----------
  $("#btn-help")?.addEventListener("click", () => openDialog("dlg-help"));


  // ---------- 设置表单：失焦/变更即存 ----------
  const settingBind = [
    ["#base-url", "base_url", "text", true],
    ["#model-name", "model", "text", false],
    ["#api-key", "api_key", "text", true],
    ["#tag-prefix", "tag_prefix", "text", false],
    ["#concurrency", "concurrency", "int", false],
    ["#character-name", "character_name", "text", false],
  ];
  settingBind.forEach(([sel, key, kind, debounce]) => {
    const el = $(sel);
    if (!el) return;
    const save = () => {
      let v = el.value;
      if (kind === "int") v = parseInt(v, 10) || 1;
      TF.saveSetting(key, v);
    };
    el.addEventListener("change", save);
    if (debounce) el.addEventListener("input", () => TF.saveSettingDebounced(key, el));
  });
  $$('input[name="prefix-mode"]').forEach((r) => r.addEventListener("change", () => TF.saveSetting("prefix_mode", r.value)));

  // 模型预设：回填 Base URL / 模型 / 该预设记忆的 Key，并从提供商拉取模型列表
  $("#model-preset")?.addEventListener("change", async () => {
    const name = $("#model-preset").value;
    const resp = await fetch("/api/settings/preset?name=" + encodeURIComponent(name));
    if (!resp.ok) return;
    const j = await resp.json();
    $("#base-url").value = j.base_url;
    $("#model-name").value = j.model;
    if (j.api_key) $("#api-key").value = j.api_key;
    await TF.saveSetting("base_url", j.base_url);
    await TF.saveSetting("model", j.model);
    if (j.api_key) await TF.saveSetting("api_key", j.api_key);
    else TF.toast("该预设未保存 API Key，可手动填写（会自动记住到该预设）", "info", 3500);
    fetchModels(j.base_url, $("#api-key").value, false);
  });

  // ---------- 模型列表（从提供商拉取，不缓存） ----------
  async function fetchModels(baseUrl, apiKey, silent) {
    const btn = $("#btn-refresh-models");
    if (btn) { btn.disabled = true; btn.innerHTML = '<svg class="tf-icon tf-icon--md tf-spin" aria-hidden="true"><use href="/static/icons.svg#icon-loader"/></svg>'; }
    try {
      const r = await fetch("/api/models", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: baseUrl || "", api_key: apiKey || "" })
      });
      const j = await r.json();
      const dl = $("#model-list");
      if (dl) {
        dl.innerHTML = "";
        (j.models || []).forEach((m) => {
          const op = document.createElement("option");
          op.value = m;
          dl.appendChild(op);
        });
      }
      if (r.ok) {
        if (!silent) {
          if (j.models && j.models.length) TF.toast("已从提供商获取 " + j.models.length + " 个模型", "positive", 2500);
          else TF.toast("该提供商未提供模型列表，可手动输入模型名", "info", 4000);
        }
      } else {
        TF.toast(j.error || "获取模型列表失败", "negative", 5000);
      }
    } catch (_) {
      if (!silent) TF.toast("获取模型列表失败", "negative");
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<svg class="tf-icon tf-icon--md" aria-hidden="true"><use href="/static/icons.svg#icon-refresh"/></svg>'; }
    }
  }
  $("#btn-refresh-models")?.addEventListener("click", () => fetchModels($("#base-url").value, $("#api-key").value, false));
  $("#base-url")?.addEventListener("change", () => fetchModels($("#base-url").value, $("#api-key").value, false));
  if ($("#base-url") && $("#base-url").value) fetchModels($("#base-url").value, $("#api-key").value, true);

  // ---------- 模型预设管理（新增 / 编辑 / 删除，内置与自定义统一） ----------
  async function reloadPresets(selectTo) {
    const r = await fetch("/api/settings/presets");
    if (!r.ok) return;
    const j = await r.json();
    const sel = $("#model-preset");
    if (!sel) return;
    const cur = selectTo || sel.value;
    sel.innerHTML = "";
    Object.keys(j.presets || {}).forEach((name) => {
      const op = document.createElement("option");
      op.value = name;
      op.textContent = name;
      sel.appendChild(op);
    });
    if (cur && j.presets[cur]) sel.value = cur;
  }
  async function openPresetDialog(mode) {
    const dlg = $("#dlg-preset");
    if (!dlg) return;
    $("#preset-name").value = "";
    $("#preset-base-url").value = "";
    $("#preset-model").value = "";
    if (mode === "edit") {
      const name = $("#model-preset").value;
      if (!name) { TF.toast("请先选择一个预设", "warning"); return; }
      $("#dlg-preset-title").textContent = "编辑预设";
      const r = await fetch("/api/settings/preset?name=" + encodeURIComponent(name));
      if (!r.ok) { TF.toast("读取预设失败", "negative"); return; }
      const j = await r.json();
      $("#preset-name").value = j.name || name;
      $("#preset-base-url").value = j.base_url || "";
      $("#preset-model").value = j.model || "";
    } else {
      $("#dlg-preset-title").textContent = "新增模型预设";
    }
    if (!dlg.open) dlg.showModal();
  }
  $("#btn-add-preset")?.addEventListener("click", () => openPresetDialog("create"));
  $("#btn-edit-preset")?.addEventListener("click", () => openPresetDialog("edit"));
  $("#btn-del-preset")?.addEventListener("click", async () => {
    const name = $("#model-preset").value;
    if (!name) { TF.toast("请先选择一个预设", "warning"); return; }
    if (!(await TF.confirm("确定删除预设「" + name + "」吗？其记忆的 API Key 也会一并清除。"))) return;
    const r = await fetch("/api/settings/presets?name=" + encodeURIComponent(name), { method: "DELETE" });
    const j = await r.json();
    if (!r.ok) { TF.toast(j.error || "删除失败", "negative"); return; }
    await reloadPresets("");
    TF.toast("已删除预设「" + name + "」", "positive");
  });
  $("#btn-save-preset")?.addEventListener("click", async () => {
    const name = $("#preset-name").value.trim();
    if (!name) { TF.toast("预设名不能为空", "negative"); return; }
    const editing = $("#dlg-preset-title").textContent === "编辑预设";
    const orig = editing ? $("#model-preset").value : "";
    const r = await fetch("/api/settings/presets", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, base_url: $("#preset-base-url").value, model: $("#preset-model").value, orig_name: orig })
    });
    const j = await r.json();
    if (!r.ok) { TF.toast(j.error || "保存失败", "negative"); return; }
    $("#dlg-preset").close();
    // 应用该预设到当前配置
    $("#base-url").value = $("#preset-base-url").value;
    $("#model-name").value = $("#preset-model").value;
    await TF.saveSetting("base_url", $("#base-url").value);
    await TF.saveSetting("model", $("#model-name").value);
    await reloadPresets(name);
    $("#model-preset").value = name;
    fetchModels($("#base-url").value, $("#api-key").value, true);
    TF.toast(editing ? "已保存预设「" + name + "」" : "已新增预设「" + name + "」", "positive");
  });
  // API Key 变更：记住到当前预设（若属于内置预设）
  $("#api-key")?.addEventListener("change", async () => {
    const key = $("#api-key").value.trim();
    const preset = $("#model-preset").value;
    if (key && preset) {
      await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ preset_keys: { [preset]: key } }) });
    }
  });

  // 提示词预设 / 文本框 / 恢复默认
  $("#prompt-preset")?.addEventListener("change", async () => {
    const r2 = await fetch("/api/settings/prompt", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt_preset: $("#prompt-preset").value }) });
    const j = await r2.json();
    $("#prompt-text").value = j.system_prompt || "";
  });
  $("#prompt-text")?.addEventListener("input", (e) => {
    const sel = $("#prompt-preset");
    if (sel && sel.value !== "custom") sel.value = "custom";
    TF.saveSettingDebounced("system_prompt", e.target);
  });
  $("#btn-restore-prompt")?.addEventListener("click", async () => {
    const r2 = await fetch("/api/settings/prompt/default", { method: "POST" });
    const j = await r2.json();
    $("#prompt-text").value = j.system_prompt;
    $("#prompt-preset").value = j.prompt_preset;
    TF.toast("已恢复为「英文 · 短标签 · 通用」默认提示词");
  });
  // 角色名变更：若当前预设为角色 LoRA，则用新名字重新解析提示词
  $("#character-name")?.addEventListener("change", async () => {
    const preset = $("#prompt-preset").value;
    if (preset && preset !== "custom") {
      const r2 = await fetch("/api/settings/prompt", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt_preset: preset }) });
      const j = await r2.json();
      $("#prompt-text").value = j.system_prompt || "";
    }
  });

})();