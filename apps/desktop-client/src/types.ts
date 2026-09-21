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
  computer_enabled: boolean;
  local_port: number;
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
  profiles: WorkspaceProfile[];
  statuses: Record<string, RuntimeStatus>;
  dependencies: DependencyStatus;
  workflow: Record<string, WorkflowSnapshot>;
}

export interface WorkflowTask {
  task_id: string;
  title: string;
  status: string;
  revision: number;
  updated_at: number;
  plan_completed: number;
  plan_total: number;
}

export interface WorkflowCheckpoint {
  checkpoint_id: string;
  label: string;
  file_count: number;
  created_at: number;
}

export interface WorkflowCheck {
  check_run_id: string;
  check_id: string;
  status: string;
  task_id: string | null;
  created_at: number;
}

export interface WorkflowReview {
  review_id: string;
  status: string;
  finding_count: number;
  task_id: string | null;
  updated_at: number;
}

export interface WorkflowApproval {
  approval_id: string;
  tool_name: string;
  permission: string;
  reason: string;
  arguments: string;
  status: "pending" | "approved" | "denied" | "expired" | "consumed";
  expires_at: number;
  created_at: number;
}

export interface WorkflowWorktree {
  worktree_id: string;
  path: string;
}

export interface WorkflowSnapshot {
  available: boolean;
  workspace_id: string;
  tasks: WorkflowTask[];
  checkpoints: WorkflowCheckpoint[];
  checks: WorkflowCheck[];
  reviews: WorkflowReview[];
  approvals: WorkflowApproval[];
  worktrees: WorkflowWorktree[];
  computer_sessions: ComputerSession[];
  warning: string | null;
}

export interface ComputerSession {
  session_id: string;
  app_name: string;
  access: "observe" | "control";
  status: string;
  expires_at: number;
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
