import { invoke } from "@tauri-apps/api/core";
import type { DesktopSnapshot, LogBundle, RuntimeStatus, WorkspaceProfile } from "./types";

export const api = {
  snapshot: () => invoke<DesktopSnapshot>("desktop_snapshot"),
  createProfile: (path: string) => invoke<WorkspaceProfile>("create_profile", { path }),
  saveProfile: (profile: WorkspaceProfile) => invoke<WorkspaceProfile>("save_profile", { profile }),
  deleteProfile: (profileId: string) => invoke<void>("delete_profile", { profileId }),
  startProfile: (profileId: string) => invoke<RuntimeStatus>("start_profile", { profileId }),
  stopProfile: (profileId: string) => invoke<RuntimeStatus>("stop_profile", { profileId }),
  status: (profileId: string) => invoke<RuntimeStatus>("profile_status", { profileId }),
  logs: (profileId: string) => invoke<LogBundle>("profile_logs", { profileId }),
};
