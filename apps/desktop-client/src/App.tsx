import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { writeText } from "@tauri-apps/plugin-clipboard-manager";
import { open } from "@tauri-apps/plugin-dialog";
import { api } from "./api";
import { detectLanguage, translator, type Language } from "./i18n";
import type { LogBundle, RuntimeStatus, WorkspaceProfile } from "./types";
import { frpSnippet, normalizeDomain, publicEndpoint, recommendedPublicUrl } from "./utils";

const SIDEBAR_STORAGE_KEY = "coding-tools-sidebar-width";
const SIDEBAR_MIN_WIDTH = 350;
const SIDEBAR_MAX_WIDTH = 480;

const defaultSidebarWidth = () => SIDEBAR_MIN_WIDTH;

const initialSidebarWidth = () => {
  const saved = localStorage.getItem(SIDEBAR_STORAGE_KEY);
  const stored = saved === null ? Number.NaN : Number(saved);
  return Number.isFinite(stored)
    ? Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, stored))
    : defaultSidebarWidth();
};

const stoppedStatus = (port: number): RuntimeStatus => ({
  state: "stopped",
  pid: null,
  local_message: "Not running",
  public_message: "Unknown",
  public_url: "",
  local_url: `http://127.0.0.1:${port}/mcp`,
});

function App() {
  const shellRef = useRef<HTMLElement>(null);
  const resizingPointerId = useRef<number | null>(null);
  const sidebarWidthRef = useRef(0);
  const [language, setLanguage] = useState<Language>(detectLanguage);
  const t = useMemo(() => translator(language), [language]);
  const [profiles, setProfiles] = useState<WorkspaceProfile[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [statuses, setStatuses] = useState<Record<string, RuntimeStatus>>({});
  const [logs, setLogs] = useState<LogBundle>({ cloudflared: "", stderr: "", stdout: "" });
  const [busy, setBusy] = useState<"start" | "stop" | "save" | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [reveal, setReveal] = useState<Record<string, boolean>>({});
  const [activeTab, setActiveTab] = useState<"configuration" | "logs" | "help">("configuration");
  const [sidebarWidth, setSidebarWidth] = useState(initialSidebarWidth);
  sidebarWidthRef.current = sidebarWidth;

  const selected = profiles.find((profile) => profile.id === selectedId) ?? null;
  const status = selected
    ? statuses[selected.id] ?? stoppedStatus(selected.runtime.local_port)
    : null;
  const currentMcpUrl = selected && status?.state === "running"
    ? publicEndpoint(selected, status?.public_url)
    : "";
  const currentAuthorizationCode = selected
    ? selected.auth.type === "oauth" ? selected.auth.oauth_password : selected.auth.bearer_token
    : "";
  const authorizationCopyLabel = selected?.auth.type === "bearer" ? "Copy token" : "Copy password";

  const refresh = useCallback(async (keepSelection = true) => {
    try {
      const snapshot = await api.snapshot();
      setProfiles(snapshot.profiles);
      setStatuses(snapshot.statuses);
      setSelectedId((current) => {
        if (keepSelection && current && snapshot.profiles.some((item) => item.id === current)) return current;
        return snapshot.profiles[0]?.id ?? null;
      });
      setError("");
    } catch (reason) {
      setError(String(reason));
    }
  }, []);

  useEffect(() => {
    void refresh(false);
  }, [refresh]);

  useEffect(() => () => document.body.classList.remove("sidebar-resizing"), []);

  useEffect(() => {
    if (!selectedId) {
      setLogs({ cloudflared: "", stderr: "", stdout: "" });
      return;
    }
    let cancelled = false;
    const load = async () => {
      try {
        const [nextStatus, nextLogs] = await Promise.all([api.status(selectedId), api.logs(selectedId)]);
        if (cancelled) return;
        setStatuses((current) => ({ ...current, [selectedId]: nextStatus }));
        setLogs(nextLogs);
      } catch {
        // A profile may disappear between the list refresh and this poll.
      }
    };
    void load();
    const timer = window.setInterval(load, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [selectedId]);

  const flash = (message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 2600);
  };

  const updateSelected = (update: (profile: WorkspaceProfile) => WorkspaceProfile) => {
    if (!selectedId) return;
    setProfiles((current) => current.map((profile) => (profile.id === selectedId ? update(profile) : profile)));
  };

  const saveSelected = async () => {
    if (!selected) return null;
    setBusy("save");
    setError("");
    try {
      const saved = await api.saveProfile(selected);
      setProfiles((current) => current.map((profile) => (profile.id === saved.id ? saved : profile)));
      flash(t("Configuration saved"));
      return saved;
    } catch (reason) {
      setError(String(reason));
      return null;
    } finally {
      setBusy(null);
    }
  };

  const addWorkspace = async () => {
    const chosen = await open({ directory: true, multiple: false, title: t("Choose folder") });
    if (typeof chosen !== "string") return;
    try {
      const profile = await api.createProfile(chosen);
      setProfiles((current) => [...current, profile]);
      setStatuses((current) => ({ ...current, [profile.id]: stoppedStatus(profile.runtime.local_port) }));
      setSelectedId(profile.id);
    } catch (reason) {
      setError(String(reason));
    }
  };

  const deleteSelected = async () => {
    if (!selected) return;
    if (!window.confirm(`${t("Confirm delete")}\n\n${t("This removes the profile, not the workspace directory.")}`)) return;
    try {
      await api.deleteProfile(selected.id);
      await refresh(false);
    } catch (reason) {
      setError(String(reason));
    }
  };

  const runAction = async (action: "start" | "stop") => {
    if (!selected) return;
    setBusy(action);
    setError("");
    try {
      if (action === "start") await api.saveProfile(selected);
      const next = action === "start" ? await api.startProfile(selected.id) : await api.stopProfile(selected.id);
      setStatuses((current) => ({ ...current, [selected.id]: next }));
      setLogs(await api.logs(selected.id));
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(null);
    }
  };

  const copy = async (value: string) => {
    if (!value) return;
    await writeText(value);
    flash(t("Copied"));
  };

  const changeLanguage = (next: Language) => {
    localStorage.setItem("coding-tools-language", next);
    setLanguage(next);
  };

  const setAndStoreSidebarWidth = (width: number) => {
    const next = Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, Math.round(width)));
    sidebarWidthRef.current = next;
    setSidebarWidth(next);
    localStorage.setItem(SIDEBAR_STORAGE_KEY, String(next));
  };

  const resizeSidebar = (clientX: number) => {
    const shellLeft = shellRef.current?.getBoundingClientRect().left ?? 0;
    const next = Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, Math.round(clientX - shellLeft)));
    sidebarWidthRef.current = next;
    setSidebarWidth(next);
  };

  const beginSidebarResize = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    resizingPointerId.current = event.pointerId;
    event.currentTarget.setPointerCapture(event.pointerId);
    document.body.classList.add("sidebar-resizing");
    resizeSidebar(event.clientX);
    event.preventDefault();
  };

  const continueSidebarResize = (event: PointerEvent<HTMLDivElement>) => {
    if (resizingPointerId.current !== event.pointerId) return;
    resizeSidebar(event.clientX);
  };

  const finishSidebarResize = (event: PointerEvent<HTMLDivElement>) => {
    if (resizingPointerId.current !== event.pointerId) return;
    resizingPointerId.current = null;
    document.body.classList.remove("sidebar-resizing");
    localStorage.setItem(SIDEBAR_STORAGE_KEY, String(sidebarWidthRef.current));
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const handleSidebarResizeKey = (event: KeyboardEvent<HTMLDivElement>) => {
    let next = sidebarWidth;
    if (event.key === "ArrowLeft") next -= event.shiftKey ? 32 : 8;
    else if (event.key === "ArrowRight") next += event.shiftKey ? 32 : 8;
    else if (event.key === "Home") next = SIDEBAR_MIN_WIDTH;
    else if (event.key === "End") next = SIDEBAR_MAX_WIDTH;
    else return;
    event.preventDefault();
    setAndStoreSidebarWidth(next);
  };

  return (
    <main
      className="app-shell"
      ref={shellRef}
      style={{ "--sidebar-width": `${sidebarWidth}px` } as React.CSSProperties}
    >
      <div className="window-titlebar" data-tauri-drag-region>
        {selected && (
          <nav className="content-tabs" role="tablist" aria-label={t("Workspace views")}>
            {([
              ["configuration", "Configuration", "configuration"],
              ["logs", "Log output", "logs"],
              ["help", "Help", "help"],
            ] as const).map(([tab, label, icon]) => (
              <button
                key={tab}
                className={activeTab === tab ? "active" : ""}
                role="tab"
                aria-selected={activeTab === tab}
                title={t(label)}
                onClick={() => setActiveTab(tab)}
              >
                <TabIcon name={icon} />
                <span>{t(label)}</span>
              </button>
            ))}
          </nav>
        )}
      </div>

      <aside className="sidebar" data-tauri-drag-region>
        <div className="brand" data-tauri-drag-region>
          <div className="app-icon" aria-hidden="true"><PluginIcon /></div>
          <div>
            <p className="eyebrow">{t("Workspace console")}</p>
            <h1>{t("CMT Desktop")}</h1>
          </div>
        </div>
        <p className="sidebar-copy">{t("ChatGPT Coding MCP Tool")}</p>

        <div className="sidebar-actions">
          <button className="button primary" onClick={addWorkspace}>＋ {t("Add workspace")}</button>
          <button className="icon-button" onClick={() => void refresh()} aria-label={t("Refresh")} title={t("Refresh")}>↻</button>
        </div>

        <nav className="workspace-list" aria-label={t("Workspace")} data-tauri-drag-region>
          {profiles.map((profile) => {
            const itemStatus = statuses[profile.id] ?? stoppedStatus(profile.runtime.local_port);
            return (
              <button
                key={profile.id}
                className={`workspace-item ${selectedId === profile.id ? "selected" : ""}`}
                onClick={() => setSelectedId(profile.id)}
              >
                <span className={`status-dot ${itemStatus.state}`} />
                <span className="workspace-copy">
                  <strong>{profile.name}</strong>
                  <small>{profile.path}</small>
                </span>
                <span className="workspace-port">:{profile.runtime.local_port}</span>
              </button>
            );
          })}
          {!profiles.length && <div className="empty-list">{t("Add a workspace to begin.")}</div>}
        </nav>

        <label className="language-picker">
          <span>{t("Language")}</span>
          <select value={language} onChange={(event) => changeLanguage(event.target.value as Language)}>
            <option value="en">English</option>
            <option value="zh-CN">简体中文</option>
          </select>
        </label>
        <div
          className="sidebar-resizer"
          role="separator"
          aria-label={t("Resize sidebar")}
          aria-orientation="vertical"
          aria-valuemin={SIDEBAR_MIN_WIDTH}
          aria-valuemax={SIDEBAR_MAX_WIDTH}
          aria-valuenow={sidebarWidth}
          tabIndex={0}
          onKeyDown={handleSidebarResizeKey}
          onPointerDown={beginSidebarResize}
          onPointerMove={continueSidebarResize}
          onPointerUp={finishSidebarResize}
          onPointerCancel={finishSidebarResize}
          onLostPointerCapture={finishSidebarResize}
        />
      </aside>

      <section className="content" data-tauri-drag-region>
        {!selected ? (
          <div className="empty-state" data-tauri-drag-region>
            <div className="empty-symbol" aria-hidden="true"><PluginIcon /></div>
            <h2>{t("Select a workspace")}</h2>
            <p>{t("Add a workspace to begin.")}</p>
            <button className="button primary" onClick={addWorkspace}>{t("Add workspace")}</button>
          </div>
        ) : (
          <>
            <header className="content-header" data-tauri-drag-region>
              <div>
                <div className="title-row">
                  <h2>{selected.name}</h2>
                  <span className={`status-pill ${status?.state ?? "stopped"}`}>
                    <span className="status-dot" />
                    {t(status?.state === "running" ? "Running" : status?.state === "error" ? "Error" : "Stopped")}
                  </span>
                </div>
                <p>{selected.path}</p>
              </div>
              <div className="header-actions">
                <button className="button secondary quick-action" onClick={() => void copy(currentMcpUrl)} disabled={!currentMcpUrl} title={t(currentMcpUrl ? "Copy MCP address" : "Start the public tunnel first")}>⧉ {t("Copy MCP")}</button>
                <button className="button secondary quick-action" onClick={() => void copy(currentAuthorizationCode)} title={t(authorizationCopyLabel)}>⌘ {t(authorizationCopyLabel)}</button>
                <button className="button danger-quiet" onClick={deleteSelected}>{t("Delete")}</button>
                <button className="button secondary" onClick={() => void saveSelected()} disabled={busy !== null}>{t("Save")}</button>
                <button className="button secondary" onClick={() => void runAction("stop")} disabled={busy !== null || !status?.pid}>
                  {busy === "stop" ? t("Stopping…") : t("Stop")}
                </button>
                <button className="button primary" onClick={() => void runAction("start")} disabled={busy !== null || Boolean(status?.pid)}>
                  {busy === "start" ? t("Starting…") : t("Start")}
                </button>
              </div>
            </header>

            {error && <div className="banner error"><strong>{t("Operation failed")}</strong><span>{error}</span><button onClick={() => setError("")}>×</button></div>}
            {notice && <div className="toast">✓ {notice}</div>}

            {activeTab === "configuration" && <div className="dashboard-grid" role="tabpanel" data-tauri-drag-region>
              <section className="card workspace-card">
                <CardTitle icon="folder" title={t("Workspace")} />
                <div className="form-grid">
                  <Field label={t("Name")}>
                    <input value={selected.name} onChange={(event) => {
                      const name = event.target.value;
                      updateSelected((profile) => {
                        const previousRecommendation = recommendedPublicUrl(profile.name, profile.tunnel.domain);
                        const shouldRefreshUrl = !profile.tunnel.public_url || profile.tunnel.public_url === previousRecommendation;
                        return {
                          ...profile,
                          name,
                          tunnel: shouldRefreshUrl
                            ? { ...profile.tunnel, public_url: recommendedPublicUrl(name, profile.tunnel.domain) }
                            : profile.tunnel,
                        };
                      });
                    }} />
                  </Field>
                  <Field label={t("Path")} wide>
                    <input value={selected.path} readOnly />
                  </Field>
                  <Field label={t("Tunnel")}>
                    <select value={selected.tunnel.type} onChange={(event) => updateSelected((profile) => ({ ...profile, tunnel: { ...profile.tunnel, type: event.target.value as WorkspaceProfile["tunnel"]["type"] } }))}>
                      <option value="cloudflare">{t("Cloudflare")}</option>
                      <option value="frp">{t("FRP (external)")}</option>
                    </select>
                  </Field>
                  {selected.tunnel.type === "cloudflare" ? (
                    <>
                      <Field label={t("Cloudflare mode")}>
                        <select value={selected.tunnel.cloudflare_mode} onChange={(event) => updateSelected((profile) => ({ ...profile, tunnel: { ...profile.tunnel, cloudflare_mode: event.target.value as WorkspaceProfile["tunnel"]["cloudflare_mode"] } }))}>
                          <option value="quick">{t("Quick tunnel")}</option>
                          <option value="named">{t("Named tunnel (recommended)")}</option>
                        </select>
                      </Field>
                      {selected.tunnel.cloudflare_mode === "named" && (
                        <>
                          <Field label={t("Domain")} wide>
                            <input
                              placeholder="example.com"
                              value={selected.tunnel.domain}
                              onChange={(event) => {
                                const domain = event.target.value;
                                updateSelected((profile) => ({
                                  ...profile,
                                  tunnel: {
                                    ...profile.tunnel,
                                    domain,
                                    public_url: recommendedPublicUrl(profile.name, domain),
                                  },
                                }));
                              }}
                              onBlur={() => updateSelected((profile) => {
                                const domain = normalizeDomain(profile.tunnel.domain);
                                return {
                                  ...profile,
                                  tunnel: {
                                    ...profile.tunnel,
                                    domain,
                                    public_url: profile.tunnel.public_url || recommendedPublicUrl(profile.name, domain),
                                  },
                                };
                              })}
                            />
                          </Field>
                          <Field label={t("Public URL (editable)")} wide>
                            <input placeholder="https://mcp.example.com" value={selected.tunnel.public_url} onChange={(event) => updateSelected((profile) => ({ ...profile, tunnel: { ...profile.tunnel, public_url: event.target.value } }))} />
                            <small className="field-hint">{t("Recommended from the workspace name and domain. You can edit it.")}</small>
                          </Field>
                          <SecretField label={t("Tunnel token")} value={selected.tunnel.cloudflare_token} placeholder={t("Paste the eyJ… token")} shown={reveal.cloudflare} onToggle={() => setReveal((state) => ({ ...state, cloudflare: !state.cloudflare }))} onChange={(value) => updateSelected((profile) => ({ ...profile, tunnel: { ...profile.tunnel, cloudflare_token: value } }))} t={t} />
                          <div className="named-route">
                            <span>{t("Cloudflare published route")}</span>
                            <code>{selected.tunnel.public_url || "https://mcp.example.com"} → http://127.0.0.1:{selected.runtime.local_port}</code>
                            <small>{t("Use the eyJ… value from Cloudflare's tunnel installation command as the Tunnel Token.")}</small>
                            <small>{t("Use a unique hostname, local port, and Tunnel Token for each workspace.")}</small>
                          </div>
                        </>
                      )}
                    </>
                  ) : (
                    <>
                      <Field label={t("FRP server")}><input placeholder="frp.example.com" value={selected.tunnel.frp_server} onChange={(event) => updateSelected((profile) => ({ ...profile, tunnel: { ...profile.tunnel, frp_server: event.target.value } }))} /></Field>
                      <Field label={t("FRP subdomain")}><input placeholder="mcp" value={selected.tunnel.frp_subdomain} onChange={(event) => updateSelected((profile) => ({ ...profile, tunnel: { ...profile.tunnel, frp_subdomain: event.target.value } }))} /></Field>
                    </>
                  )}
                </div>
              </section>

              <section className="card runtime-card">
                <CardTitle icon="bolt" title={t("Runtime")} />
                <div className="form-grid compact">
                  <Field label={t("Local port")}><input type="number" min={1024} max={65535} value={selected.runtime.local_port} onChange={(event) => updateSelected((profile) => ({ ...profile, runtime: { ...profile.runtime, local_port: Number(event.target.value) } }))} /></Field>
                  <Field label={t("Permission mode")}>
                    <select value={selected.runtime.permission_mode} onChange={(event) => updateSelected((profile) => ({ ...profile, runtime: { ...profile.runtime, permission_mode: event.target.value as WorkspaceProfile["runtime"]["permission_mode"] } }))}>
                      <option value="safe">{t("Safe")}</option>
                      <option value="trusted">{t("Trusted")}</option>
                      <option value="dangerous">{t("Dangerous")}</option>
                    </select>
                  </Field>
                </div>
                <div className="runtime-health">
                  <span className={`health-icon ${status?.state}`}>{status?.state === "running" ? "✓" : "○"}</span>
                  <div><strong>{status?.local_message}</strong><small>PID {status?.pid ?? "—"}</small></div>
                </div>
              </section>

              <section className="card auth-card">
                <CardTitle icon="lock" title={t("Authentication")} />
                <div className="segmented">
                  {(["oauth", "bearer"] as const).map((kind) => (
                    <button key={kind} className={selected.auth.type === kind ? "active" : ""} onClick={() => updateSelected((profile) => ({ ...profile, auth: { ...profile.auth, type: kind } }))}>
                      {t(kind === "oauth" ? "OAuth" : "Bearer token")}
                    </button>
                  ))}
                </div>
                {selected.auth.type === "oauth" && <><SecretField label={t("Authorization password")} value={selected.auth.oauth_password} shown={reveal.oauth} onToggle={() => setReveal((state) => ({ ...state, oauth: !state.oauth }))} onChange={(value) => updateSelected((profile) => ({ ...profile, auth: { ...profile.auth, oauth_password: value } }))} copyLabel={t("Copy password")} onCopy={() => copy(selected.auth.oauth_password)} t={t} /><p className="helper-text">{t("The MCP client registers automatically. Enter this password on the browser authorization page; there is no authorization code to copy manually.")}</p></>}
                {selected.auth.type === "bearer" && <SecretField label={t("Bearer token")} value={selected.auth.bearer_token} shown={reveal.bearer} onToggle={() => setReveal((state) => ({ ...state, bearer: !state.bearer }))} onChange={(value) => updateSelected((profile) => ({ ...profile, auth: { ...profile.auth, bearer_token: value } }))} copyLabel={t("Copy token")} onCopy={() => copy(selected.auth.bearer_token)} t={t} />}
              </section>

              <section className="card connection-card">
                <CardTitle icon="link" title={t("Connection")} />
                <UrlRow label={t("Local MCP URL")} value={status?.local_url ?? `http://127.0.0.1:${selected.runtime.local_port}/mcp`} onCopy={copy} />
                <UrlRow label={t("Public MCP URL")} value={publicEndpoint(selected, status?.public_url)} empty={t("No public URL yet")} onCopy={copy} />
                {selected.tunnel.type === "frp" && <button className="text-button" onClick={() => copy(frpSnippet(selected))}>{t("Copy FRP snippet")}</button>}
              </section>
            </div>}

            {activeTab === "logs" && (
              <section className="card logs-card logs-view" role="tabpanel">
                <CardTitle icon="terminal" title={t("Logs")} action={<button className="icon-button small" onClick={() => selectedId && api.logs(selectedId).then(setLogs)}>↻</button>} />
                <pre>{[logs.cloudflared && `[cloudflared.log]\n${logs.cloudflared}`, logs.stderr && `[stderr.log]\n${logs.stderr}`, logs.stdout && `[stdout.log]\n${logs.stdout}`].filter(Boolean).join("\n\n") || t("No logs yet.")}</pre>
              </section>
            )}

            {activeTab === "help" && (
              <section className="help-view" role="tabpanel">
                <div className="help-intro">
                  <span className="help-kicker">{t("Quick setup")}</span>
                  <h3>{t("What needs to be installed")}</h3>
                  <p>{t("Install these two command-line dependencies before starting a workspace.")}</p>
                </div>
                <div className="help-grid">
                  <article className="card help-card">
                    <span className="help-number">01</span>
                    <h4>{t("MCP runtime")}</h4>
                    <p>{t("Install uv so the app can launch coding-tools-mcp with uvx.")}</p>
                    <code>brew install uv</code>
                  </article>
                  <article className="card help-card">
                    <span className="help-number">02</span>
                    <h4>{t("Cloudflare Tunnel")}</h4>
                    <p>{t("Required for ChatGPT on the web to reach this Mac.")}</p>
                    <code>brew install cloudflared</code>
                  </article>
                  <article className="card help-card">
                    <span className="help-number">03</span>
                    <h4>{t("Connect to ChatGPT")}</h4>
                    <p>{t("Start this workspace and copy its public MCP URL. Add it to ChatGPT as a custom MCP app, then complete OAuth using the authorization password shown here.")}</p>
                    <code>{publicEndpoint(selected, status?.public_url) || "https://mcp.example.com/mcp"}</code>
                  </article>
                </div>
                <div className="card help-note">
                  <strong>{t("Fixed domain")}</strong>
                  <p>{t("For long-term use, create a Cloudflare Named Tunnel and route your hostname to the local port shown in Configuration.")}</p>
                  <code>http://127.0.0.1:{selected.runtime.local_port}</code>
                </div>
              </section>
            )}
          </>
        )}
      </section>
    </main>
  );
}

