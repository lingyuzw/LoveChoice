/* ============================================================
   main.js — Application entry point (page routing)
   LoveChoice Voice Console
   ============================================================ */

import { renderIcons } from "./utils.js";

const page = document.body.dataset.page || "dashboard";

document.addEventListener("DOMContentLoaded", async () => {
  renderIcons();

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
});
