import { createRouter, createWebHistory } from "vue-router";
import AssetsPage from "@/pages/AssetsPage.vue";
import DashboardPage from "@/pages/DashboardPage.vue";
import DiagnosticsPage from "@/pages/DiagnosticsPage.vue";
import IntegrationsPage from "@/pages/IntegrationsPage.vue";
import MemoryPage from "@/pages/MemoryPage.vue";
import ServicesPage from "@/pages/ServicesPage.vue";
import SettingsPage from "@/pages/SettingsPage.vue";

export const router = createRouter({
  history: createWebHistory("/app/"),
  routes: [
    { path: "/", name: "dashboard", component: DashboardPage },
    { path: "/services", name: "services", component: ServicesPage },
    { path: "/integrations", name: "integrations", component: IntegrationsPage },
    { path: "/diagnostics", name: "diagnostics", component: DiagnosticsPage },
    { path: "/memory", name: "memory", component: MemoryPage },
    { path: "/assets", name: "assets", component: AssetsPage },
    { path: "/settings", name: "settings", component: SettingsPage },
  ],
});
