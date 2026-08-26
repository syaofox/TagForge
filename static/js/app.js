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

})();