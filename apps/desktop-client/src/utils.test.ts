import { describe, expect, it } from "vitest";
import type { WorkspaceProfile } from "./types";
import { authorizationValidity, frpSnippet, normalizeDomain, normalizePublicOrigin, publicEndpoint, recommendedPublicUrl, workspaceHue } from "./utils";

const profile: WorkspaceProfile = {
  id: "a".repeat(32),
  name: "Tax Review",
  path: "/work/tax",
  tunnel: {
    type: "frp",
    domain: "",
    public_url: "",
    frp_server: "example.com",
    frp_subdomain: "tax",
    cloudflare_mode: "quick",
    cloudflare_token: "",
  },
  auth: { type: "oauth", oauth_password: "pw", oauth_token_secret: "secret", oauth_token_ttl_seconds: 0, bearer_token: "token" },
  runtime: { server_name_prefix: "www", local_port: 28767, permission_mode: "trusted", file_access_scope: "workspace", allowed_paths: [], default_search_path: "", environment_variables: [] },
};

describe("authorization validity", () => {
  it("shows unlimited authorization in both languages", () => {
    expect(authorizationValidity(profile.auth, "zh-CN")).toBe("不限时");
    expect(authorizationValidity(profile.auth, "en")).toBe("No expiration");
  });

  it("shows the configured duration without rounding it", () => {
    expect(authorizationValidity({ ...profile.auth, oauth_token_ttl_seconds: 604800 }, "en")).toBe("7 days");
    expect(authorizationValidity({ ...profile.auth, oauth_token_ttl_seconds: 86400 }, "zh-CN")).toBe("1天");
    expect(authorizationValidity({ ...profile.auth, oauth_token_ttl_seconds: 5400 }, "en")).toBe("90 minutes");
    expect(authorizationValidity({ ...profile.auth, oauth_token_ttl_seconds: 61 }, "en")).toBe("61 seconds");
  });

  it("keeps static bearer authorization unlimited", () => {
    expect(authorizationValidity({ ...profile.auth, type: "bearer", oauth_token_ttl_seconds: 86400 }, "en")).toBe("No expiration");
  });
});

describe("desktop URL helpers", () => {
  it("builds the public MCP endpoint", () => {
    expect(publicEndpoint(profile)).toBe("https://tax.example.com/mcp");
  });

  it("accepts a pasted MCP endpoint without duplicating its path", () => {
    const origin = normalizePublicOrigin(" https://mcp-dev.example.com/mcp/ ");
    expect(origin).toBe("https://mcp-dev.example.com");
    const named: WorkspaceProfile = { ...profile, tunnel: { ...profile.tunnel, type: "cloudflare", cloudflare_mode: "named", public_url: origin } };
    expect(publicEndpoint(named)).toBe("https://mcp-dev.example.com/mcp");
    expect(normalizePublicOrigin("https://mcp-dev.example.com/")).toBe(origin);
  });

  it("keeps unsupported endpoint inputs for origin validation", () => {
    for (const invalid of ["http://example.com/mcp", "https://user:pass@example.com/mcp", "https://example.com/other/mcp", "https://example.com/mcp?project=one", "https://example.com/mcp#section", "not-a-url"]) {
      expect(normalizePublicOrigin(invalid)).toBe(invalid);
    }
  });

  it("builds a safe FRP snippet", () => {
    expect(frpSnippet(profile)).toContain('name = "tax-review-mcp"');
    expect(frpSnippet(profile)).toContain("localPort = 28767");
  });

  it("normalizes a domain and recommends a workspace-specific public URL", () => {
    expect(normalizeDomain(" HTTPS://Example.COM/path ")).toBe("example.com");
    expect(recommendedPublicUrl("Tax Review", "example.com")).toBe("https://tax-review-mcp.example.com");
  });

  it("gives neighboring workspaces distinct non-warning background hues", () => {
    const hues = Array.from({ length: 12 }, (_, index) => workspaceHue(index + 1));
    expect(new Set(hues).size).toBe(hues.length);
    expect(hues.every((hue) => hue >= 150 && hue <= 290)).toBe(true);
    expect(workspaceHue(1)).toBe(210);
  });
});
