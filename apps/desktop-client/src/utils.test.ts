import { describe, expect, it } from "vitest";
import type { WorkspaceProfile } from "./types";
import { frpSnippet, normalizeDomain, publicEndpoint, recommendedPublicUrl, workspaceHue } from "./utils";

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
  auth: { type: "oauth", oauth_password: "pw", oauth_token_secret: "secret", bearer_token: "token" },
  runtime: { computer_enabled: false, local_port: 28767, permission_mode: "trusted", file_access_scope: "workspace", allowed_paths: [], environment_variables: [] },
};

describe("desktop URL helpers", () => {
  it("builds the public MCP endpoint", () => {
    expect(publicEndpoint(profile)).toBe("https://tax.example.com/mcp");
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
