import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LogicalSize } from "@tauri-apps/api/dpi";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { writeText } from "@tauri-apps/plugin-clipboard-manager";
import { api } from "./api";
import { detectLanguage, translator, type Language } from "./i18n";
import type { DependencyStatus, GatewayConfig, LogBundle, PermissionMode, RuntimeStatus, WorkspaceProfile } from "./types";
import { publicEndpoint, workspaceHue } from "./utils";

const PANEL_WIDTH = 420;
const APP_VERSION = "0.5.1";
type Page = "home" | "new-access" | "workspaces" | "environment" | "logs" | "settings" | "approvals" | "more";
type CopyAction = "server-name" | "server-url" | "credential" | "logs";
type ConfirmAction = { kind: "stop"; profileId: string } | { kind: "stop-all" } | { kind: "quit" } | null;
type WorkspaceAction = { state: "starting" | "stopping" };

const stoppedStatus = (port: number): RuntimeStatus => ({ state: "stopped", pid: null, server_name: "", local_message: "Not running", public_message: "Unknown", public_url: "", local_url: `http://127.0.0.1:${port}/mcp` });
const quickTunnelProfile = (profile: WorkspaceProfile): WorkspaceProfile => ({ ...profile, tunnel: { ...profile.tunnel, type: "cloudflare", cloudflare_mode: "quick", domain: "", public_url: "", frp_server: "", frp_subdomain: "", cloudflare_token: "" }, auth: { ...profile.auth, type: "oauth" } });
const shortPath = (path: string) => path.split(/[\\/]/).filter(Boolean).slice(-2).join(" / ") || path;

