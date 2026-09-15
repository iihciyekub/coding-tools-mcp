import type { WorkspaceProfile } from "./types";

export function normalizeDomain(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .split(/[/?#]/, 1)[0]
    .replace(/^\.+|\.+$/g, "");
}

export function recommendedPublicUrl(workspaceName: string, domain: string): string {
  const normalizedDomain = normalizeDomain(domain);
  if (!normalizedDomain) return "";
  const workspaceSlug = workspaceName
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "workspace";
  const hostname = workspaceSlug.endsWith("-mcp") ? workspaceSlug : `${workspaceSlug}-mcp`;
  return `https://${hostname}.${normalizedDomain}`;
}

export function publicEndpoint(profile: WorkspaceProfile, resolvedUrl = ""): string {
  let base = resolvedUrl.trim();
  if (!base && profile.tunnel.type === "cloudflare" && profile.tunnel.cloudflare_mode === "named") {
    base = profile.tunnel.public_url.trim();
  }
  if (!base && profile.tunnel.type === "frp" && profile.tunnel.frp_server && profile.tunnel.frp_subdomain) {
    base = `https://${profile.tunnel.frp_subdomain}.${profile.tunnel.frp_server}`;
  }
  return base ? `${base.replace(/\/$/, "")}/mcp` : "";
}

export function workspaceHue(position: number): number {
  const ordinal = Math.max(1, Math.trunc(position));
  const calmHues = [210, 170, 250, 190, 275, 225, 160, 265, 200, 240, 180, 285];
  return calmHues[(ordinal - 1) % calmHues.length];
}

export function frpSnippet(profile: WorkspaceProfile): string {
  const name = profile.name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^[-_]+|[-_]+$/g, "") || "workspace";
  return [
    "[[proxies]]",
    `name = ${JSON.stringify(`${name}-mcp`)}`,
    'type = "http"',
    'localIP = "127.0.0.1"',
    `localPort = ${profile.runtime.local_port}`,
    `subdomain = ${JSON.stringify(profile.tunnel.frp_subdomain)}`,
  ].join("\n");
}
