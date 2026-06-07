/* ============================================================
   main.js — SPA router (single HTML, all pages inline)
   LoveChoice Voice Console · Precision Console
   ============================================================ */

import { renderIcons } from "./utils.js";

let currentPage = "dashboard";

/* ---- SPA navigation ---- */

document.addEventListener("DOMContentLoaded", async () => {
  renderIcons();
  setupNav();

  // start with the page from data-page attribute
  const initial = document.body.dataset.page || "dashboard";
  await switchPage(initial);
});

function setupNav() {
  document.querySelectorAll("#mainNav a[data-nav]").forEach((link) => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      const page = link.dataset.nav;
      if (page && page !== currentPage) switchPage(page);
    });
  });

  // brand click → dashboard
  document.querySelector(".brand[data-nav]")?.addEventListener("click", (e) => {
    e.preventDefault();
    if (currentPage !== "dashboard") switchPage("dashboard");
  });

  // use history.pushState for back/forward
  window.addEventListener("popstate", (e) => {
    const page = e.state?.page || "dashboard";
    if (page !== currentPage) switchPage(page, false);
  });
}

async function switchPage(page, pushState = true) {
  // hide all pages
  document.querySelectorAll(".page-view").forEach((p) => { p.style.display = "none"; });

  // show target
  const view = document.getElementById(`page-${page}`);
  if (view) view.style.display = "";

  // update nav active
  document.querySelectorAll("#mainNav a[data-nav]").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === page);
  });

  // update body data-page
  document.body.dataset.page = page;

  // show/hide memory button (dashboard only)
  const memBtn = document.getElementById("memoryTriggerBtn");
  if (memBtn) memBtn.style.display = page === "dashboard" ? "" : "none";

  currentPage = page;

  if (pushState) {
    history.pushState({ page }, "", `#${page}`);
  }

  // init page module dynamically
  try {
    if (page === "dashboard") {
      const { initDashboard } = await import("./ui-dashboard.js");
      await initDashboard();
    } else if (page === "services") {
      const { initServices } = await import("./ui-services.js");
      initServices();
    } else if (page === "settings") {
      const { initSettings } = await import("./ui-settings.js");
      await initSettings();
    }
  } catch (e) {
    console.error(`Failed to init page ${page}:`, e);
  }
}
