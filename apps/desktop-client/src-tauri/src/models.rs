use rand::distr::{Alphanumeric, SampleString};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use uuid::Uuid;

pub const MCP_ENDPOINT_PATH: &str = "/mcp";

fn default_tunnel_type() -> String {
    "cloudflare".into()
}
fn default_cloudflare_mode() -> String {
    "quick".into()
}
fn default_auth_type() -> String {
    "oauth".into()
}
fn default_permission_mode() -> String {
    "trusted".into()
}
fn default_file_access_scope() -> String {
    "workspace".into()
}
fn default_server_name_prefix() -> String {
    "www".into()
}

pub(crate) fn user_home_directory() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .and_then(|home| std::fs::canonicalize(home).ok())
}
fn default_allowed_paths() -> Vec<String> {
    Vec::new()
}

fn default_environment_variables() -> Vec<EnvironmentVariable> {
    Vec::new()
}
fn default_port() -> u16 {
    28766
}

fn new_secret() -> String {
    Alphanumeric.sample_string(&mut rand::rng(), 48)
}

fn new_oauth_token_secret() -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let bytes: [u8; 32] = rand::random();
    let mut secret = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        secret.push(HEX[(byte >> 4) as usize] as char);
        secret.push(HEX[(byte & 0x0f) as usize] as char);
    }
    secret
}