function PluginIcon() {
  return (
    <svg className="plugin-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" focusable="false">
      <path d="M12.252 12.331H7.749v1.002c0 1.014.821 1.835 1.835 1.835h.833a1.835 1.835 0 0 0 1.835-1.835zm-11.25-1.498a4.83 4.83 0 0 1 3.784-4.717A5.666 5.666 0 0 1 15.63 7.717 4 4 0 0 1 17 15.13a.665.665 0 1 1-.666-1.15A2.669 2.669 0 0 0 15 8.999a.665.665 0 0 1-.665-.666 4.335 4.335 0 0 0-8.313-1.725L5.9 6.92a.67.67 0 0 1-.448.423l-.092.02a3.502 3.502 0 0 0-1.783 6.147l.156.124.099.092a.665.665 0 0 1-.782 1.04l-.116-.068-.215-.171a4.82 4.82 0 0 1-1.717-3.695m12.58 2.5a3.164 3.164 0 0 1-2.917 3.155V17.5a.665.665 0 0 1-1.33 0v-1.012a3.164 3.164 0 0 1-2.916-3.155v-1.667c0-.367.298-.665.665-.665h.585v-1a.665.665 0 0 1 1.33 0v1h2.003v-1a.665.665 0 0 1 1.33 0v1h.585c.367 0 .665.298.665.665z" />
    </svg>
  );
}

