import { fetchJson } from "./client";

export interface ServiceSummary {
  id: string;
  label: string;
  description?: string;
  running?: boolean;
  status?: string;
  health_url?: string;
  cwd?: string;
  command?: string;
  startup_wait_sec?: number;
  pid?: number | string | null;
  port?: number | string | null;
  health?: string | null;
}

export interface ServicesResponse {
  services: ServiceSummary[];
  resources?: unknown;
}

export async function loadServices(): Promise<ServicesResponse> {
  return fetchJson<ServicesResponse>("/api/services");
}

export async function startService(serviceId: string) {
  return fetchJson(`/api/services/${encodeURIComponent(serviceId)}/start`, { method: "POST" });
}

export async function stopService(serviceId: string) {
  return fetchJson(`/api/services/${encodeURIComponent(serviceId)}/stop`, { method: "POST" });
}

export async function startAllServices() {
  return fetchJson("/api/services/start-all", { method: "POST" });
}

export async function stopAllServices() {
  return fetchJson("/api/services/stop-all", { method: "POST" });
}

export async function fetchServiceLogs(serviceId: string) {
  const data = await fetchJson<{ logs?: string }>(`/api/services/${encodeURIComponent(serviceId)}/logs?max_bytes=50000`);
  return data.logs || "";
}

export async function clearServiceLogs(serviceId: string) {
  return fetchJson(`/api/services/${encodeURIComponent(serviceId)}/logs`, { method: "DELETE" });
}

export async function clearAllServiceLogs() {
  return fetchJson("/api/services/logs", { method: "DELETE" });
}

export async function updateServiceConfig(serviceId: string, payload: Partial<ServiceSummary>) {
  return fetchJson(`/api/services/${encodeURIComponent(serviceId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function loadSystemResources() {
  return fetchJson<Record<string, unknown>>("/api/system/resources");
}