fn valid_oauth_token_secret(secret: &str) -> bool {
    secret.len() >= 64
        && secret.len() % 2 == 0
        && secret.bytes().all(|byte| byte.is_ascii_hexdigit())
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TunnelConfig {
    #[serde(default = "default_tunnel_type")]
    pub r#type: String,
    #[serde(default)]
    pub domain: String,
    #[serde(default)]
    pub public_url: String,
    #[serde(default)]
    pub frp_server: String,
    #[serde(default)]
    pub frp_subdomain: String,
    #[serde(default = "default_cloudflare_mode")]
    pub cloudflare_mode: String,
    #[serde(default)]
    pub cloudflare_token: String,
}

impl Default for TunnelConfig {
    fn default() -> Self {
        Self {
            r#type: default_tunnel_type(),
            domain: String::new(),
            public_url: String::new(),
            frp_server: String::new(),
            frp_subdomain: String::new(),
            cloudflare_mode: default_cloudflare_mode(),
            cloudflare_token: String::new(),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AuthConfig {
    #[serde(default = "default_auth_type")]
    pub r#type: String,
    #[serde(default = "new_secret")]
    pub oauth_password: String,
    #[serde(default = "new_oauth_token_secret")]
    pub oauth_token_secret: String,
    #[serde(default = "new_secret")]
    pub bearer_token: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct EnvironmentVariable {
    pub name: String,
    #[serde(default)]
    pub value: String,
}

impl Default for AuthConfig {
    fn default() -> Self {
        Self {
            r#type: default_auth_type(),
            oauth_password: new_secret(),
            oauth_token_secret: new_oauth_token_secret(),
            bearer_token: new_secret(),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RuntimeConfig {
    #[serde(default)]
    pub computer_enabled: bool,
    #[serde(default = "default_server_name_prefix")]
    pub server_name_prefix: String,
    #[serde(default = "default_port")]
    pub local_port: u16,
    #[serde(default = "default_permission_mode")]
    pub permission_mode: String,
    #[serde(default = "default_file_access_scope")]
    pub file_access_scope: String,
    #[serde(default = "default_allowed_paths")]
    pub allowed_paths: Vec<String>,
    #[serde(default = "default_environment_variables")]
    pub environment_variables: Vec<EnvironmentVariable>,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            computer_enabled: false,
            server_name_prefix: default_server_name_prefix(),
            local_port: default_port(),
            permission_mode: default_permission_mode(),
            file_access_scope: default_file_access_scope(),
            allowed_paths: default_allowed_paths(),
            environment_variables: default_environment_variables(),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct WorkspaceProfile {
    pub id: String,
    pub name: String,
    pub path: String,
    #[serde(default)]
    pub tunnel: TunnelConfig,
    #[serde(default)]
    pub auth: AuthConfig,
    #[serde(default)]
    pub runtime: RuntimeConfig,
}

impl WorkspaceProfile {
    pub fn new(path: String, port: u16) -> Result<Self, String> {
        let cleaned = Path::new(path.trim());
        if !cleaned.is_dir() {
            return Err(format!(
                "Workspace directory does not exist: {}",
                cleaned.display()
            ));
        }
        let name = cleaned
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("Workspace")
            .to_string();
        Ok(Self {
            id: Uuid::new_v4().simple().to_string(),
            name,
            path: cleaned.to_string_lossy().to_string(),
            tunnel: TunnelConfig::default(),
            auth: AuthConfig::default(),
            runtime: RuntimeConfig {
                local_port: port,
                ..RuntimeConfig::default()
            },
        })
    }

    pub fn new_full_access(path: String, port: u16) -> Result<Self, String> {
        let mut profile = Self::new(path, port)?;
        profile.enable_full_access();
        Ok(profile)
    }

    pub fn enable_full_access(&mut self) {
        self.runtime.permission_mode = "host".into();
    }

    pub fn validate(&self) -> Result<(), String> {
        if !Path::new(&self.path).is_dir() {
            return Err(format!("Workspace directory does not exist: {}", self.path));
        }
        let workspace = std::fs::canonicalize(&self.path).map_err(|error| error.to_string())?;
        if workspace.parent().is_none() {
            return Err("Choose a project folder instead of the filesystem root.".into());
        }
        if user_home_directory().is_some_and(|home| workspace == home)
            && self.runtime.permission_mode != "host"
        {
            return Err("Choose a project folder inside your home directory instead of the home directory itself.".into());
        }
        if self.name.trim().is_empty() {
            return Err("Workspace name cannot be empty.".into());
        }
        let server_name_prefix = self.runtime.server_name_prefix.trim();
        if server_name_prefix.is_empty()
            || server_name_prefix.chars().count() > 24
            || !server_name_prefix
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_'))
        {
            return Err(
                "MCP name prefix must be 1-24 characters using letters, numbers, '.', '-' or '_'."
                    .into(),
            );
        }
        if self.runtime.local_port < 1024 {
            return Err("Local port must be between 1024 and 65535.".into());
        }
        if !matches!(
            self.runtime.permission_mode.as_str(),
            "safe" | "trusted" | "dangerous" | "host"
        ) {
            return Err("Unknown permission mode.".into());
        }
        if self.runtime.file_access_scope != "workspace" {
            return Err("Unknown file access scope.".into());
        }
        for allowed in &self.runtime.allowed_paths {
            let path = Path::new(allowed.trim());
            if !path.is_absolute() {
                return Err(format!(
                    "Allowed folder must be an absolute path: {allowed}"
                ));
            }
            if !path.is_dir() {
                return Err(format!("Allowed folder does not exist: {allowed}"));
            }
            if path.parent().is_none() {
                return Err(format!(
                    "Filesystem root cannot be used as an allowed folder: {allowed}"
                ));
            }
            let resolved = std::fs::canonicalize(path).map_err(|error| error.to_string())?;
            if user_home_directory().is_some_and(|home| resolved == home)
                && self.runtime.permission_mode != "host"
            {
                return Err("The user home directory can only be added as an allowed folder in Full Access mode.".into());
            }
        }
        let mut environment_names = std::collections::HashSet::new();
        for variable in &self.runtime.environment_variables {
            let name = variable.name.trim();
            if name.is_empty()
                || !name.bytes().enumerate().all(|(index, byte)| {
                    byte == b'_'
                        || byte.is_ascii_alphanumeric() && (index > 0 || byte.is_ascii_alphabetic())
                })
            {
                return Err(format!(
                    "Invalid environment variable name: {}",
                    variable.name
                ));
            }
            let upper = name.to_ascii_uppercase();
            let reserved = matches!(
                upper.as_str(),
                "PATH"
                    | "HOME"
                    | "USERPROFILE"
                    | "SHELL"
                    | "TMPDIR"
                    | "TEMP"
                    | "TMP"
                    | "SSH_AUTH_SOCK"
                    | "PYTHONPATH"
                    | "PYTHONHOME"
                    | "NODE_OPTIONS"
                    | "RUBYOPT"
                    | "BASH_ENV"
                    | "ENV"
                    | "ZDOTDIR"
            ) || upper.starts_with("CODING_TOOLS_MCP_")
                || upper.starts_with("DYLD_")
                || upper.starts_with("LD_");
            if reserved {
                return Err(format!("Environment variable is reserved: {name}"));
            }
            if variable.value.is_empty() {
                return Err(format!(
                    "Environment variable value cannot be empty: {name}"
                ));
            }
            if !environment_names.insert(upper) {
                return Err(format!("Duplicate environment variable: {name}"));
            }
        }
        if !matches!(self.auth.r#type.as_str(), "oauth" | "bearer") {
            return Err("Unknown authentication type.".into());
        }
        if self.auth.r#type == "oauth" && self.auth.oauth_password.trim().is_empty() {
            return Err("OAuth mode requires an authorization password.".into());
        }
        if self.auth.r#type == "bearer" && self.auth.bearer_token.trim().is_empty() {
            return Err("Bearer mode requires a token.".into());
        }
        match self.tunnel.r#type.as_str() {
            "cloudflare" => {
                if !matches!(self.tunnel.cloudflare_mode.as_str(), "quick" | "named") {
                    return Err("Unknown Cloudflare mode.".into());
                }
                if self.tunnel.cloudflare_mode == "named" {
                    if self.tunnel.cloudflare_token.trim().is_empty() {
                        return Err("A named Cloudflare tunnel requires a Tunnel Token.".into());
                    }
                    let url = self.tunnel.public_url.trim();
                    let origin = url
                        .strip_prefix("https://")
                        .unwrap_or_default()
                        .trim_end_matches('/');
                    if origin.is_empty()
                        || origin.contains(['/', '?', '#'])
                        || origin.chars().any(char::is_whitespace)
                    {
                        return Err("Public URL must be an HTTPS origin without a path.".into());
                    }
                }
            }
            "frp" => {
                if self.tunnel.frp_server.trim().is_empty()
                    || self.tunnel.frp_subdomain.trim().is_empty()
                {
                    return Err("FRP requires a server domain and subdomain.".into());
                }
            }
            _ => return Err("Unknown tunnel type.".into()),
        }
        Ok(())
    }

    pub fn public_url(&self) -> String {
        if self.tunnel.r#type == "frp" {
            format!(
                "https://{}.{}",
                self.tunnel.frp_subdomain.trim(),
                self.tunnel.frp_server.trim()
            )
        } else if self.tunnel.cloudflare_mode == "named" {
            self.tunnel.public_url.trim_end_matches('/').to_string()
        } else {
            String::new()
        }
    }
}

impl AuthConfig {
    pub(crate) fn repair_oauth_token_secret(&mut self) -> bool {
        if valid_oauth_token_secret(&self.oauth_token_secret) {
            return false;
        }
        self.oauth_token_secret = new_oauth_token_secret();
        true
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct RuntimeStatus {
    pub state: String,
    pub pid: Option<u32>,
    pub server_name: String,
    pub local_message: String,
    pub public_message: String,
    pub public_url: String,
    pub local_url: String,
}

impl RuntimeStatus {
    pub fn is_active(&self) -> bool {
        self.pid.is_some() || matches!(self.state.as_str(), "starting" | "stopping")
    }

    pub fn stopped(port: u16) -> Self {
        Self {
            state: "stopped".into(),
            pid: None,
            server_name: String::new(),
            local_message: "Not running".into(),
            public_message: "Unknown".into(),
            public_url: String::new(),
            local_url: format!("http://127.0.0.1:{port}{MCP_ENDPOINT_PATH}"),
        }
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct LogBundle {
    pub cloudflared: String,
    pub stderr: String,
    pub stdout: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn workspace_validation_explains_home_root_rejection_but_accepts_children() {
        let temporary = tempfile::tempdir().unwrap();
        let mut profile =
            WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766).unwrap();
        assert!(profile.validate().is_ok());
        let home = std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .unwrap();
        profile.path = Path::new(&home).to_string_lossy().into_owned();
        assert!(profile
            .validate()
            .unwrap_err()
            .contains("home directory itself"));
        let root = Path::new(&profile.path)
            .ancestors()
            .last()
            .unwrap()
            .to_path_buf();
        profile.path = root.to_string_lossy().into_owned();
        assert!(profile.validate().unwrap_err().contains("filesystem root"));
    }

    #[test]
    fn full_access_profile_uses_selected_workspace_without_synthetic_folders() {
        let workspace = tempfile::tempdir().unwrap();
        let profile = WorkspaceProfile::new_full_access(
            workspace.path().to_string_lossy().into_owned(),
            28766,
        )
        .unwrap();
        assert_eq!(
            profile.name,
            workspace.path().file_name().unwrap().to_string_lossy()
        );
        assert_eq!(profile.runtime.permission_mode, "host");
        assert_eq!(
            std::fs::canonicalize(&profile.path).unwrap(),
            std::fs::canonicalize(workspace.path()).unwrap()
        );
        assert!(profile.runtime.allowed_paths.is_empty());
        assert!(profile.validate().is_ok());
    }

    #[test]
    fn home_can_be_explicitly_allowed_only_for_full_access() {
        let Some(home) = user_home_directory() else {
            return;
        };
        let temporary = tempfile::tempdir().unwrap();
        let mut profile =
            WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766).unwrap();
        profile.runtime.allowed_paths = vec![home.to_string_lossy().into_owned()];
        assert!(profile.validate().unwrap_err().contains("Full Access"));
        profile.runtime.permission_mode = "host".into();
        assert!(profile.validate().is_ok());
    }

    #[test]
    fn environment_variable_validation_accepts_api_keys() {
        let temporary = tempfile::tempdir().unwrap();
        let mut profile =
            WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766).unwrap();
        profile.runtime.environment_variables = vec![EnvironmentVariable {
            name: "OPENAI_API_KEY".into(),
            value: "secret".into(),
        }];
        assert!(profile.validate().is_ok());
    }

    #[test]
    fn environment_variable_validation_rejects_unsafe_or_duplicate_names() {
        let temporary = tempfile::tempdir().unwrap();
        let mut profile =
            WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766).unwrap();
        for name in [
            "9INVALID",
            "PATH",
            "CODING_TOOLS_MCP_AUTH_TOKEN",
            "DYLD_INSERT_LIBRARIES",
        ] {
            profile.runtime.environment_variables = vec![EnvironmentVariable {
                name: name.into(),
                value: "secret".into(),
            }];
            assert!(profile.validate().is_err(), "{name} should be rejected");
        }
        profile.runtime.environment_variables = vec![
            EnvironmentVariable {
                name: "API_KEY".into(),
                value: "one".into(),
            },
            EnvironmentVariable {
                name: "api_key".into(),
                value: "two".into(),
            },
        ];
        assert!(profile.validate().unwrap_err().contains("Duplicate"));
    }

    #[test]
    fn public_urls_are_deterministic() {
        let mut profile = WorkspaceProfile::new(
            std::env::current_dir()
                .unwrap()
                .to_string_lossy()
                .to_string(),
            28766,
        )
        .unwrap();
        profile.tunnel.r#type = "frp".into();
        profile.tunnel.frp_server = "example.com".into();
        profile.tunnel.frp_subdomain = "code".into();
        assert_eq!(profile.public_url(), "https://code.example.com");
    }

    #[test]
    fn named_tunnels_require_a_clean_https_origin() {
        let mut profile = WorkspaceProfile::new(
            std::env::current_dir()
                .unwrap()
                .to_string_lossy()
                .to_string(),
            28766,
        )
        .unwrap();
        profile.tunnel.cloudflare_mode = "named".into();
        profile.tunnel.cloudflare_token = "eyJ-example".into();
        profile.tunnel.public_url = "https://mcp.example.com/path".into();
        assert!(profile.validate().is_err());

        profile.tunnel.public_url = "https://mcp.example.com/".into();
        assert!(profile.validate().is_ok());
        assert_eq!(profile.public_url(), "https://mcp.example.com");
    }

    #[test]
    fn host_permission_mode_is_explicitly_supported() {
        let mut profile = WorkspaceProfile::new(
            std::env::current_dir()
                .unwrap()
                .to_string_lossy()
                .to_string(),
            28766,
        )
        .unwrap();
        profile.runtime.permission_mode = "host".into();
        assert!(profile.validate().is_ok());
    }

    #[test]
    fn oauth_token_secrets_are_hex_encoded_32_byte_keys() {
        let auth = AuthConfig::default();
        assert_eq!(auth.oauth_token_secret.len(), 64);
        assert!(auth
            .oauth_token_secret
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit()));
    }

    #[test]
    fn invalid_oauth_token_secrets_are_repaired_without_rotating_valid_keys() {
        let mut auth = AuthConfig {
            oauth_token_secret: "not-hex".into(),
            ..AuthConfig::default()
        };
        assert!(auth.repair_oauth_token_secret());
        let repaired = auth.oauth_token_secret.clone();
        assert!(!auth.repair_oauth_token_secret());
        assert_eq!(auth.oauth_token_secret, repaired);
    }
}
