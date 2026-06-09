/* ============================================================
   main.js — SPA router (single HTML, all pages inline)
   BranchWhisper · Precision Console
   ============================================================ */

import { renderIcons } from "./utils.js";

let currentPage = "dashboard";
let dashboardInitialized = false;
let servicesInitialized = false;
let integrationsInitialized = false;
let memoryInitialized = false;
let currentLeave = null;

/* ---- SPA navigation ---- */

document.addEventListener("DOMContentLoaded", async () => {
  renderIcons();
  setupNav();
  loadTheme();
  registerServiceWorker();

  const initial = pageFromHash() || document.body.dataset.page || "dashboard";
  await switchPage(initial, false);
});

function setupNav() {
  document.querySelectorAll("#mainNav a[data-nav]").forEach((link) => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      const page = link.dataset.nav;
      if (page && page !== currentPage) switchPage(page);
    });
  });

  document.querySelector(".brand[data-nav]")?.addEventListener("click", (e) => {
    e.preventDefault();
    if (currentPage !== "dashboard") switchPage("dashboard");
  });

  window.addEventListener("popstate", (e) => {
    const page = e.state?.page || pageFromHash() || "dashboard";
    if (page !== currentPage) switchPage(page, false);
  });

  window.addEventListener("hashchange", () => {
    const page = pageFromHash();
    if (page && page !== currentPage) switchPage(page, false);
  });
}

async function switchPage(page, pushState = true) {
  page = normalizePage(page);
  if (currentLeave) {
    currentLeave();
    currentLeave = null;
  }

  // hide all
  document.querySelectorAll(".page-view").forEach((p) => { p.style.display = "none"; });

  // show target
  const view = document.getElementById(`page-${page}`);
  if (view) {
    view.style.display = "";
    // reset animation
    view.style.animation = "none";
    view.offsetHeight; // trigger reflow
    view.style.animation = "";
    window.scrollTo(0, 0);
  }

  // nav active
  document.querySelectorAll("#mainNav a[data-nav]").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === page);
  });

  document.body.dataset.page = page;

  // memory button only on dashboard
  currentPage = page;
  if (pushState) history.pushState({ page }, "", `#${page}`);

  // init page module — always re-init settings (it's lightweight and idempotent)
  // dashboard and services are heavy (websocket, polling) so only init once
  try {
    if (page === "dashboard") {
      const dashboardModule = await import("./ui-dashboard.js");
      if (!dashboardInitialized) {
        await dashboardModule.initDashboard();
        dashboardInitialized = true;
      } else {
        await dashboardModule.enterDashboard?.();
      }
    } else if (page === "services") {
      const servicesModule = await import("./ui-services.js");
      if (!servicesInitialized) {
        servicesModule.initServices();
        servicesInitialized = true;
      }
      servicesModule.enterServices?.();
      currentLeave = servicesModule.leaveServices || null;
    } else if (page === "integrations") {
      const integrationsModule = await import("./ui-integrations.js");
      if (!integrationsInitialized) {
        integrationsModule.initIntegrations();
        integrationsInitialized = true;
      }
      integrationsModule.enterIntegrations?.();
      currentLeave = integrationsModule.leaveIntegrations || null;
    } else if (page === "memory") {
      const memoryModule = await import("./ui-memory.js");
      if (!memoryInitialized) {
        await memoryModule.initMemoryPage();
        memoryInitialized = true;
      } else {
        await memoryModule.enterMemoryPage?.();
      }
    } else if (page === "settings") {
      const settingsModule = await import("./ui-settings.js");
      await settingsModule.initSettings();
      currentLeave = settingsModule.leaveSettings || null;
    }
  } catch (e) {
    console.error(`Failed to init page ${page}:`, e);
    // reset latch so user can retry
    if (page === "services") servicesInitialized = false;
    if (page === "integrations") integrationsInitialized = false;
    if (page === "dashboard") dashboardInitialized = false;
  }
}

function normalizePage(page) {
  return ["dashboard", "services", "integrations", "memory", "settings"].includes(page) ? page : "dashboard";
}

function pageFromHash() {
  const hash = location.hash.replace(/^#/, "");
  if (!hash) return "";
  if (hash.startsWith("logs-")) return "services";
  return normalizePage(hash.split("-")[0]);
}

/* ---- theme ---- */

const THEME_KEY = "branchwhisper.theme";
const LEGACY_THEME_KEY = "lovechoice.theme";

function readTheme() {
  const current = localStorage.getItem(THEME_KEY);
  if (current) return current;
  const legacy = localStorage.getItem(LEGACY_THEME_KEY);
  if (legacy) {
    localStorage.setItem(THEME_KEY, legacy);
    return legacy;
  }
  return "dark";
}

export function loadTheme() {
  const saved = readTheme();
  applyTheme(saved);
}

function applyTheme(theme) {
  if (theme === "light") {
    document.documentElement.classList.add("theme-light");
  } else {
    document.documentElement.classList.remove("theme-light");
  }
  // sync toggle buttons if present
  document.querySelectorAll("#themeToggle button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.theme === theme);
  });
}

export function setTheme(theme) {
  applyTheme(theme);
  localStorage.setItem(THEME_KEY, theme);
  // update toggle buttons
  document.querySelectorAll("#themeToggle button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.theme === theme);
  });
}

// 暴露主题 API 给其他模块使用
window.__branchwhisper = {
  setTheme,
  loadTheme,
  getTheme: readTheme,
};
window.__lovechoice = window.__branchwhisper;

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  if (location.protocol !== "https:" && location.hostname !== "localhost" && location.hostname !== "127.0.0.1") return;
  navigator.serviceWorker.register("/static/sw.js").catch(() => {});
}
