import { describe, expect, it } from "vitest";
import { appApprovalScope } from "./ComputerPanel";
import type { WorkflowApproval } from "./types";

const approval = (args: unknown): WorkflowApproval => ({ approval_id: "test", tool_name: "computer_session_start", permission: "computer_control", reason: "Test", arguments: JSON.stringify(args), status: "pending", expires_at: 0, created_at: 0 });
const scope = { app: { name: "Fixture", pid: 123, app_id: "pid:123", bundle_id: "test.fixture", path: "/Fixture.app" }, access: "control", ttl_seconds: 600 };

describe("application approval display", () => {
  it("shows the exact process identity, scope and duration", () => {
    expect(appApprovalScope(approval(scope))).toEqual({ name: "Fixture", identity: "test.fixture · PID 123", path: "/Fixture.app", access: "control", seconds: 600 });
  });
  it("does not approve malformed or unbounded scope", () => {
    for (const invalid of [null, {}, { ...scope, ttl_seconds: -1 }, { ...scope, ttl_seconds: 1801 }, { ...scope, ttl_seconds: 45.5 }, { ...scope, access: "all" }, { ...scope, app: { name: "Fixture" } }]) {
      expect(appApprovalScope(approval(invalid))).toBeNull();
    }
    expect(appApprovalScope({ ...approval(scope), arguments: "bad json" })).toBeNull();
  });
});
