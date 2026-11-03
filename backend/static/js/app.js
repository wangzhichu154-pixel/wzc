"use strict";

// Progressive enhancement only. Forms and server navigation work without JS.
document.body.classList.add("js-enabled");

const navigationToggle = document.querySelector(".nav-toggle");
const navigation = document.getElementById("primary-navigation");

function closeNavigation(restoreFocus = false) {
  document.body.removeAttribute("data-navigation-open");
  navigationToggle?.setAttribute("aria-expanded", "false");
  navigationToggle?.setAttribute("aria-label", "打开导航");
  if (restoreFocus) navigationToggle?.focus();
}

navigationToggle?.addEventListener("click", () => {
  const open = navigationToggle.getAttribute("aria-expanded") !== "true";
  document.body.toggleAttribute("data-navigation-open", open);
  if (open) document.body.dataset.navigationOpen = "true";
  navigationToggle.setAttribute("aria-expanded", String(open));
  navigationToggle.setAttribute("aria-label", open ? "关闭导航" : "打开导航");
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.hasAttribute("data-navigation-open")) {
    closeNavigation(true);
  }
});

document.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) return;
  if (document.body.hasAttribute("data-navigation-open") &&
      !navigation?.contains(event.target) && !navigationToggle?.contains(event.target)) {
    closeNavigation();
  }
  const dismiss = event.target.closest("[data-dismiss-alert]");
  if (dismiss) dismiss.closest(".alert")?.remove();
  if (event.target.closest("[data-reload]")) window.location.reload();
});

for (const link of document.querySelectorAll("[data-nav-path]")) {
  const prefix = link.dataset.navPath;
  if ((prefix === "/" && window.location.pathname === "/") ||
      (prefix !== "/" && window.location.pathname.startsWith(prefix))) {
    link.setAttribute("aria-current", "page");
  }
}

for (const form of document.querySelectorAll("form[data-prevent-double-submit], form[data-confirm]")) {
  form.addEventListener("submit", (event) => {
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    if (!form.hasAttribute("data-prevent-double-submit")) return;
    if (form.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = "true";
    form.setAttribute("aria-busy", "true");
    form.classList.add("is-submitting");
  });
}

// Back/forward cache must not leave a valid form locked after navigation.
window.addEventListener("pageshow", () => {
  for (const form of document.querySelectorAll("form[data-submitting]")) {
    delete form.dataset.submitting;
    form.removeAttribute("aria-busy");
    form.classList.remove("is-submitting");
  }
});

// Bounded, same-origin polling. A server-rendered page remains usable when JS,
// the network or the session is unavailable; no client value changes job state.
for (const panel of document.querySelectorAll("[data-job-poll]")) {
  const message = panel.querySelector("[data-poll-message]");
  const statusLabel = document.querySelector("[data-job-status]");
  const labels = {queued: "等待执行", running: "正在执行", succeeded: "已完成", failed: "失败", canceled: "已取消"};
  const terminal = new Set(["succeeded", "failed", "canceled"]);
  let tries = 0;
  let failures = 0;
  let timer;
  let stopped = false;
  let controller;
  let endpoint;
  try {
    endpoint = new URL(panel.dataset.jobPoll, window.location.href);
    if (endpoint.origin !== window.location.origin) throw new Error("Invalid polling origin");
  } catch {
    if (message) message.textContent = "无法自动更新任务状态，请使用刷新按钮。";
    continue;
  }
  async function poll() {
    if (stopped) return;
    if (tries >= 120) {
      if (message) message.textContent = "自动更新已暂停，请手动刷新以查看最新状态。";
      stopped = true;
      return;
    }
    if (document.hidden) {
      timer = window.setTimeout(poll, 3000);
      return;
    }
    tries += 1;
    controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(endpoint.href, {credentials: "same-origin", headers: {Accept: "application/json"}, cache: "no-store", signal: controller.signal});
      if ([401, 403, 404].includes(response.status)) {
        if (message) message.textContent = "无法继续读取任务。请检查登录状态和访问权限，再刷新页面。";
        stopped = true;
        return;
      }
      if (!response.ok) throw new Error("Status unavailable");
      const payload = await response.json();
      const status = payload?.data?.status;
      if (payload.success !== true || !Object.hasOwn(labels, status)) throw new Error("Invalid status response");
      failures = 0;
      if (statusLabel) {
        statusLabel.textContent = labels[status];
        statusLabel.className = `badge badge-${status}`;
      }
      if (terminal.has(status)) {
        stopped = true;
        window.location.reload();
        return;
      }
      if (message) message.textContent = "状态已更新。你可以离开页面，任务仍在后台执行。";
    } catch {
      if (stopped) return;
      failures += 1;
      if (message) message.textContent = "暂时无法更新任务状态，正在自动重试。任务实际状态以服务器记录为准。";
    } finally {
      window.clearTimeout(timeout);
      if (!stopped) timer = window.setTimeout(poll, Math.min(30000, 3000 * 2 ** Math.min(failures, 4)));
    }
  }
  timer = window.setTimeout(poll, 3000);
  window.addEventListener("pagehide", () => {
    stopped = true;
    window.clearTimeout(timer);
    controller?.abort();
  });
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    stopped = false;
    tries = 0;
    failures = 0;
    window.clearTimeout(timer);
    timer = window.setTimeout(poll, 3000);
  });
}
