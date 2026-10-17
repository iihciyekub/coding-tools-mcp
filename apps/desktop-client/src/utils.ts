import type { AuthConfig, WorkspaceProfile } from "./types";

export function authorizationValidity(auth: AuthConfig, language: "en" | "zh-CN"): string {
  const seconds = auth.type === "bearer" ? 0 : auth.oauth_token_ttl_seconds;
  if (seconds === 0) return language === "zh-CN" ? "不限时" : "No expiration";
  const units = [[86400, "day"], [3600, "hour"], [60, "minute"], [1, "second"]] as const;
  const [divisor, unit] = units.find(([size]) => seconds % size === 0)!;
  return new Intl.NumberFormat(language, { style: "unit", unit, unitDisplay: "long" }).format(seconds / divisor);
}

export function normalizePublicOrigin(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, "");
  try {
    const url = new URL(trimmed);
    if (url.protocol === "https:" && !url.username && !url.password
      && url.pathname === "/mcp" && !trimmed.includes("?") && !trimmed.includes("#")) {
      return url.origin;
    }
  } catch { /* Leave invalid addresses for the server's validation. */ }
  return trimmed;
}

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