function App() {
  const panelRef = useRef<HTMLElement>(null);
  const boundProjectId = useMemo(() => new URLSearchParams(window.location.search).get("project"), []);
  const projectWindow = Boolean(boundProjectId);
  const [language, setLanguage] = useState<Language>(detectLanguage());
  const [homeDirectory, setHomeDirectory] = useState<string | null>(null);
  const t = useMemo(() => translator(language), [language]);
  const [page, setPage] = useState<Page>("home");
  const [backTarget, setBackTarget] = useState<Page>("home");
  const [profiles, setProfiles] = useState<WorkspaceProfile[]>([]);
  const [gateway, setGateway] = useState<GatewayConfig | null>(null);
  const [discoveredCapabilityRoots, setDiscoveredCapabilityRoots] = useState<string[]>([]);
  const [migrationWarning, setMigrationWarning] = useState<string | null>(null);
  const [statuses, setStatuses] = useState<Record<string, RuntimeStatus>>({});
  const [workspaceActions, setWorkspaceActions] = useState<Record<string, WorkspaceAction>>({});
  const workspaceActionsRef = useRef<Record<string, WorkspaceAction>>({});
  const [runtimeState, setRuntimeState] = useState<Awaited<ReturnType<typeof api.snapshot>>["runtime_state"]>({});
  const [dependencies, setDependencies] = useState<DependencyStatus>({ uv: false, cloudflared: false, runtime_ready: false, runtime_version: null });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const refreshSequenceRef = useRef(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [copied, setCopied] = useState<CopyAction | null>(null);
  const [revealCredential, setRevealCredential] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction>(null);
  const [runtimeLogs, setRuntimeLogs] = useState<LogBundle | null>(null);
  const [installLog, setInstallLog] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [mcpNamePrefix, setMcpNamePrefix] = useState("www");

  const workspaceStatus = (profile: WorkspaceProfile) => {
    const current = statuses[profile.id] ?? stoppedStatus(profile.runtime.local_port);
    const action = workspaceActions[profile.id];
    return action ? { ...current, state: action.state } : current;
  };
  const selected = profiles.find((profile) => profile.id === selectedId) ?? null;
  const selectedPosition = selected ? profiles.findIndex((profile) => profile.id === selected.id) + 1 : 0;
  const selectedSubtitle = selected
    ? shortPath(selected.path)
    : t("Choose an access mode to begin.");
  const status = selected ? workspaceStatus(selected) : null;
  const starting = status?.state === "starting";
  const stopping = status?.state === "stopping";
  const running = Boolean(status?.pid) || starting || stopping;
  const serverUrl = selected && status?.state === "running" ? publicEndpoint(selected, status.public_url) : "";
  const credentialLabel = gateway?.auth.type === "bearer" ? "Bearer Token" : t("OAuth authorization passcode");
  const credential = gateway?.auth.type === "bearer" ? gateway.auth.bearer_token : gateway?.auth.oauth_password ?? "";
  const selectedRuntimeState = selected ? runtimeState[selected.id] : undefined;
  const allowedFolderCount = selected?.runtime.allowed_paths.length ?? 0;
  const defaultSearchPath = selected?.runtime.default_search_path || (selected?.runtime.permission_mode === "host" ? "~" : "");
  const localCapabilityRoots = gateway?.local_capability_roots ?? [];
  const environmentReady = dependencies.runtime_ready && dependencies.cloudflared;
  const activeWorkspaceCount = profiles.reduce((count, profile) => {
    const profileStatus = workspaceStatus(profile);
    return count + (Boolean(profileStatus.pid) || profileStatus.state === "starting" || profileStatus.state === "stopping" ? 1 : 0);
  }, 0);

  const refresh = useCallback(async (keepSelection = true) => {
    const sequence = ++refreshSequenceRef.current;
    try {
      const snapshot = await api.snapshot();
      if (sequence !== refreshSequenceRef.current) return;
      setLanguage(snapshot.language);
      setHomeDirectory(snapshot.home_directory);
      setGateway(snapshot.gateway);
      setDiscoveredCapabilityRoots(snapshot.discovered_local_capability_roots);
      setMigrationWarning(snapshot.migration_warning);
      setProfiles(snapshot.profiles);
      setStatuses(snapshot.statuses);
      setRuntimeState(snapshot.runtime_state);
      setDependencies(snapshot.dependencies);
      if (!snapshot.profiles.length && !projectWindow) setPage("new-access");
      setSelectedId((current) => {
        if (boundProjectId) {
          return snapshot.profiles.some((profile) => profile.id === boundProjectId) ? boundProjectId : null;
        }
        return keepSelection && current && snapshot.profiles.some((profile) => profile.id === current) ? current : snapshot.profiles[0]?.id ?? null;
      });
    } catch (reason) { setError(String(reason)); }
  }, [boundProjectId, projectWindow]);

  useEffect(() => { void refresh(false); const timer = window.setInterval(() => void refresh(), 2500); return () => window.clearInterval(timer); }, [refresh]);
  useEffect(() => { selectedIdRef.current = selectedId; setRevealCredential(false); setConfirmDelete(false); setConfirmAction(null); setRuntimeLogs(null); }, [selectedId]);
  useEffect(() => { setMcpNamePrefix(gateway?.server_name_prefix ?? "www"); }, [gateway?.server_name_prefix]);
  useEffect(() => { if (!confirmAction) return; const timer = window.setTimeout(() => setConfirmAction(null), 4000); return () => window.clearTimeout(timer); }, [confirmAction]);
  useEffect(() => { if (!confirmRemoveId) return; const timer = window.setTimeout(() => setConfirmRemoveId(null), 4000); return () => window.clearTimeout(timer); }, [confirmRemoveId]);
  useEffect(() => {
    if (!projectWindow) return;
    document.documentElement.classList.add("project-window-root");
    return () => document.documentElement.classList.remove("project-window-root");
  }, [projectWindow]);
  useEffect(() => {
    const panel = panelRef.current;
    if (projectWindow || !panel || typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const resize = () => { window.cancelAnimationFrame(frame); frame = window.requestAnimationFrame(() => void getCurrentWindow().setSize(new LogicalSize(PANEL_WIDTH, Math.min(544, Math.max(166, Math.ceil(panel.scrollHeight)))))); };
    const observer = new ResizeObserver(resize); observer.observe(panel); resize();
    return () => { window.cancelAnimationFrame(frame); observer.disconnect(); };
  }, [projectWindow]);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") { if (page !== "home") setPage("home"); else void getCurrentWindow().hide(); } };
    window.addEventListener("keydown", onKeyDown); return () => window.removeEventListener("keydown", onKeyDown);
  }, [page]);

  const copy = async (value: string, action: CopyAction) => {
    if (!value) return;
    try { await writeText(value); setCopied(action); window.setTimeout(() => setCopied((current) => current === action ? null : current), 1200); } catch (reason) { setError(String(reason)); }
  };
  const addStandardWorkspace = async () => {
    const appWindow = getCurrentWindow(); setError("");
    try {
      await appWindow.hide();
      let chosen: string | null;
      try { chosen = await api.pickWorkspaceFolder(); }
      finally { await appWindow.show(); await appWindow.setFocus(); }
      if (!chosen) return;
      const profile = await api.createProfile(chosen); await refresh(false); setSelectedId(profile.id); setPage("home");
    } catch (reason) { setError(String(reason)); }
  };
  const addFullAccess = async () => {
    const appWindow = getCurrentWindow(); setBusy("full-access-profile"); setError("");
    try {
      await appWindow.hide();
      let chosen: string | null;
      try { chosen = await api.pickWorkspaceFolder(); }
      finally { await appWindow.show(); await appWindow.setFocus(); }
      if (!chosen) return;
      const profile = await api.createFullAccessProfile(chosen);
      await refresh(false); setSelectedId(profile.id); setPage("home");
    } catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };
  const setWorkspaceAction = (profileId: string, state: WorkspaceAction["state"] | null, expected?: WorkspaceAction) => {
    if (expected && workspaceActionsRef.current[profileId] !== expected) return;
    const next = { ...workspaceActionsRef.current };
    const action = state ? { state } : undefined;
    if (action) next[profileId] = action;
    else delete next[profileId];
    workspaceActionsRef.current = next;
    setWorkspaceActions(next);
    return action;
  };
  const startWorkspace = async (profile: WorkspaceProfile) => {
    const action = setWorkspaceAction(profile.id, "starting"); setError("");
    try {
      const saved = await api.saveProfile(quickTunnelProfile(profile));
      setProfiles((current) => current.map((item) => item.id === saved.id ? saved : item));
      if (workspaceActionsRef.current[profile.id] !== action) return;
      const next = await api.startProfile(saved.id);
      if (workspaceActionsRef.current[profile.id] !== action) return;
      setStatuses((current) => ({ ...current, [profile.id]: next }));
      const endpoint = next.state === "running" ? publicEndpoint(saved, next.public_url) : "";
      if (endpoint) await copy(endpoint, "server-url");
    } catch (reason) {
      if (workspaceActionsRef.current[profile.id] === action && String(reason) !== "Workspace startup was cancelled.") setError(String(reason));
    } finally { setWorkspaceAction(profile.id, null, action); }
  };
  const stopWorkspace = async (profileId: string) => {
    const action = setWorkspaceAction(profileId, "stopping"); setError("");
    try {
      const next = await api.stopProfile(profileId);
      setStatuses((current) => ({ ...current, [profileId]: next }));
    } catch (reason) { setError(String(reason)); }
    finally { setWorkspaceAction(profileId, null, action); }
  };
  const toggleWorkspace = () => {
    if (!selected || !status || stopping || busy === "stop-all") return;
    if (starting) { setConfirmAction(null); void stopWorkspace(selected.id); return; }
    if (running) {
      if (confirmAction?.kind !== "stop" || confirmAction.profileId !== selected.id) { setConfirmAction({ kind: "stop", profileId: selected.id }); return; }
      setConfirmAction(null); void stopWorkspace(selected.id); return;
    }
    void startWorkspace(selected);
  };
  const runEnvironmentAction = async (label: string, action: () => Promise<string>) => {
    setBusy(label); setError("");
    try { const result = await action(); setInstallLog((current) => [...current, `${new Date().toLocaleTimeString()}  ${result}`]); await refresh(); }
    catch (reason) { const message = String(reason); setInstallLog((current) => [...current, `${new Date().toLocaleTimeString()}  ${t("Failed")}: ${message}`]); setError(message); }
    finally { setBusy(null); }
  };
  const loadLogs = async () => {
    if (!selected) return; const profileId = selected.id; setBusy("logs");
    try { const logs = await api.logs(profileId); if (selectedIdRef.current !== profileId) return; setRuntimeLogs(logs); setPage("logs"); setError(""); } catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };
  const savePermission = async (permissionMode: PermissionMode) => {
    if (!selected) return; setBusy("permission");
    try { const saved = await api.saveProfile({ ...selected, runtime: { ...selected.runtime, permission_mode: permissionMode, default_search_path: permissionMode === "host" ? defaultSearchPath || "~" : "" } }); setProfiles((current) => current.map((profile) => profile.id === saved.id ? saved : profile)); setError(""); }
    catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };
  const saveDefaultSearchPath = async (path: string) => {
    if (!selected) return; setBusy("search-path");
    try {
      const saved = await api.saveProfile({ ...selected, runtime: { ...selected.runtime, default_search_path: path } });
      setProfiles((current) => current.map((profile) => profile.id === saved.id ? saved : profile)); setError("");
    } catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };
  const chooseDefaultSearchFolder = async () => {
    if (!selected || selected.runtime.permission_mode !== "host") return;
    const appWindow = getCurrentWindow(); setBusy("search-path"); setError("");
    try {
      await appWindow.hide();
      let chosen: string | null;
      try { chosen = await api.pickAllowedFolder(); }
      finally { await appWindow.show(); await appWindow.setFocus(); }
      if (!chosen) return;
      const saved = await api.saveProfile({ ...selected, runtime: { ...selected.runtime, default_search_path: chosen } });
      setProfiles((current) => current.map((profile) => profile.id === saved.id ? saved : profile));
    } catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };
  const saveMcpNamePrefix = async () => {
    if (!gateway) return;
    const nextPrefix = mcpNamePrefix.trim();
    if (!nextPrefix || nextPrefix === gateway?.server_name_prefix) {
      setMcpNamePrefix(gateway?.server_name_prefix ?? "www");
      return;
    }
    setBusy("mcp-prefix");
    try {
      const saved = await api.saveServerNamePrefix(nextPrefix);
      setGateway(saved);
      setMcpNamePrefix(saved.server_name_prefix);
      await refresh();
      setError("");
    } catch (reason) {
      setMcpNamePrefix(gateway?.server_name_prefix ?? "www");
      setError(String(reason));
    } finally { setBusy(null); }
  };
  const addAllowedFolder = async () => {
    if (!selected) return;
    const appWindow = getCurrentWindow(); setBusy("file-scope"); setError("");
    try {
      await appWindow.hide();
      let chosen: string | null;
      try { chosen = await api.pickAllowedFolder(); }
      finally { await appWindow.show(); await appWindow.setFocus(); }
      if (!chosen || selected.runtime.allowed_paths.includes(chosen) || chosen === selected.path) return;
      const saved = await api.saveProfile({ ...selected, runtime: { ...selected.runtime, allowed_paths: [...selected.runtime.allowed_paths, chosen] } });
      setProfiles((current) => current.map((profile) => profile.id === saved.id ? saved : profile));
    } catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };
  const addLocalCapabilityFolder = async () => {
    if (!gateway) return;
    const appWindow = getCurrentWindow(); setBusy("local-capabilities"); setError("");
    try {
      await appWindow.hide();
      let chosen: string | null;
      try { chosen = await api.pickAllowedFolder(); }
      finally { await appWindow.show(); await appWindow.setFocus(); }
      if (!chosen || localCapabilityRoots.includes(chosen)) return;
      setGateway(await api.saveLocalCapabilityRoots([...localCapabilityRoots, chosen]));
    } catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };
  const removeLocalCapabilityFolder = async (path: string) => {
    if (!gateway) return;
    setBusy("local-capabilities"); setError("");
    try { setGateway(await api.saveLocalCapabilityRoots(localCapabilityRoots.filter((item) => item !== path))); }
    catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };
  const setAutoDiscoverLocalCapabilities = async (enabled: boolean) => {
    if (!gateway) return;
    setBusy("local-capabilities"); setError("");
    try { setGateway(await api.saveAutoDiscoverLocalCapabilities(enabled)); await refresh(); }
    catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };
  const changeWorkspace = async () => {
    if (!selected) return;
    const appWindow = getCurrentWindow(); setBusy("workspace-path"); setError("");
    try {
      await appWindow.hide();
      let chosen: string | null;
      try { chosen = await api.pickWorkspaceFolder(); }
      finally { await appWindow.show(); await appWindow.setFocus(); }
      if (!chosen || chosen === selected.path) return;
      const name = chosen.split(/[\\/]/).filter(Boolean).at(-1) || selected.name;
      const saved = await api.saveProfile({ ...selected, name, path: chosen, runtime: { ...selected.runtime, default_search_path: "" } });
      setProfiles((current) => current.map((profile) => profile.id === saved.id ? saved : profile));
    } catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };
  const removeAllowedFolder = async (path: string) => {
    if (!selected) return; setBusy("file-scope");
    try {
      const saved = await api.saveProfile({ ...selected, runtime: { ...selected.runtime, allowed_paths: selected.runtime.allowed_paths.filter((item) => item !== path), default_search_path: defaultSearchPath === path ? "" : defaultSearchPath } });
      setProfiles((current) => current.map((profile) => profile.id === saved.id ? saved : profile)); setError("");
    } catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };
  const stopAllWorkspaces = async () => {
    if (!activeWorkspaceCount || busy === "stop-all") return;
    if (confirmAction?.kind !== "stop-all") { setConfirmAction({ kind: "stop-all" }); return; }
    setConfirmAction(null); setBusy("stop-all"); setError("");
    const actions = profiles.filter((profile) => {
      const current = workspaceStatus(profile);
      return current.pid || current.state === "starting" || current.state === "stopping";
    }).map((profile) => [profile.id, setWorkspaceAction(profile.id, "stopping")] as const);
    try { setStatuses(await api.stopAllProfiles()); }
    catch (reason) { setError(String(reason)); }
    finally {
      for (const [id, action] of actions) setWorkspaceAction(id, null, action);
      setBusy(null);
    }
  };
  const quitApp = async () => { if (confirmAction?.kind !== "quit") { setConfirmAction({ kind: "quit" }); return; } setConfirmAction(null); await api.quit(); };
  const removeWorkspace = async () => {
    if (!selected) return; if (!confirmDelete) { setConfirmDelete(true); return; } setBusy("delete");
    try { await api.deleteProfile(selected.id); await refresh(false); setPage("home"); } catch (reason) { setError(String(reason)); } finally { setBusy(null); setConfirmDelete(false); }
  };
  const removeWorkspaceFromList = async (profileId: string) => {
    if (busy === "delete") return;
    if (confirmRemoveId !== profileId) { setConfirmRemoveId(profileId); return; }
    setConfirmRemoveId(null); setBusy("delete"); setError("");
    try { await api.deleteProfile(profileId); await refresh(false); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(null); }
  };
  const changeLanguage = async (next: Language) => {
    setBusy("language");
    try { await api.setLanguage(next); localStorage.setItem("coding-tools-language", next); setLanguage(next); setError(""); } catch (reason) { setError(String(reason)); } finally { setBusy(null); }
  };

  const decideApproval = async (approvalId: string, approved: boolean) => {
    if (!selected) return;
    setBusy(approvalId);
    try { await api.decideApproval(selected.id, approvalId, approved); await refresh(); setError(""); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(null); }
  };
  const openSubpage = (next: Page, back: Page) => { setBackTarget(back); setPage(next); };

  const pageTitle: Record<Page, string> = { home: "", "new-access": t("Choose access mode"), workspaces: t("Access profiles"), environment: t("Environment & setup"), logs: t("Runtime logs"), settings: t("Workspace settings"), approvals: t("Approvals"), more: t("More") };
  const backPage = page === "environment" || page === "logs" || page === "settings" || page === "approvals" ? backTarget : "home";

  return <main className={`panel${projectWindow ? " project-window" : ""}`} ref={panelRef}>
    {page === "home" ? <Header /> : <header className="subpage-header"><button className="icon-button" type="button" aria-label={t("Back")} onClick={() => setPage(backPage)}><BackIcon /></button><strong>{pageTitle[page]}</strong></header>}
    {error && <div className="error-banner" role="alert"><span>{t(error)}</span><button type="button" onClick={() => setError("")} aria-label={t("Dismiss")}>×</button></div>}
    {migrationWarning && <div className="migration-banner" role="status">{t(migrationWarning)}</div>}

    {page === "home" && <>
      <section className={`workspace-hero ${selected ? "tinted" : ""}`} style={selected ? { "--workspace-hue": workspaceHue(selectedPosition) } as React.CSSProperties : undefined}>
        <div className="workspace-picker-row"><button className="workspace-picker" type="button" disabled={!profiles.length || projectWindow} onClick={() => setPage("workspaces")}>{selected?.runtime.permission_mode === "host" ? <ShieldIcon /> : <FolderIcon />}<span><strong>{selected ? t(selected.name) : t(projectWindow ? "Project unavailable" : "No access profile")}</strong><small>{selectedSubtitle}</small></span>{profiles.length > 1 && !projectWindow && <SwitchIcon />}</button>{!projectWindow && <button className="icon-button" type="button" aria-label={t("Add access profile")} onClick={() => setPage("new-access")}><PlusIcon /></button>}</div>
        {selected && <div className="status-row"><span className="status-copy"><i className={`status-dot ${status?.state ?? "stopped"}`} />{profiles.length > 1 && <b className="status-index">[{selectedPosition}/{profiles.length}]</b>}{starting ? t("Preparing runtime dependencies…") : stopping ? t("Stopping…") : status?.state === "running" ? t("Running · public tunnel ready") : status?.state === "error" ? t("Connection error") : t("Workspace stopped")}</span><button className={`power-button ${running && !starting ? "danger" : ""} ${confirmAction?.kind === "stop" && confirmAction.profileId === selected.id ? "confirm" : ""}`} type="button" disabled={stopping} onClick={toggleWorkspace}>{starting || stopping ? <SpinnerIcon /> : null}{stopping ? t("Stopping…") : starting ? t("Cancel startup") : confirmAction?.kind === "stop" && confirmAction.profileId === selected.id ? t("Click again to stop") : t(running ? "Stop" : "Start")}</button></div>}
      </section>
      {selected && <section className="connection-block"><div className="gateway-label"><strong>{t("Shared MCP Gateway")}</strong><small>{t("One connection is shared by all projects.")}</small></div><ValueButton label={t("MCP name")} copyLabel={t("Copy")} value={status?.server_name || t("Start the workspace to create an MCP name")} disabled={!status?.server_name} copied={copied === "server-name"} onClick={() => void copy(status?.server_name ?? "", "server-name")} /><ValueButton label="Server URL" copyLabel={t("Copy")} value={serverUrl || t("Start the workspace to create a public URL")} disabled={!serverUrl} copied={copied === "server-url"} onClick={() => void copy(serverUrl, "server-url")} /><ValueButton label={credentialLabel} copyLabel={t("Copy")} value={revealCredential ? credential : "••••••••••••"} disabled={!credential} copied={copied === "credential"} onClick={() => void copy(credential, "credential")} after={<button className="reveal-button" type="button" aria-label={t(revealCredential ? "Hide authorization passcode" : "Show authorization passcode")} onClick={(event) => { event.stopPropagation(); setRevealCredential((current) => !current); }}>{revealCredential ? <EyeOffIcon /> : <EyeIcon />}</button>} /></section>}
      <nav className="menu-list" aria-label={t("Manage")}><MenuRow icon={<MoreIcon />} label={t("More")} onClick={() => setPage("more")} /></nav>
      <footer className="panel-footer"><button type="button" disabled={!selected} onClick={() => openSubpage("settings", "home")}><ShieldIcon /><span>{selected?.runtime.permission_mode === "host" ? t("Full Access") : t("Standard access")}{selected?.runtime.permission_mode !== "host" && allowedFolderCount ? ` · ${allowedFolderCount} ${t("folders")}` : ""}</span></button><button className={`footer-quit-button ${confirmAction?.kind === "quit" ? "confirm" : ""}`} type="button" onClick={() => void quitApp()}><PowerIcon /><span>{confirmAction?.kind === "quit" ? t("Click again to quit") : t("Quit Coding Tools MCP")}</span></button></footer>
    </>}

    {page === "new-access" && <section className="subpage-body access-mode-page">
      <button className="access-mode-choice" type="button" onClick={() => void addStandardWorkspace()}><span className="access-mode-icon"><FolderIcon /></span><span><strong>{t("Standard")}</strong><small>{t("Work in your project and folders you add.")}</small></span><ChevronIcon /></button>
      <button className="access-mode-choice full" type="button" disabled={busy === "full-access-profile"} onClick={() => void addFullAccess()}><span className="access-mode-icon"><ShieldIcon /></span><span><strong>{t("Full Access")}</strong><small>{t("Use Mac files and commands. Searches start in your home folder.")}</small></span>{busy === "full-access-profile" ? <SpinnerIcon /> : <ChevronIcon />}</button>
    </section>}

    {page === "workspaces" && <section className="subpage-body workspace-page">
      <div className="workspace-list">{profiles.map((profile, index) => { const profileStatus = workspaceStatus(profile); const removing = confirmRemoveId === profile.id; return <div style={{ "--workspace-hue": workspaceHue(index + 1) } as React.CSSProperties} className={`workspace-item ${profile.id === selectedId ? "selected" : ""}`} key={profile.id}>
        <button className="workspace-choice" type="button" onClick={() => { setConfirmRemoveId(null); setSelectedId(profile.id); setPage("home"); }}><i className={`status-dot ${profileStatus.state}`} /><span><strong>{t(profile.name)}</strong><small title={profile.path}>{shortPath(profile.path)}</small></span></button>
        <div className="workspace-item-actions"><button className="workspace-item-action" type="button" aria-label={`${t("Workspace settings")}: ${profile.name}`} title={t("Workspace settings")} onClick={() => { setConfirmRemoveId(null); setSelectedId(profile.id); openSubpage("settings", "workspaces"); }}><SettingsIcon /></button><button className={`workspace-item-action remove ${removing ? "confirm" : ""}`} type="button" disabled={busy === "delete"} aria-label={`${t(removing ? "Click again to remove project" : "Remove project")}: ${profile.name}`} title={t(removing ? "Click again to remove project" : "Remove project")} onClick={() => void removeWorkspaceFromList(profile.id)}><TrashIcon />{removing && <small>{t("Confirm")}</small>}</button></div>
      </div>; })}</div>
      <button className="primary-button" type="button" onClick={() => setPage("new-access")}><PlusIcon />{t("Add access profile")}</button>
      <button className={`stop-all-button ${confirmAction?.kind === "stop-all" ? "confirm" : ""}`} type="button" disabled={!activeWorkspaceCount || busy === "stop-all"} onClick={() => void stopAllWorkspaces()}>{busy === "stop-all" ? <SpinnerIcon /> : <StopIcon />}{busy === "stop-all" ? t("Stopping all…") : confirmAction?.kind === "stop-all" ? t("Click again to stop all") : t("Stop all")}{activeWorkspaceCount > 1 && busy !== "stop-all" && confirmAction?.kind !== "stop-all" ? <small>{activeWorkspaceCount}</small> : null}</button>
    </section>}

    {page === "environment" && <section className="subpage-body environment-page">
      <div className="environment-summary"><span><strong>{environmentReady ? t("Runtime environment is ready") : t("Runtime environment needs setup")}</strong><small>{t("Installed in the app's private directory")}</small></span><small>{navigator.platform.includes("Mac") ? "macOS" : navigator.platform}</small></div>
      <button className="primary-button" type="button" disabled={Boolean(busy)} onClick={() => void runEnvironmentAction("runtime", () => api.prepareRuntime(dependencies.runtime_ready))}>{busy === "runtime" ? <SpinnerIcon /> : <RepairIcon />}{dependencies.runtime_ready ? t("Repair runtime environment") : t("Install runtime environment")}</button>
      <div className="dependency-list"><DependencyRow name="MCP Runtime" detail={dependencies.runtime_version ?? t("Coding tools runtime")} ready={dependencies.runtime_ready} busy={busy === "runtime"} action={() => void runEnvironmentAction("runtime", () => api.prepareRuntime(dependencies.runtime_ready))} t={t} /><DependencyRow name="uv" detail={t("Runtime environment manager")} ready={dependencies.uv} busy={busy === "uv"} action={() => void runEnvironmentAction("uv", () => api.installResource("uv"))} t={t} /><DependencyRow name="cloudflared" detail={t("Public tunnel component")} ready={dependencies.cloudflared} busy={busy === "cloudflared"} action={() => void runEnvironmentAction("cloudflared", () => api.installResource("cloudflared"))} t={t} /></div>
      <div className="menu-list compact"><MenuRow icon={<RepairIcon />} label={t("Repair uv & cloudflared")} disabled={Boolean(busy)} onClick={() => void runEnvironmentAction("dependencies", api.repairDependencies)} /><MenuRow icon={<LogIcon />} label={t("Installation log")} detail={installLog.length ? String(installLog.length) : ""} onClick={() => openSubpage("logs", "environment")} /><MenuRow icon={<RefreshIcon />} label={t("Recheck environment")} disabled={Boolean(busy)} onClick={() => void refresh()} /></div>{busy && <div className="progress-line" role="progressbar" aria-label={t("Preparing runtime dependencies…")}><span /></div>}
    </section>}

    {page === "logs" && <section className="subpage-body logs-page">{installLog.length > 0 && <LogSection title={t("Installation log")} text={installLog.join("\n")} />}{runtimeLogs && <><LogSection title="stdout" text={runtimeLogs.stdout} /><LogSection title="stderr" text={runtimeLogs.stderr} /><LogSection title="cloudflared" text={runtimeLogs.cloudflared} /></>}{!installLog.length && !runtimeLogs && <p className="empty-copy">{t("No logs yet.")}</p>}<div className="button-row">{selected && <button className="secondary-button" type="button" onClick={() => void api.openLogs(selected.id)}>{t("Open logs folder")}</button>}<button className="secondary-button" type="button" onClick={() => void copy([installLog.join("\n"), runtimeLogs ? `stdout\n${runtimeLogs.stdout}\n\nstderr\n${runtimeLogs.stderr}\n\ncloudflared\n${runtimeLogs.cloudflared}` : ""].filter(Boolean).join("\n\n"), "logs")}>{copied === "logs" ? t("Copied") : t("Copy logs")}</button></div></section>}

    {page === "settings" && selected && <section className="subpage-body settings-page">
      <div className="field-copy"><strong>{t("MCP access")}</strong><small>{t(selected.runtime.permission_mode === "host" ? "Mac files and commands available to your account." : "File tools can use this project and extra folders.")}</small></div>
      <div className="segmented-control"><button type="button" className={selected.runtime.permission_mode !== "host" ? "selected" : ""} disabled={busy === "permission"} onClick={() => void savePermission("trusted")}>{t("Standard")}</button><button type="button" className={selected.runtime.permission_mode === "host" ? "selected danger" : ""} disabled={busy === "permission"} onClick={() => void savePermission("host")}>{t("Full Access")}</button></div>
      <div className="field-copy scope-copy"><strong>{t("Project folder")}</strong><small>{t("Commands and project rules start here. Git can target a child repository.")}</small></div>
      <div className="allowed-folder-list"><div className="allowed-folder-row fixed"><span><strong title={selected.path}>{selected.path}</strong></span></div></div>
      <button className="secondary-button add-folder-button" type="button" disabled={busy === "workspace-path"} onClick={() => void changeWorkspace()}><FolderIcon />{t("Change folder")}</button>
      <div className="field-copy scope-copy"><strong>{t("Default search folder")}</strong><small>{t("Searches without a path start here. Larger folders are slower; access stays unchanged.")}</small></div>
      <select className="search-folder-select" aria-label={t("Default search folder")} value={defaultSearchPath} disabled={busy === "search-path"} onChange={(event) => void saveDefaultSearchPath(event.target.value)}>
        <option value={selected.runtime.permission_mode === "host" ? "." : ""}>{selected.path}</option>
        {selected.runtime.permission_mode === "host" && homeDirectory && <option value="~">{homeDirectory}</option>}
        {selected.runtime.allowed_paths.map((path) => <option value={path} key={path}>{path}</option>)}
        {defaultSearchPath && defaultSearchPath !== "~" && !selected.runtime.allowed_paths.includes(defaultSearchPath) && <option value={defaultSearchPath}>{defaultSearchPath}</option>}
      </select>
      {selected.runtime.permission_mode === "host" && <button className="secondary-button add-folder-button" type="button" disabled={busy === "search-path"} onClick={() => void chooseDefaultSearchFolder()}><FolderIcon />{t("Choose search folder")}</button>}
      {selected.runtime.permission_mode !== "host" && <>
        <div className="field-copy scope-copy"><strong>{t("Extra folders")}</strong></div>
        <div className="allowed-folder-list">{selected.runtime.allowed_paths.map((path) => <div className="allowed-folder-row" key={path}><span><strong title={path}>{path}</strong></span><button type="button" disabled={busy === "file-scope"} onClick={() => void removeAllowedFolder(path)} aria-label={t("Remove allowed folder")}>×</button></div>)}</div>
        <button className="secondary-button add-folder-button" type="button" disabled={busy === "file-scope"} onClick={() => void addAllowedFolder()}><PlusIcon />{t("Add folder")}</button>
      </>}
      <details className="advanced-settings"><summary>{t("Advanced settings")}</summary><div className="advanced-settings-body">
        <div className="field-copy"><strong>{t("MCP name prefix")}</strong><small>{t("Shared by all projects.")}</small></div>
        <div className="mcp-prefix-row"><input aria-label={t("MCP name prefix")} value={mcpNamePrefix} maxLength={24} spellCheck={false} disabled={busy === "mcp-prefix"} onChange={(event) => setMcpNamePrefix(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void saveMcpNamePrefix(); }} /><button className="secondary-button" type="button" disabled={busy === "mcp-prefix" || !mcpNamePrefix.trim() || mcpNamePrefix.trim() === gateway?.server_name_prefix} onClick={() => void saveMcpNamePrefix()}>{t("Save")}</button></div>
        <div className="field-copy scope-copy"><strong>{t("Local Skills and plugins")}</strong><small>{t("Share folders for Skill discovery. Plugin actions need their own tools.")}</small></div>
        <label className="auto-discover-row"><input type="checkbox" checked={gateway?.auto_discover_local_capabilities ?? true} disabled={busy === "local-capabilities"} onChange={(event) => void setAutoDiscoverLocalCapabilities(event.target.checked)} /><span><strong>{t("Automatically find installed Skills")}</strong><small>{t("Checks known Codex and other agent folders, not your whole home folder.")}</small></span></label>
        {gateway?.auto_discover_local_capabilities && <><div className="field-copy scope-copy"><strong>{t("Found folders")}</strong><small>{discoveredCapabilityRoots.length} {t("folders")}</small></div><div className="allowed-folder-list">{discoveredCapabilityRoots.map((path) => <div className="allowed-folder-row fixed" key={path}><span><strong title={path}>{path}</strong></span></div>)}</div></>}
        <div className="field-copy scope-copy"><strong>{t("Additional Skill folders")}</strong></div>
        <div className="allowed-folder-list">{localCapabilityRoots.map((path) => <div className="allowed-folder-row" key={path}><span><strong title={path}>{path}</strong></span><button type="button" disabled={busy === "local-capabilities"} onClick={() => void removeLocalCapabilityFolder(path)} aria-label={t("Remove capability folder")}>×</button></div>)}</div>
        <button className="secondary-button add-folder-button" type="button" disabled={busy === "local-capabilities"} onClick={() => void addLocalCapabilityFolder()}><PlusIcon />{t("Add folder")}</button>
        {Boolean(activeWorkspaceCount) && <p className="hint-copy">{t("Shared changes apply when the Gateway restarts.")}</p>}
        <button className={`secondary-button remove-button ${confirmDelete ? "confirm" : ""}`} type="button" disabled={busy === "delete"} onClick={() => void removeWorkspace()}>{confirmDelete ? t("Click again to remove project") : t("Remove project")}</button>
        <p className="hint-copy">{t("Your project files stay on this Mac.")}</p>
      </div></details>
    </section>}


    {page === "approvals" && <section className="subpage-body approvals-page">{!selectedRuntimeState?.available ? <p className="empty-copy">{selectedRuntimeState?.warning || t("No approvals yet.")}</p> : <>{selectedRuntimeState.approvals.filter((item) => item.status === "pending").map((approval) => <div className="approval-item" key={approval.approval_id}><span><strong>{approval.reason}</strong><small>{approval.tool_name} · {approval.permission}</small></span><div><button type="button" disabled={busy === approval.approval_id} onClick={() => void decideApproval(approval.approval_id, false)}>{t("Deny")}</button><button className="approve" type="button" disabled={busy === approval.approval_id} onClick={() => void decideApproval(approval.approval_id, true)}>{t("Approve")}</button></div></div>)}{!selectedRuntimeState.approvals.some((item) => item.status === "pending") && <p className="empty-copy">{t("No approvals yet.")}</p>}</>}</section>}

    {page === "more" && <section className="subpage-body more-page"><div className="menu-list compact">
      <MenuRow icon={<FolderIcon />} label={t("Workspace settings")} disabled={!selected} onClick={() => openSubpage("settings", "more")} />
      <MenuRow icon={<PackageIcon />} label={t("Environment & setup")} detail={environmentReady ? t("Ready") : t("Setup needed")} onClick={() => openSubpage("environment", "more")} />
      {!projectWindow && <MenuRow icon={<SwitchIcon />} label={t("Open project window")} disabled={!selected} onClick={() => { if (selected) void api.openProjectWindow(selected.id); }} />}
      <MenuRow icon={<LogIcon />} label={t("Runtime logs")} disabled={!selected || busy === "logs"} onClick={() => { setBackTarget("more"); void loadLogs(); }} />
      <MenuRow icon={<ActivityIcon />} label={t("Approvals")} detail={selectedRuntimeState ? `${selectedRuntimeState.approvals.filter((item) => item.status === "pending").length} ${t("approvals")}` : ""} disabled={!selected} onClick={() => openSubpage("approvals", "more")} />
      <div className="language-row"><span><LanguageIcon />{t("Language")}</span><select aria-label={t("Language")} value={language} disabled={busy === "language"} onChange={(event) => void changeLanguage(event.target.value as Language)}><option value="zh-CN">简体中文</option><option value="en">English</option></select></div>
      <MenuRow icon={<RefreshIcon />} label={t("Refresh status")} onClick={() => void refresh()} />
      <MenuRow icon={<GithubIcon />} label={t("GitHub source")} onClick={() => void api.openResource("github")} />
    </div><div className="version-block"><span>Coding Tools MCP <strong>{APP_VERSION}</strong></span><span>MCP Runtime <strong>{dependencies.runtime_version ?? "—"}</strong></span></div><button className={`quit-button ${confirmAction?.kind === "quit" ? "confirm" : ""}`} type="button" onClick={() => void quitApp()}><PowerIcon />{confirmAction?.kind === "quit" ? t("Click again to quit") : t("Quit Coding Tools MCP")}</button></section>}
  </main>;
}

