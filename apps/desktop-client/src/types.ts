export type TunnelType = "cloudflare" | "frp";
export type CloudflareMode = "quick" | "named";
export type AuthType = "oauth" | "bearer";
export type PermissionMode = "safe" | "trusted" | "dangerous" | "host";
export type RuntimeState = "stopped" | "starting" | "running" | "stopping" | "error";

export interface TunnelConfig {
  type: TunnelType;
  domain: string;
  public_url: string;
  frp_server: string;
  frp_subdomain: string;
  cloudflare_mode: CloudflareMode;
  cloudflare_token: string;
}

export interface AuthConfig {
  type: AuthType;
  oauth_password: string;
  oauth_token_secret: string;
  bearer_token: string;
}

export interface RuntimeConfig {
  server_name_prefix: string;
  local_port: number;
  permission_mode: PermissionMode;
  file_access_scope: "workspace";
  allowed_paths: string[];
  default_search_path: string;
  environment_variables: EnvironmentVariable[];
}

export interface GatewayConfig {
  server_name_prefix: string;
  local_port: number;
  tunnel: TunnelConfig;
  auth: AuthConfig;
  local_capability_roots: string[];
}

export interface ProjectProfile {
  id: string;
  name: string;
  path: string;
  permission_mode: PermissionMode;
  file_access_scope: "workspace";
  allowed_paths: string[];
  environment_variables: EnvironmentVariable[];
}

export interface EnvironmentVariable {
  name: string;
  value: string;
}

export interface WorkspaceProfile {
  id: string;
  name: string;
  path: string;
  tunnel: TunnelConfig;
  auth: AuthConfig;
  runtime: RuntimeConfig;
}

export interface RuntimeStatus {
  state: RuntimeState;
  pid: number | null;
  server_name: string;
  local_message: string;
  public_message: string;
  public_url: string;
  local_url: string;
}

export interface DesktopSnapshot {
  language: "en" | "zh-CN";
  home_directory: string | null;
  gateway: GatewayConfig;
  projects: ProjectProfile[];
  migration_warning: string | null;
  profiles: WorkspaceProfile[];
  statuses: Record<string, RuntimeStatus>;
  dependencies: DependencyStatus;
  runtime_state: Record<string, RuntimeStateSnapshot>;
}

export interface RuntimeApproval {
  approval_id: string;
  tool_name: string;
  permission: string;
  reason: string;
  arguments: string;
  status: "pending" | "approved" | "denied" | "expired" | "consumed";
  expires_at: number;
  created_at: number;
}

export interface RuntimeStateSnapshot {
  available: boolean;
  workspace_id: string;
  approvals: RuntimeApproval[];
  warning: string | null;
}


export interface DependencyStatus {
  uv: boolean;
  cloudflared: boolean;
  runtime_ready: boolean;
  runtime_version: string | null;
}

export interface LogBundle {
  cloudflared: string;
  stderr: string;
  stdout: string;
}
