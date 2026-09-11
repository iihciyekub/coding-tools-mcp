import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LogicalSize } from "@tauri-apps/api/dpi";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { writeText } from "@tauri-apps/plugin-clipboard-manager";
import { open } from "@tauri-apps/plugin-dialog";
import { api } from "./api";
import { detectLanguage, translator } from "./i18n";
import type { DependencyStatus, PermissionMode, RuntimeStatus, WorkflowSnapshot, WorkspaceProfile } from "./types";
import { publicEndpoint } from "./utils";

const PANEL_WIDTH = 358;

const stoppedStatus = (port: number): RuntimeStatus => ({
  state: "stopped",
  pid: null,
  local_message: "Not running",
  public_message: "Unknown",
  public_url: "",
  local_url: `http://127.0.0.1:${port}/mcp`,
});

const quickTunnelProfile = (profile: WorkspaceProfile): WorkspaceProfile => ({
  ...profile,
  tunnel: {
    ...profile.tunnel,
    type: "cloudflare",
    cloudflare_mode: "quick",
    domain: "",
    public_url: "",
    frp_server: "",
    frp_subdomain: "",
    cloudflare_token: "",
  },
  auth: { ...profile.auth, type: "oauth" },
});

function App() {
  const panelRef = useRef<HTMLElement>(null);
  const t = useMemo(() => translator(detectLanguage()), []);
  const [profiles, setProfiles] = useState<WorkspaceProfile[]>([]);
  const [statuses, setStatuses] = useState<Record<string, RuntimeStatus>>({});
  const [workflow, setWorkflow] = useState<Record<string, WorkflowSnapshot>>({});
  const [dependencies, setDependencies] = useState<DependencyStatus>({
    uv: false,
    cloudflared: false,
    app_helper: false,
    runtime_ready: false,
    runtime_version: null,
    playwright_ready: false,
    playwright_version: null,
    chrome_installed: false,
    chrome_cdp_ready: false,
    chrome_manifest: false,
    chrome_bridge_connected: false,
    accessibility_trusted: null,
    screen_recording_trusted: null,
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [copiedAction, setCopiedAction] = useState<"server-url" | "passcode" | null>(null);
  const [revealPasscode, setRevealPasscode] = useState(false);
  const [error, setError] = useState("");

  const selected = profiles.find((profile) => profile.id === selectedId) ?? null;
  const selectedStatus = selected
    ? statuses[selected.id] ?? stoppedStatus(selected.runtime.local_port)
    : null;
  const selectedMcpUrl = selected && selectedStatus?.state === "running"
    ? publicEndpoint(selected, selectedStatus.public_url)
    : "";
  const selectedPassword = selected?.auth.oauth_password ?? "";
  const selectedWorkflow = selected ? workflow[selected.id] : undefined;

  const refresh = useCallback(async (keepSelection = true) => {
    try {
      const snapshot = await api.snapshot();
      setProfiles(snapshot.profiles);
      setStatuses(snapshot.statuses);
      setWorkflow(snapshot.workflow);
      setDependencies(snapshot.dependencies);
      setSelectedId((current) => {
        if (keepSelection && current && snapshot.profiles.some((profile) => profile.id === current)) {
          return current;
        }
        return snapshot.profiles[0]?.id ?? null;
      });
      setError("");
    } catch (reason) {
      setError(String(reason));
    }
  }, []);

  useEffect(() => {
    void refresh(false);
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    setExpanded(false);
    setMoreOpen(false);
    setConfirmDeleteId(null);
    setRevealPasscode(false);
  }, [selectedId]);

  useEffect(() => {
    if (!confirmDeleteId) return;
    const timer = window.setTimeout(() => setConfirmDeleteId(null), 3200);
    return () => window.clearTimeout(timer);
  }, [confirmDeleteId]);

  useEffect(() => {
    const collapseTransientUi = () => {
      setExpanded(false);
      setMoreOpen(false);
      setConfirmDeleteId(null);
      setRevealPasscode(false);
    };
    window.addEventListener("blur", collapseTransientUi);
    return () => window.removeEventListener("blur", collapseTransientUi);
  }, []);

  useEffect(() => {
    const panel = panelRef.current;
    if (!panel || typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const syncSize = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const height = Math.min(620, Math.max(168, Math.ceil(panel.scrollHeight)));
        void getCurrentWindow().setSize(new LogicalSize(PANEL_WIDTH, height));
      });
    };
    const observer = new ResizeObserver(syncSize);
    observer.observe(panel);
    syncSize();
    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, []);

  const markCopied = (action: "server-url" | "passcode") => {
    setCopiedAction(action);
    window.setTimeout(() => {
      setCopiedAction((current) => current === action ? null : current);
    }, 1200);
  };

  const copy = async (value: string, action: "server-url" | "passcode") => {
    if (!value) return;
    try {
      await writeText(value);
      markCopied(action);
    } catch (reason) {
      setError(String(reason));
    }
  };

  const openResource = async (target: "uv" | "cloudflared" | "github") => {
    setMoreOpen(false);
    setError("");
    try {
      await api.openResource(target);
    } catch (reason) {
      setError(String(reason));
    }
  };

  const installResource = async (target: "uv" | "cloudflared") => {
    setMoreOpen(false);
    setError("");
    try {
      await api.installResource(target);
      await refresh();
    } catch (reason) {
      setError(String(reason));
    }
  };

  const repairDependencies = async () => {
    setMoreOpen(false);
    setError("");
    try {
      await api.repairDependencies();
      await refresh();
    } catch (reason) {
      setError(String(reason));
    }
  };

  const prepareRuntime = async (repair = false) => {
    setMoreOpen(false);
    setError("");
    try {
      await api.prepareRuntime(repair);
      await refresh();
    } catch (reason) {
      setError(String(reason));
    }
  };

  const openPermissionSettings = async (permission: "accessibility" | "screen_recording") => {
    setMoreOpen(false);
    setError("");
    try {
      await api.openPermissionSettings(permission);
      await refresh();
    } catch (reason) {
      setError(String(reason));
    }
  };

  const prepareChromeBridge = async () => {
    setMoreOpen(false);
    setError("");
    try {
      await api.prepareChromeBridge();
      await refresh();
    } catch (reason) {
      setError(String(reason));
    }
  };

  const decideApproval = async (approvalId: string, approved: boolean) => {
    if (!selected) return;
    setBusyId(approvalId);
    setError("");
    try {
      await api.decideApproval(selected.id, approvalId, approved);
      await refresh();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusyId(null);
    }
  };

  const addManagedWorktree = async (path: string) => {
    setBusyId(path);
    setError("");
    try {
      const profile = await api.createProfile(path);
      await refresh();
      setSelectedId(profile.id);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusyId(null);
    }
  };

  const addWorkspace = async () => {
    const appWindow = getCurrentWindow();
    setError("");
    try {
      let chosen: string | string[] | null;
      try {
        chosen = await open({ directory: true, multiple: false, title: t("Choose folder") });
      } finally {
        await appWindow.show();
        await appWindow.setFocus();
      }
      if (typeof chosen !== "string") return;
      const profile = await api.createProfile(chosen);
      setProfiles((current) => [...current, profile]);
      setStatuses((current) => ({
        ...current,
        [profile.id]: stoppedStatus(profile.runtime.local_port),
      }));
      setSelectedId(profile.id);
    } catch (reason) {
      setError(String(reason));
    }
  };

  const toggleWorkspace = async (profile: WorkspaceProfile) => {
    const status = statuses[profile.id] ?? stoppedStatus(profile.runtime.local_port);
    setConfirmDeleteId(null);
    setBusyId(profile.id);
    setError("");
    try {
      let next: RuntimeStatus;
      if (status.pid || status.state === "starting") {
        next = await api.stopProfile(profile.id);
      } else {
        const saved = await api.saveProfile(quickTunnelProfile(profile));
        setProfiles((current) => current.map((item) => (item.id === saved.id ? saved : item)));
        next = await api.startProfile(saved.id);
        const serverUrl = next.state === "running" ? publicEndpoint(saved, next.public_url) : "";
        if (serverUrl) {
          await copy(serverUrl, "server-url");
        }
      }
      setStatuses((current) => ({ ...current, [profile.id]: next }));
    } catch (reason) {
      if (String(reason) !== "Workspace startup was cancelled.") setError(String(reason));
    } finally {
      setBusyId(null);
    }
  };

  const deleteWorkspace = async (profile: WorkspaceProfile) => {
    const status = statuses[profile.id] ?? stoppedStatus(profile.runtime.local_port);
    if (status.pid || status.state === "starting") return;
    if (confirmDeleteId !== profile.id) {
      setConfirmDeleteId(profile.id);
      return;
    }
    setBusyId(profile.id);
    setConfirmDeleteId(null);
    setError("");
    try {
      await api.deleteProfile(profile.id);
      const remaining = profiles.filter((item) => item.id !== profile.id);
      setProfiles(remaining);
      setStatuses((current) => {
        const next = { ...current };
        delete next[profile.id];
        return next;
      });
      if (selectedId === profile.id) setSelectedId(remaining[0]?.id ?? null);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusyId(null);
    }
  };

  const setPermissionMode = async (profile: WorkspaceProfile, permissionMode: PermissionMode) => {
    const status = statuses[profile.id] ?? stoppedStatus(profile.runtime.local_port);
    if (status.pid || status.state === "starting") return;
    setBusyId(profile.id);
    setError("");
    try {
      const updated: WorkspaceProfile = {
        ...profile,
        runtime: { ...profile.runtime, permission_mode: permissionMode },
      };
      const saved = await api.saveProfile(updated);
      setProfiles((current) => current.map((item) => (item.id === saved.id ? saved : item)));
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusyId(null);
    }
  };

  const statusLabel = selectedStatus?.state === "starting"
    ? t("Preparing runtime dependencies…")
    : selectedStatus?.pid
    ? selectedStatus.state === "error" ? t("Connection error") : t("Cloudflare quick tunnel is running")
    : selected ? t("Workspace stopped") : t("Add a workspace to begin.");

  return (
    <main className="panel" ref={panelRef}>
      <section className="active-connection" aria-label={t("Current connection")}>
        <div className="connection-row">
          <span className="brand-icon" aria-hidden="true"><PluginIcon /></span>
          <button
            className="connection-toggle"
            type="button"
            aria-expanded={expanded}
            onClick={() => selected && setExpanded((current) => !current)}
            disabled={!selected}
          >
            <ChevronIcon />
            <span className="connection-copy">
              <strong>{selected?.name ?? t("No workspace")}</strong>
              <span>
                <i className={`status-dot ${selectedStatus?.state ?? "stopped"}`} />
                {statusLabel}
              </span>
            </span>
          </button>
          <span className="copy-shortcuts">
            <button
              className={`quiet-action tooltip-action ${copiedAction === "server-url" ? "copied" : ""}`}
              type="button"
              disabled={!selectedMcpUrl}
              aria-label={t("Copy Server URL")}
              data-tooltip={t("Copy Server URL")}
              onClick={() => void copy(selectedMcpUrl, "server-url")}
            >
              {copiedAction === "server-url" ? <CheckIcon /> : <CopyIcon />}
            </button>
            <button
              className={`quiet-action tooltip-action ${copiedAction === "passcode" ? "copied" : ""}`}
              type="button"
              disabled={!selectedPassword}
              aria-label={t("Copy authorization passcode")}
              data-tooltip={t("Copy authorization passcode")}
              onClick={() => void copy(selectedPassword, "passcode")}
            >
              {copiedAction === "passcode" ? <CheckIcon /> : <KeyIcon />}
            </button>
          </span>
        </div>

        {expanded && selected && (
          <div className="connection-detail">
            {selectedStatus?.pid ? (
              <>
                <DetailRow
                  label={t("Server URL · changes after restart")}
                  value={selectedMcpUrl || t("Waiting for Cloudflare…")}
                  actions={selectedMcpUrl ? (
                    <IconAction
                      label={t("Copy Server URL")}
                      copied={copiedAction === "server-url"}
                      onClick={() => void copy(selectedMcpUrl, "server-url")}
                    >
                      <CopyIcon />
                    </IconAction>
                  ) : null}
                />
                <DetailRow
                  label={t("OAuth authorization passcode")}
                  value={revealPasscode ? selectedPassword : "••••••••••••"}
                  actions={(
                    <>
                      <IconAction
                        label={t(revealPasscode ? "Hide authorization passcode" : "Show authorization passcode")}
                        onClick={() => setRevealPasscode((current) => !current)}
                      >
                        {revealPasscode ? <EyeOffIcon /> : <EyeIcon />}
                      </IconAction>
                      <IconAction
                        label={t("Copy authorization passcode")}
                        copied={copiedAction === "passcode"}
                        onClick={() => void copy(selectedPassword, "passcode")}
                      >
                        <CopyIcon />
                      </IconAction>
                    </>
                  )}
                />
              </>
            ) : (
              <p className="idle-copy">{t("Starting automatically creates a Cloudflare quick tunnel. No domain or token is needed.")} {t("First launch may download missing runtime dependencies. Later launches reuse them.")}</p>
            )}
            <label className={`permission-mode-control ${selected.runtime.permission_mode === "host" ? "host-enabled" : ""}`}>
              <span className="permission-mode-copy">
                <strong>{t("Permission mode")}</strong>
                <small>{t("Choose how much command access this workspace receives.")}</small>
              </span>
              <select
                className="permission-select"
                value={selected.runtime.permission_mode}
                disabled={Boolean(selectedStatus?.pid) || selectedStatus?.state === "starting" || busyId === selected.id}
                onChange={(event) => void setPermissionMode(selected, event.target.value as PermissionMode)}
              >
                <option value="safe">{t("Safe")}</option>
                <option value="trusted">{t("Trusted")}</option>
                <option value="dangerous">{t("Dangerous")}</option>
                <option value="host">{t("Host")}</option>
              </select>
            </label>
            {selected.runtime.permission_mode === "host" && (
              <p className="host-warning">{t("High risk: authenticated MCP commands can access this Mac as you.")}</p>
            )}
          </div>
        )}
      </section>

      {error && <div className="error-banner" role="alert"><span>{error}</span><button type="button" onClick={() => setError("")} aria-label={t("Dismiss")}>×</button></div>}

      <section className="workspace-section" aria-label={t("Workspace")}>
        <header className="section-header">
          <span>{t("Workspace")}</span>
          <button className="add-action" type="button" onClick={() => void addWorkspace()}>
            <PlusIcon />
            <span>{t("Add")}</span>
          </button>
        </header>

        <div className="workspace-list">
          {profiles.map((profile) => {
            const status = statuses[profile.id] ?? stoppedStatus(profile.runtime.local_port);
            const running = Boolean(status.pid) || status.state === "starting";
            const busy = busyId === profile.id;
            const confirmingDelete = confirmDeleteId === profile.id;
            return (
              <div
                className={`workspace-row ${selectedId === profile.id ? "selected" : ""}`}
                key={profile.id}
                onClick={() => setSelectedId(profile.id)}
              >
                <span className="workspace-status"><i className={`status-dot ${status.state}`} /></span>
                <span className="workspace-copy">
                  <strong>{profile.name}</strong>
                  <span>{profile.path}</span>
                </span>
                <span className="row-actions">
                  <button
                    className={`run-action ${running ? "running" : ""}`}
                    type="button"
                    disabled={busy && status.state !== "starting"}
                    onClick={(event) => {
                      event.stopPropagation();
                      setSelectedId(profile.id);
                      void toggleWorkspace(profile);
                    }}
                  >
                    <PowerIcon />
                    <span>{status.state === "starting" ? t("Cancel startup") : busy ? t("Working…") : t(running ? "Stop" : "Start")}</span>
                  </button>
                  <button
                    className={`delete-action ${confirmingDelete ? "confirming" : ""}`}
                    type="button"
                    disabled={busy || running}
                    aria-label={running
                      ? `${t("Stop workspace before deleting")} ${profile.name}`
                      : `${confirmingDelete ? t("Click again to delete") : t("Delete workspace")} ${profile.name}`}
                    title={running ? t("Stop workspace before deleting") : confirmingDelete ? t("Click again to delete") : t("Delete workspace")}
                    onClick={(event) => {
                      event.stopPropagation();
                      void deleteWorkspace(profile);
                    }}
                  >
                    {confirmingDelete ? <><CheckIcon /><span>{t("Confirm")}</span></> : <TrashIcon />}
                  </button>
                </span>
              </div>
            );
          })}
          {!profiles.length && <div className="empty-list">{t("Add a workspace to begin.")}</div>}
        </div>
      </section>

      {selected && (
        <section className="workflow-section" aria-label={t("Workflow activity")}>
          <header className="section-header">
            <span>{t("Workflow activity")}</span>
            <span className="workflow-counts">
              {selectedWorkflow?.approvals.filter((item) => item.status === "pending").length ?? 0} {t("approvals")} · {selectedWorkflow?.tasks.length ?? 0} {t("tasks")} · {selectedWorkflow?.checks.length ?? 0} {t("checks")}
            </span>
          </header>
          {selectedWorkflow?.warning ? (
            <div className="workflow-empty workflow-warning">{selectedWorkflow.warning}</div>
          ) : !selectedWorkflow?.available || (
            !selectedWorkflow.tasks.length && !selectedWorkflow.checks.length && !selectedWorkflow.checkpoints.length && !selectedWorkflow.reviews.length && !selectedWorkflow.approvals.length && !selectedWorkflow.worktrees.length
          ) ? (
            <div className="workflow-empty">{t("No workflow activity yet.")}</div>
          ) : (
            <div className="workflow-list">
              {selectedWorkflow.approvals.filter((item) => item.status === "pending").map((approval) => (
                <div className="workflow-row approval-row" key={approval.approval_id} title={approval.arguments}>
                  <span className="workflow-badge pending">{t("Approval")}</span>
                  <span className="workflow-copy"><strong>{approval.reason}</strong><small>{approval.tool_name} · {approval.permission}</small></span>
                  <span className="approval-actions">
                    <button type="button" disabled={busyId === approval.approval_id} onClick={() => void decideApproval(approval.approval_id, false)}>{t("Deny")}</button>
                    <button className="approve" type="button" disabled={busyId === approval.approval_id} onClick={() => void decideApproval(approval.approval_id, true)}>{t("Approve")}</button>
                  </span>
                </div>
              ))}
              {selectedWorkflow.worktrees.filter((item) => !profiles.some((profile) => profile.path === item.path)).map((worktree) => (
                <div className="workflow-row approval-row" key={worktree.worktree_id} title={worktree.path}>
                  <span className="workflow-badge checkpoint">{t("Worktree")}</span>
                  <span className="workflow-copy"><strong>{worktree.worktree_id}</strong><small>{t("Isolated workspace")}</small></span>
                  <span className="approval-actions">
                    <button className="approve" type="button" disabled={busyId === worktree.path} onClick={() => void addManagedWorktree(worktree.path)}>{t("Add")}</button>
                  </span>
                </div>
              ))}
              {selectedWorkflow.tasks.slice(0, 3).map((task) => (
                <div className="workflow-row" key={task.task_id} title={task.task_id}>
                  <span className={`workflow-badge ${task.status}`}>{t("Task")}</span>
                  <span className="workflow-copy"><strong>{task.title}</strong><small>{task.status} · r{task.revision}{task.plan_total ? ` · ${task.plan_completed}/${task.plan_total} ${t("steps")}` : ""}</small></span>
                </div>
              ))}
              {selectedWorkflow.checks.slice(0, 2).map((check) => (
                <div className="workflow-row" key={check.check_run_id} title={check.check_run_id}>
                  <span className={`workflow-badge ${check.status}`}>{t("Check")}</span>
                  <span className="workflow-copy"><strong>{check.check_id}</strong><small>{check.status}</small></span>
                </div>
              ))}
              {selectedWorkflow.checkpoints.slice(0, 2).map((checkpoint) => (
                <div className="workflow-row" key={checkpoint.checkpoint_id} title={checkpoint.checkpoint_id}>
                  <span className="workflow-badge checkpoint">{t("Checkpoint")}</span>
                  <span className="workflow-copy"><strong>{checkpoint.label}</strong><small>{checkpoint.file_count} {t("files")}</small></span>
                </div>
              ))}
              {selectedWorkflow.reviews.slice(0, 2).map((review) => (
                <div className="workflow-row" key={review.review_id} title={review.review_id}>
                  <span className={`workflow-badge ${review.status}`}>{t("Review")}</span>
                  <span className="workflow-copy"><strong>{review.status}</strong><small>{review.finding_count} {t("findings")}</small></span>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      <footer className="panel-footer">
        <span><CheckIcon />{t("Local only")}</span>
        <button className="more-action" type="button" aria-label={t("More")} aria-expanded={moreOpen} onClick={() => setMoreOpen((current) => !current)}><MoreIcon /></button>
        {moreOpen && (
          <div className="more-menu">
            <div className="dependency-summary">
              <span>{t("Dependencies")}</span>
              <small>{dependencies.runtime_ready ? "✓" : "○"} {t("MCP Runtime")}{dependencies.runtime_version ? ` ${dependencies.runtime_version}` : ""}</small>
              <small>{dependencies.playwright_ready ? "✓" : "○"} Playwright{dependencies.playwright_version ? ` ${dependencies.playwright_version}` : ""}</small>
              <small>{dependencies.uv ? "✓" : "○"} uv</small>
              <small>{dependencies.cloudflared ? "✓" : "○"} cloudflared</small>
              <small>{dependencies.app_helper ? "✓" : "○"} {t("App Helper")}</small>
              <small>{dependencies.chrome_installed ? "✓" : "○"} Chrome</small>
              <small>{dependencies.chrome_cdp_ready ? "✓" : "○"} Chrome CDP</small>
              <small>{dependencies.chrome_bridge_connected ? "✓" : dependencies.chrome_manifest ? "◐" : "○"} {t("Chrome bridge")}{dependencies.chrome_bridge_connected ? ` · ${t("Connected")}` : dependencies.chrome_manifest ? ` · ${t("Installed")}` : ""}</small>
              <small>{dependencies.accessibility_trusted ? "✓" : "○"} {t("Accessibility")}</small>
              <small>{dependencies.screen_recording_trusted ? "✓" : "○"} {t("Screen Recording")}</small>
            </div>
            <button type="button" onClick={() => void prepareRuntime(false)}><DownloadIcon /><span>{t("Prepare runtime")}</span></button>
            <button type="button" onClick={() => void prepareRuntime(true)}><DownloadIcon /><span>{t("Repair runtime")}</span></button>
            <button type="button" onClick={() => void prepareChromeBridge()}><DownloadIcon /><span>{t("Prepare Chrome bridge")}</span></button>
            <button type="button" onClick={() => void openPermissionSettings("accessibility")}><DownloadIcon /><span>{t("Accessibility settings")}</span></button>
            <button type="button" onClick={() => void openPermissionSettings("screen_recording")}><DownloadIcon /><span>{t("Screen Recording settings")}</span></button>
            <button type="button" onClick={() => void repairDependencies()}><DownloadIcon /><span>{t("Repair uv & cloudflared")}</span></button>
            <button type="button" onClick={() => void installResource("uv")}><DownloadIcon /><span>{t("Install uv")}</span></button>
            <button type="button" onClick={() => void installResource("cloudflared")}><DownloadIcon /><span>{t("Install cloudflared")}</span></button>
            <button type="button" onClick={() => void openResource("github")}><GithubIcon /><span>{t("GitHub source")}</span></button>
            <div className="menu-separator" />
            <button className="quit-action" type="button" onClick={() => void api.quit()}><PowerIcon /><span>{t("Quit Coding Tools MCP")}</span></button>
          </div>
        )}
      </footer>

    </main>
  );
}

function DetailRow({
  label,
  value,
  actions,
}: {
  label: string;
  value: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="value-row">
      <div className="value-copy">
        <span>{label}</span>
        <code>{value}</code>
      </div>
      {actions && <div className="value-actions">{actions}</div>}
    </div>
  );
}

function IconAction({
  label,
  copied = false,
  onClick,
  children,
}: {
  label: string;
  copied?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      className={`detail-action tooltip-action ${copied ? "copied" : ""}`}
      aria-label={label}
      data-tooltip={label}
      onClick={onClick}
    >
      {copied ? <CheckIcon /> : children}
    </button>
  );
}

function PluginIcon() {
  return (
    <svg className="plugin-icon" viewBox="0 0 20 20" aria-hidden="true">
      <path d="M12.252 12.331H7.749v1.002c0 1.014.821 1.835 1.835 1.835h.833a1.835 1.835 0 0 0 1.835-1.835zm-11.25-1.498a4.83 4.83 0 0 1 3.784-4.717A5.666 5.666 0 0 1 15.63 7.717 4 4 0 0 1 17 15.13a.665.665 0 1 1-.666-1.15A2.669 2.669 0 0 0 15 8.999a.665.665 0 0 1-.665-.666 4.335 4.335 0 0 0-8.313-1.725L5.9 6.92a.67.67 0 0 1-.448.423l-.092.02a3.502 3.502 0 0 0-1.783 6.147l.156.124.099.092a.665.665 0 0 1-.782 1.04l-.116-.068-.215-.171a4.82 4.82 0 0 1-1.717-3.695m12.58 2.5a3.164 3.164 0 0 1-2.917 3.155V17.5a.665.665 0 0 1-1.33 0v-1.012a3.164 3.164 0 0 1-2.916-3.155v-1.667c0-.367.298-.665.665-.665h.585v-1a.665.665 0 0 1 1.33 0v1h2.003v-1a.665.665 0 0 1 1.33 0v1h.585c.367 0 .665.298.665.665z" />
    </svg>
  );
}

const StrokeIcon = ({ children, className = "" }: { children: React.ReactNode; className?: string }) => (
  <svg className={`stroke-icon ${className}`} viewBox="0 0 20 20" aria-hidden="true">{children}</svg>
);

const ChevronIcon = () => <StrokeIcon className="chevron-icon"><path d="m7 4 6 6-6 6" /></StrokeIcon>;

// Font Awesome Free 6.7.2 icons, Fonticons, Inc. (CC BY 4.0).
const FaIcon = ({ viewBox, path }: { viewBox: string; path: string }) => (
  <svg className="fa-icon" viewBox={viewBox} aria-hidden="true"><path d={path} /></svg>
);

const CopyIcon = () => <FaIcon viewBox="0 0 448 512" path="M208 0L332.1 0c12.7 0 24.9 5.1 33.9 14.1l67.9 67.9c9 9 14.1 21.2 14.1 33.9L448 336c0 26.5-21.5 48-48 48l-192 0c-26.5 0-48-21.5-48-48l0-288c0-26.5 21.5-48 48-48zM48 128l80 0 0 64-64 0 0 256 192 0 0-32 64 0 0 48c0 26.5-21.5 48-48 48L48 512c-26.5 0-48-21.5-48-48L0 176c0-26.5 21.5-48 48-48z" />;
const KeyIcon = () => <FaIcon viewBox="0 0 512 512" path="M336 352c97.2 0 176-78.8 176-176S433.2 0 336 0S160 78.8 160 176c0 18.7 2.9 36.8 8.3 53.7L7 391c-4.5 4.5-7 10.6-7 17l0 80c0 13.3 10.7 24 24 24l80 0c13.3 0 24-10.7 24-24l0-40 40 0c13.3 0 24-10.7 24-24l0-40 40 0c6.4 0 12.5-2.5 17-7l33.3-33.3c16.9 5.4 35 8.3 53.7 8.3zM376 96a40 40 0 1 1 0 80 40 40 0 1 1 0-80z" />;
const EyeIcon = () => <FaIcon viewBox="0 0 576 512" path="M288 32c-80.8 0-145.5 36.8-192.6 80.6C48.6 156 17.3 208 2.5 243.7c-3.3 7.9-3.3 16.7 0 24.6C17.3 304 48.6 356 95.4 399.4C142.5 443.2 207.2 480 288 480s145.5-36.8 192.6-80.6c46.8-43.5 78.1-95.4 93-131.1c3.3-7.9 3.3-16.7 0-24.6c-14.9-35.7-46.2-87.7-93-131.1C433.5 68.8 368.8 32 288 32zM144 256a144 144 0 1 1 288 0 144 144 0 1 1 -288 0zm144-64c0 35.3-28.7 64-64 64c-7.1 0-13.9-1.2-20.3-3.3c-5.5-1.8-11.9 1.6-11.7 7.4c.3 6.9 1.3 13.8 3.2 20.7c13.7 51.2 66.4 81.6 117.6 67.9s81.6-66.4 67.9-117.6c-11.1-41.5-47.8-69.4-88.6-71.1c-5.8-.2-9.2 6.1-7.4 11.7c2.1 6.4 3.3 13.2 3.3 20.3z" />;
const EyeOffIcon = () => <FaIcon viewBox="0 0 640 512" path="M38.8 5.1C28.4-3.1 13.3-1.2 5.1 9.2S-1.2 34.7 9.2 42.9l592 464c10.4 8.2 25.5 6.3 33.7-4.1s6.3-25.5-4.1-33.7L525.6 386.7c39.6-40.6 66.4-86.1 79.9-118.4c3.3-7.9 3.3-16.7 0-24.6c-14.9-35.7-46.2-87.7-93-131.1C465.5 68.8 400.8 32 320 32c-68.2 0-125 26.3-169.3 60.8L38.8 5.1zM223.1 149.5C248.6 126.2 282.7 112 320 112c79.5 0 144 64.5 144 144c0 24.9-6.3 48.3-17.4 68.7L408 294.5c8.4-19.3 10.6-41.4 4.8-63.3c-11.1-41.5-47.8-69.4-88.6-71.1c-5.8-.2-9.2 6.1-7.4 11.7c2.1 6.4 3.3 13.2 3.3 20.3c0 10.2-2.4 19.8-6.6 28.3l-90.3-70.8zM373 389.9c-16.4 6.5-34.3 10.1-53 10.1c-79.5 0-144-64.5-144-144c0-6.9 .5-13.6 1.4-20.2L83.1 161.5C60.3 191.2 44 220.8 34.5 243.7c-3.3 7.9-3.3 16.7 0 24.6c14.9 35.7 46.2 87.7 93 131.1C174.5 443.2 239.2 480 320 480c47.8 0 89.9-12.9 126.2-32.5L373 389.9z" />;
const PlusIcon = () => <FaIcon viewBox="0 0 448 512" path="M256 80c0-17.7-14.3-32-32-32s-32 14.3-32 32l0 144L48 224c-17.7 0-32 14.3-32 32s14.3 32 32 32l144 0 0 144c0 17.7 14.3 32 32 32s32-14.3 32-32l0-144 144 0c17.7 0 32-14.3 32-32s-14.3-32-32-32l-144 0 0-144z" />;
const PowerIcon = () => <FaIcon viewBox="0 0 512 512" path="M288 32c0-17.7-14.3-32-32-32s-32 14.3-32 32l0 224c0 17.7 14.3 32 32 32s32-14.3 32-32l0-224zM143.5 120.6c13.6-11.3 15.4-31.5 4.1-45.1s-31.5-15.4-45.1-4.1C49.7 115.4 16 181.8 16 256c0 132.5 107.5 240 240 240s240-107.5 240-240c0-74.2-33.8-140.6-86.6-184.6c-13.6-11.3-33.8-9.4-45.1 4.1s-9.4 33.8 4.1 45.1c38.9 32.3 63.5 81 63.5 135.4c0 97.2-78.8 176-176 176s-176-78.8-176-176c0-54.4 24.7-103.1 63.5-135.4z" />;
const TrashIcon = () => <FaIcon viewBox="0 0 448 512" path="M135.2 17.7C140.6 6.8 151.7 0 163.8 0L284.2 0c12.1 0 23.2 6.8 28.6 17.7L320 32l96 0c17.7 0 32 14.3 32 32s-14.3 32-32 32L32 96C14.3 96 0 81.7 0 64S14.3 32 32 32l96 0 7.2-14.3zM32 128l384 0 0 320c0 35.3-28.7 64-64 64L96 512c-35.3 0-64-28.7-64-64l0-320zm96 64c-8.8 0-16 7.2-16 16l0 224c0 8.8 7.2 16 16 16s16-7.2 16-16l0-224c0-8.8-7.2-16-16-16zm96 0c-8.8 0-16 7.2-16 16l0 224c0 8.8 7.2 16 16 16s16-7.2 16-16l0-224c0-8.8-7.2-16-16-16zm96 0c-8.8 0-16 7.2-16 16l0 224c0 8.8 7.2 16 16 16s16-7.2 16-16l0-224c0-8.8-7.2-16-16-16z" />;
const CheckIcon = () => <FaIcon viewBox="0 0 448 512" path="M438.6 105.4c12.5 12.5 12.5 32.8 0 45.3l-256 256c-12.5 12.5-32.8 12.5-45.3 0l-128-128c-12.5-12.5-12.5-32.8 0-45.3s32.8-12.5 45.3 0L160 338.7 393.4 105.4c12.5-12.5 32.8-12.5 45.3 0z" />;
const MoreIcon = () => <FaIcon viewBox="0 0 448 512" path="M8 256a56 56 0 1 1 112 0A56 56 0 1 1 8 256zm160 0a56 56 0 1 1 112 0 56 56 0 1 1 -112 0zm216-56a56 56 0 1 1 0 112 56 56 0 1 1 0-112z" />;
const DownloadIcon = () => <FaIcon viewBox="0 0 512 512" path="M288 32c0-17.7-14.3-32-32-32s-32 14.3-32 32l0 242.7-73.4-73.4c-12.5-12.5-32.8-12.5-45.3 0s-12.5 32.8 0 45.3l128 128c12.5 12.5 32.8 12.5 45.3 0l128-128c12.5-12.5 12.5-32.8 0-45.3s-32.8-12.5-45.3 0L288 274.7 288 32zM64 352c-35.3 0-64 28.7-64 64l0 32c0 35.3 28.7 64 64 64l384 0c35.3 0 64-28.7 64-64l0-32c0-35.3-28.7-64-64-64l-101.5 0-45.3 45.3c-25 25-65.5 25-90.5 0L165.5 352 64 352zm368 56a24 24 0 1 1 0 48 24 24 0 1 1 0-48z" />;
const GithubIcon = () => <FaIcon viewBox="0 0 496 512" path="M165.9 397.4c0 2-2.3 3.6-5.2 3.6-3.3.3-5.6-1.3-5.6-3.6 0-2 2.3-3.6 5.2-3.6 3-.3 5.6 1.3 5.6 3.6zm-31.1-4.5c-.7 2 1.3 4.3 4.3 4.9 2.6 1 5.6 0 6.2-2s-1.3-4.3-4.3-5.2c-2.6-.7-5.5.3-6.2 2.3zm44.2-1.7c-2.9.7-4.9 2.6-4.6 4.9.3 2 2.9 3.3 5.9 2.6 2.9-.7 4.9-2.6 4.6-4.6-.3-1.9-3-3.2-5.9-2.9zM244.8 8C106.1 8 0 113.3 0 252c0 110.9 69.8 205.8 169.5 239.2 12.8 2.3 17.3-5.6 17.3-12.1 0-6.2-.3-40.4-.3-61.4 0 0-70 15-84.7-29.8 0 0-11.4-29.1-27.8-36.6 0 0-22.9-15.7 1.6-15.4 0 0 24.9 2 38.6 25.8 21.9 38.6 58.6 27.5 72.9 20.9 2.3-16 8.8-27.1 16-33.7-55.9-6.2-112.3-14.3-112.3-110.5 0-27.5 7.6-41.3 23.6-58.9-2.6-6.5-11.1-33.3 2.6-67.9 20.9-6.5 69 27 69 27 20-5.6 41.5-8.5 62.8-8.5s42.8 2.9 62.8 8.5c0 0 48.1-33.6 69-27 13.7 34.7 5.2 61.4 2.6 67.9 16 17.7 25.8 31.5 25.8 58.9 0 96.5-58.9 104.2-114.8 110.5 9.2 7.9 17 22.9 17 46.4 0 33.7-.3 75.4-.3 83.6 0 6.5 4.6 14.4 17.3 12.1C428.2 457.8 496 362.9 496 252 496 113.3 383.5 8 244.8 8zM97.2 352.9c-1.3 1-1 3.3.7 5.2 1.6 1.6 3.9 2.3 5.2 1 1.3-1 1-3.3-.7-5.2-1.6-1.6-3.9-2.3-5.2-1zm-10.8-8.1c-.7 1.3.3 2.9 2.3 3.9 1.6 1 3.6.7 4.3-.7.7-1.3-.3-2.9-2.3-3.9-2-.6-3.6-.3-4.3.7zm32.4 35.6c-1.6 1.3-1 4.3 1.3 6.2 2.3 2.3 5.2 2.6 6.5 1 1.3-1.3.7-4.3-1.3-6.2-2.2-2.3-5.2-2.6-6.5-1zm-11.4-14.7c-1.6 1-1.6 3.6 0 5.9 1.6 2.3 4.3 3.3 5.6 2.3 1.6-1.3 1.6-3.9 0-6.2-1.4-2.3-4-3.3-5.6-2z" />;

export default App;
