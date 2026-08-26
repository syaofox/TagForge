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
})();