function TabIcon({ name }: { name: "configuration" | "logs" | "help" }) {
  if (name === "configuration") {
    return (
      <svg className="tab-icon" viewBox="0 0 20 20" aria-hidden="true">
        <path d="M3 5h7M14 5h3M3 10h2M9 10h8M3 15h9M16 15h1M10 3v4M5 8v4M12 13v4" />
      </svg>
    );
  }
  if (name === "logs") {
    return (
      <svg className="tab-icon" viewBox="0 0 20 20" aria-hidden="true">
        <path d="m4 6 3 3-3 3M9 13h6M3 3.5h14v13H3z" />
      </svg>
    );
  }
  return (
    <svg className="tab-icon" viewBox="0 0 20 20" aria-hidden="true">
      <path d="M10 14.5v.01M7.8 7.3a2.3 2.3 0 1 1 3.25 2.1c-.7.34-1.05.72-1.05 1.6M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Z" />
    </svg>
  );
}

function CardTitle({ icon, title, action }: { icon: string; title: string; action?: React.ReactNode }) {
  const symbols: Record<string, string> = { folder: "▱", bolt: "ϟ", lock: "⌾", link: "↗", terminal: ">_" };
  return <div className="card-title"><span className={`card-icon ${icon}`}>{symbols[icon]}</span><h3>{title}</h3>{action && <div className="card-action">{action}</div>}</div>;
}

function Field({ label, children, wide = false }: { label: string; children: React.ReactNode; wide?: boolean }) {
  return <label className={`field ${wide ? "wide" : ""}`}><span>{label}</span>{children}</label>;
}

function SecretField({ label, value, placeholder, shown, onToggle, onChange, copyLabel, onCopy, t }: { label: string; value: string; placeholder?: string; shown?: boolean; onToggle: () => void; onChange: (value: string) => void; copyLabel?: string; onCopy?: () => void; t: (text: string) => string }) {
  return <Field label={label} wide><div className="secret-input"><input type={shown ? "text" : "password"} value={value} placeholder={placeholder} autoComplete="off" spellCheck={false} onChange={(event) => onChange(event.target.value)} /><button type="button" onClick={onToggle}>{t(shown ? "Hide" : "Reveal")}</button>{onCopy && <button type="button" onClick={onCopy}>{copyLabel}</button>}</div></Field>;
}

function UrlRow({ label, value, empty, onCopy }: { label: string; value: string; empty?: string; onCopy: (value: string) => void }) {
  return <div className="url-row"><div><span>{label}</span><code>{value || empty}</code></div><button className="copy-icon" disabled={!value} onClick={() => onCopy(value)}>⌘C</button></div>;
}

export default App;
