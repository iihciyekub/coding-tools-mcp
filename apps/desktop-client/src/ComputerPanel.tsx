import { api } from "./api";
import type { ComputerSession, WorkflowApproval } from "./types";

type Props = {
  enabled: boolean;
  running: boolean;
  approvals: WorkflowApproval[];
  sessions: ComputerSession[];
  t: (text: string) => string;
  onEnabled: (enabled: boolean) => Promise<void>;
  onDecision: (approvalId: string, approved: boolean) => Promise<void>;
  onStop: (sessionId: string) => Promise<void>;
  onError: (message: string) => void;
};

export function appApprovalScope(approval: WorkflowApproval): { name: string; identity: string; path: string; access: string; seconds: number } | null {
  try {
    const args = JSON.parse(approval.arguments);
    if (approval.tool_name !== "computer_session_start" || typeof args.app?.name !== "string" || !args.app.name || !Number.isInteger(args.app.pid) || typeof args.app.path !== "string" || !["observe", "control"].includes(args.access) || !Number.isInteger(args.ttl_seconds) || args.ttl_seconds < 30 || args.ttl_seconds > 1800) return null;
    return { name: args.app.name, identity: `${args.app.bundle_id || args.app.app_id} · PID ${args.app.pid}`, path: args.app.path, access: args.access, seconds: args.ttl_seconds };
  } catch { return null; }
}

export function ComputerPanel({ enabled, running, approvals, sessions, t, onEnabled, onDecision, onStop, onError }: Props) {
  const supported = navigator.platform.includes("Mac");
  const setup = async (permission: "accessibility" | "screen_recording") => {
    try { await api.computerPermissionSettings(permission); }
    catch (error) { onError(String(error)); }
  };
  const pending = approvals.filter((item) => item.tool_name === "computer_session_start" && item.status === "pending");
  return <section className="subpage-body computer-page">
    <label className="computer-toggle"><span><strong>{t("Enable application control")}</strong><small>{t("Adds app tools to this workspace's MCP connection.")}</small></span><input type="checkbox" checked={enabled} disabled={running || !supported} onChange={(event) => void onEnabled(event.target.checked)} /></label>
    <p className="field-copy">{t(supported ? "Stop the service before changing this switch. After starting it, refresh the tools in ChatGPT." : "Application control currently requires macOS.")}</p>
    {supported && <div className="computer-permissions"><button type="button" onClick={() => void setup("accessibility")}>{t("Accessibility settings")}</button><button type="button" onClick={() => void setup("screen_recording")}>{t("Screen Recording settings")}</button></div>}
    <p className="field-copy">{t("Approved window screenshots and text are sent to the connected AI. Access expires and can be stopped here.")}</p>
    {pending.map((approval) => {
      const scope = appApprovalScope(approval);
      return <div className="computer-request" key={approval.approval_id}>
        <strong>{scope?.name ?? t("Invalid access request")}</strong>
        {scope && <small className="computer-identity">{scope.identity}<br />{scope.path}</small>}
        <span>{scope ? `${t(scope.access === "control" ? "Observe and control" : "Observe only")} · ${Math.ceil(scope.seconds / 60)} ${t("minutes")}` : ""}</span>
        <p>{approval.reason}</p>
        <div><button type="button" onClick={() => void onDecision(approval.approval_id, false)}>{t("Deny")}</button><button className="approve" type="button" disabled={!scope} onClick={() => void onDecision(approval.approval_id, true)}>{t("Allow this session")}</button></div>
      </div>;
    })}
    {sessions.filter((session) => session.status === "active" && running).map((session) => <div className="approval-item" key={session.session_id}><span><strong>{session.app_name}</strong><small>{t(session.access === "control" ? "Observe and control" : "Observe only")}</small></span><button type="button" onClick={() => void onStop(session.session_id)}>{t("Stop now")}</button></div>)}
    {!pending.length && !sessions.some((session) => session.status === "active" && running) && <p className="empty-copy">{t("App access requests from ChatGPT will appear here.")}</p>}
  </section>;
}
