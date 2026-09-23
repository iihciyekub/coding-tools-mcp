import { invoke } from "@tauri-apps/api/core";
import type { DesktopSnapshot, GatewayConfig, LogBundle, RuntimeStatus, WorkspaceProfile } from "./types";

export const api = {
  snapshot: () => invoke<DesktopSnapshot>("desktop_snapshot"),
  decideApproval: (profileId: string, approvalId: string, approved: boolean) =>
    invoke<void>("decide_approval", { profileId, approvalId, approved }),
  createProfile: (path: string) => invoke<WorkspaceProfile>("create_profile", { path }),
  createFullAccessProfile: (path: string) => invoke<WorkspaceProfile>("create_full_access_profile", { path }),
  saveProfile: (profile: WorkspaceProfile) => invoke<WorkspaceProfile>("save_profile", { profile }),
  saveLocalCapabilityRoots: (roots: string[]) => invoke<GatewayConfig>("save_local_capability_roots", { roots }),
  saveAutoDiscoverLocalCapabilities: (enabled: boolean) => invoke<GatewayConfig>("save_auto_discover_local_capabilities", { enabled }),
  saveServerNamePrefix: (prefix: string) => invoke<GatewayConfig>("save_server_name_prefix", { prefix }),
  deleteProfile: (profileId: string) => invoke<void>("delete_profile", { profileId }),
  startProfile: (profileId: string) => invoke<RuntimeStatus>("start_profile", { profileId }),
  stopProfile: (profileId: string) => invoke<RuntimeStatus>("stop_profile", { profileId }),
  stopAllProfiles: () => invoke<Record<string, RuntimeStatus>>("stop_all_profiles"),
  status: (profileId: string) => invoke<RuntimeStatus>("profile_status", { profileId }),
  logs: (profileId: string) => invoke<LogBundle>("profile_logs", { profileId }),
  openLogs: (profileId: string) => invoke<void>("open_logs", { profileId }),
  openProjectWindow: (profileId: string) => invoke<void>("open_project_window", { profileId }),
  setLanguage: (language: "en" | "zh-CN") => invoke<void>("set_language", { language }),
  pickWorkspaceFolder: () => invoke<string | null>("pick_workspace_folder"),
  pickAllowedFolder: () => invoke<string | null>("pick_allowed_folder"),
  openResource: (target: "uv" | "cloudflared" | "github") => invoke<void>("open_resource", { target }),
  installResource: (target: "uv" | "cloudflared") => invoke<string>("install_resource", { target }),
  repairDependencies: () => invoke<string>("repair_dependencies"),
  prepareRuntime: (repair = false) => invoke<string>("prepare_runtime", { repair }),
  quit: () => invoke<void>("quit_app"),
};
