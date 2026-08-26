/* TagForge 前端脚本
 * Step2 骨架：主题 / Tab / Toast / 通用设置保存
 * Step4+ 扩展：上传拖拽、详情抽屉、lightbox、快捷键、批量 SSE
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
  $("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

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
  function refreshGrid() {
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

  // ---------- 详情抽屉 ----------
  const drawer = $("#detail-drawer");
  function closeDetail() { if (drawer) { drawer.classList.remove("open"); drawer.dataset.name = ""; } }
  $("#btn-detail-close")?.addEventListener?.("click", closeDetail);
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

  // ---------- 再生按钮状态 ----------
  const genBtn = $("#btn-regenerate");
  if (genBtn) {
    genBtn.addEventListener("htmx:beforeRequest", () => { genBtn.disabled = true; genBtn.textContent = "生成中…"; $("#gen-spinner")?.toggleAttribute("hidden", false); });
    genBtn.addEventListener("htmx:afterRequest", () => { genBtn.disabled = false; genBtn.textContent = "重新生成"; $("#gen-spinner")?.toggleAttribute("hidden", true); });
  }

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

})();