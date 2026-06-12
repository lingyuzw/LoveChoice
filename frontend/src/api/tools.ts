import { fetchJson } from "./client";

export type ToolProviderConfig = Record<string, any>;

export interface ToolResolveResult {
  tool_call: Record<string, any> | null;
  result: Record<string, any> | null;
  direct_answer?: string;
}

export async function loadToolsConfig() {
  return fetchJson<ToolProviderConfig>("/api/tools");
}

export async function saveToolsConfig(config: ToolProviderConfig) {
  return fetchJson<ToolProviderConfig>("/api/tools", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export async function resolveTool(text: string) {
  return fetchJson<ToolResolveResult>("/api/tools/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

export async function testTool(tool: string, argumentsData: Record<string, unknown> = {}) {
  return fetchJson<{ id: string; arguments: Record<string, unknown>; elapsed_ms: number; result: Record<string, any> }>(
    "/api/tools/test",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool, arguments: argumentsData }),
    },
  );
}
