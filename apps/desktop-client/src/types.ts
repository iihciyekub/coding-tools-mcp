export type TunnelType = "cloudflare" | "frp";
export type CloudflareMode = "quick" | "named";
export type AuthType = "oauth" | "bearer";
export type PermissionMode = "safe" | "trusted" | "dangerous";
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
  local_port: number;
  permission_mode: PermissionMode;
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
  local_message: string;
  public_message: string;
  public_url: string;
  local_url: string;
}

export interface DesktopSnapshot {
  profiles: WorkspaceProfile[];
  statuses: Record<string, RuntimeStatus>;
}

export interface LogBundle {
  cloudflared: string;
  stderr: string;
  stdout: string;
}
