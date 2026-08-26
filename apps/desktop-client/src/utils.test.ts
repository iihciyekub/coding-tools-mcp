import { describe, expect, it } from "vitest";
import type { WorkspaceProfile } from "./types";
import { frpSnippet, publicEndpoint } from "./utils";

const profile: WorkspaceProfile = {
  id: "a".repeat(32),
  name: "Tax Review",
  path: "/work/tax",
  tunnel: {
    type: "frp",
    public_url: "",
    frp_server: "example.com",
    frp_subdomain: "tax",
    cloudflare_mode: "quick",
    cloudflare_token: "",
  },
  auth: { type: "oauth", oauth_password: "pw", oauth_token_secret: "secret", bearer_token: "token" },
  runtime: { local_port: 28767, permission_mode: "trusted" },
};

describe("desktop URL helpers", () => {
  it("builds the public MCP endpoint", () => {
    expect(publicEndpoint(profile)).toBe("https://tax.example.com/mcp");
  });

  it("builds a safe FRP snippet", () => {
    expect(frpSnippet(profile)).toContain('name = "tax-review-mcp"');
    expect(frpSnippet(profile)).toContain("localPort = 28767");
  });
});