function Header() { return <header className="panel-header"><span className="brand-mark"><AppMarkIcon /></span><strong>Coding Tools MCP</strong><small>{APP_VERSION}</small></header>; }
function middleEllipsis(value: string, limit = 30) {
  if (value.length <= limit) return value;
  const visible = limit - 1;
  const start = Math.ceil(visible / 2);
  return `${value.slice(0, start)}…${value.slice(-Math.floor(visible / 2))}`;
}

function ValueButton({ label, copyLabel, value, disabled, copied, onClick, after }: { label: string; copyLabel: string; value: string; disabled: boolean; copied: boolean; onClick: () => void; after?: React.ReactNode }) { return <div className={`value-button-wrap ${after ? "has-secondary-action" : ""}`}><button className="value-button" type="button" disabled={disabled} onClick={onClick} title={disabled ? undefined : value}><span className="value-content"><small>{label}</small><code>{middleEllipsis(value)}</code></span><span className={`copy-action ${copied ? "copied" : ""}`} aria-hidden="true">{copied ? <CheckIcon /> : <><CopyIcon /><small>{disabled ? "" : copyLabel}</small></>}</span></button>{after}</div>; }
function MenuRow({ icon, label, detail = "", disabled = false, onClick }: { icon: React.ReactNode; label: string; detail?: string; disabled?: boolean; onClick: () => void }) { return <button className="menu-row" type="button" disabled={disabled} onClick={onClick}><span>{icon}<strong>{label}</strong></span><span>{detail && <small>{detail}</small>}<ChevronIcon /></span></button>; }
function DependencyRow({ name, detail, ready, busy, action, t }: { name: string; detail: string; ready: boolean; busy: boolean; action: () => void; t: (text: string) => string }) { return <div className="dependency-row"><span><strong>{name}</strong><small>{detail}</small></span><span className={ready ? "ready" : "needed"}>{ready ? t("Ready") : t("Missing")}</span><button type="button" disabled={busy} onClick={action}>{busy ? <SpinnerIcon /> : ready ? t("Repair") : t("Install")}</button></div>; }
function LogSection({ title, text }: { title: string; text: string }) { return <section className="log-section"><strong>{title}</strong><pre>{text || "—"}</pre></section>; }
const Icon = ({ children, className = "" }: { children: React.ReactNode; className?: string }) => <svg className={`icon ${className}`} viewBox="0 0 24 24" aria-hidden="true">{children}</svg>;
const AppMarkIcon = () => <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M12.252 12.331H7.749v1.002c0 1.014.821 1.835 1.835 1.835h.833a1.835 1.835 0 0 0 1.835-1.835zm-11.25-1.498a4.83 4.83 0 0 1 3.784-4.717A5.666 5.666 0 0 1 15.63 7.717 4 4 0 0 1 17 15.13a.665.665 0 1 1-.666-1.15A2.669 2.669 0 0 0 15 8.999a.665.665 0 0 1-.665-.666 4.335 4.335 0 0 0-8.313-1.725L5.9 6.92a.67.67 0 0 1-.448.423l-.092.02a3.502 3.502 0 0 0-1.783 6.147l.156.124.099.092a.665.665 0 0 1-.782 1.04l-.116-.068-.215-.171a4.82 4.82 0 0 1-1.717-3.695m12.58 2.5a3.164 3.164 0 0 1-2.917 3.155V17.5a.665.665 0 0 1-1.33 0v-1.012a3.164 3.164 0 0 1-2.916-3.155v-1.667c0-.367.298-.665.665-.665h.585v-1a.665.665 0 0 1 1.33 0v1h2.003v-1a.665.665 0 0 1 1.33 0v1h.585c.367 0 .665.298.665.665z" /></svg>;
const BackIcon = () => <Icon><path d="m15 18-6-6 6-6" /></Icon>;
const ChevronIcon = () => <Icon><path d="m9 18 6-6-6-6" /></Icon>;
const FolderIcon = () => <Icon><path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" /></Icon>;
const PlusIcon = () => <Icon><path d="M12 5v14M5 12h14" /></Icon>;
const SwitchIcon = () => <Icon><path d="m8 7 4-4 4 4M16 17l-4 4-4-4" /></Icon>;
const CopyIcon = () => <Icon><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></Icon>;
const CheckIcon = () => <Icon><path d="m5 12 4 4L19 6" /></Icon>;
const EyeIcon = () => <Icon><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" /><circle cx="12" cy="12" r="3" /></Icon>;
const EyeOffIcon = () => <Icon><path d="m3 3 18 18M10.6 10.6a2 2 0 0 0 2.8 2.8M9.9 4.2A10 10 0 0 1 12 4c6.5 0 10 8 10 8a18 18 0 0 1-2 3M6.6 6.6C3.5 8.5 2 12 2 12s3.5 8 10 8a9 9 0 0 0 4-.9" /></Icon>;
const PackageIcon = () => <Icon><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9Z" /><path d="m4.5 7.5 7.5 4 7.5-4M12 12v9" /></Icon>;
const LogIcon = () => <Icon><path d="M6 4h13v16H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z" /><path d="M8 8h7M8 12h8M8 16h5" /></Icon>;
const MoreIcon = () => <Icon><circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" /><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" /><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none" /></Icon>;
const ShieldIcon = () => <Icon><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z" /><path d="m9 12 2 2 4-4" /></Icon>;
const RefreshIcon = () => <Icon><path d="M20 11a8 8 0 0 0-14.8-4M4 4v5h5M4 13a8 8 0 0 0 14.8 4M20 20v-5h-5" /></Icon>;
const RepairIcon = () => <Icon><path d="M14.7 6.3a4 4 0 0 0-5-5L7 4l3 3 2.7-2.7a4 4 0 0 0 2 5L6 18.9a2.1 2.1 0 0 0 3 3l9.6-8.7a4 4 0 0 0 5-5L21 11l-3-3Z" /></Icon>;
const SpinnerIcon = () => <Icon className="spinner"><path d="M21 12a9 9 0 1 1-3-6.7" /></Icon>;
const LanguageIcon = () => <Icon><circle cx="12" cy="12" r="10" /><path d="M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20" /></Icon>;
const GithubIcon = () => <Icon><path d="M15 22v-4a4.8 4.8 0 0 0-1-3.5c3.3-.4 6.8-1.6 6.8-7A5.4 5.4 0 0 0 19.4 4 5 5 0 0 0 19.3.5S18.2.1 15 2a13.4 13.4 0 0 0-7 0C4.8.1 3.7.5 3.7.5A5 5 0 0 0 3.6 4a5.4 5.4 0 0 0-1.4 3.7c0 5.3 3.5 6.5 6.8 6.9A4.8 4.8 0 0 0 8 18v4M8 19c-3 .9-3-1.5-4-2" /></Icon>;
const PowerIcon = () => <Icon><path d="M12 2v10M18.4 6.6a9 9 0 1 1-12.8 0" /></Icon>;
const StopIcon = () => <Icon><rect x="6" y="6" width="12" height="12" rx="2" /></Icon>;
const TrashIcon = () => <Icon><path d="M4 7h16M10 3h4l1 2H9l1-2ZM6 7l1 13h10l1-13M10 11v5M14 11v5" /></Icon>;
const ActivityIcon = () => <Icon><path d="M3 12h4l3-8 4 16 3-8h4" /></Icon>;
const SettingsIcon = () => <Icon><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z" /></Icon>;

export default App;
