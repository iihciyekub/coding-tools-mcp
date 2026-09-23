use crate::models::{user_home_directory, EnvironmentVariable, GatewayConfig, WorkspaceProfile};
use keyring::Entry;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

fn keyring_service() -> &'static str {
    match option_env!("CODING_TOOLS_MCP_BUILD_CHANNEL") {
        Some("preview") => "coding-tools-mcp-desktop-preview",
        _ => "coding-tools-mcp-desktop",
    }
}

fn storage_directory_name() -> &'static str {
    match option_env!("CODING_TOOLS_MCP_BUILD_CHANNEL") {
        Some("preview") => ".coding-tools-mcp-desktop-preview",
        _ => ".coding-tools-mcp-desktop",
    }
}

#[derive(Default, Deserialize, Serialize)]
struct ProfileDocument {
    #[serde(default)]
    language: String,
    #[serde(default)]
    gateway: Option<GatewayConfig>,
    #[serde(default)]
    profiles: Vec<WorkspaceProfile>,
}

#[derive(Clone, Default, Deserialize, Serialize)]
struct GatewaySecrets {
    #[serde(default)]
    cloudflare_token: String,
    #[serde(default)]
    oauth_password: String,
    #[serde(default)]
    oauth_token_secret: String,
    #[serde(default)]
    bearer_token: String,
}

impl GatewaySecrets {
    fn from_gateway(gateway: &GatewayConfig) -> Self {
        Self {
            cloudflare_token: gateway.tunnel.cloudflare_token.clone(),
            oauth_password: gateway.auth.oauth_password.clone(),
            oauth_token_secret: gateway.auth.oauth_token_secret.clone(),
            bearer_token: gateway.auth.bearer_token.clone(),
        }
    }

    fn apply(self, gateway: &mut GatewayConfig) {
        gateway.tunnel.cloudflare_token = self.cloudflare_token;
        gateway.auth.oauth_password = self.oauth_password;
        gateway.auth.oauth_token_secret = self.oauth_token_secret;
        gateway.auth.bearer_token = self.bearer_token;
    }
}

#[derive(Clone, Default, Deserialize, Serialize)]
struct ProfileSecrets {
    #[serde(default)]
    cloudflare_token: String,
    #[serde(default)]
    oauth_password: String,
    #[serde(default)]
    oauth_token_secret: String,
    #[serde(default)]
    bearer_token: String,
    #[serde(default)]
    environment_variables: Vec<EnvironmentVariable>,
}

impl ProfileSecrets {
    fn from_profile(profile: &WorkspaceProfile) -> Self {
        Self {
            cloudflare_token: profile.tunnel.cloudflare_token.clone(),
            oauth_password: profile.auth.oauth_password.clone(),
            oauth_token_secret: profile.auth.oauth_token_secret.clone(),
            bearer_token: profile.auth.bearer_token.clone(),
            environment_variables: profile.runtime.environment_variables.clone(),
        }
    }

    fn apply(self, profile: &mut WorkspaceProfile) {
        profile.tunnel.cloudflare_token = self.cloudflare_token;
        profile.auth.oauth_password = self.oauth_password;
        profile.auth.oauth_token_secret = self.oauth_token_secret;
        profile.auth.bearer_token = self.bearer_token;
        profile.runtime.environment_variables = self.environment_variables;
    }
}

pub struct ProfileStore {
    home: PathBuf,
    language: String,
    gateway: GatewayConfig,
    profiles: Vec<WorkspaceProfile>,
    migration_warning: Option<String>,
}

impl ProfileStore {
    pub fn open_default() -> Result<Self, String> {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .ok_or("Could not resolve the user home directory.")?
            .join(storage_directory_name());
        Self::open(home)
    }

    pub fn open(home: PathBuf) -> Result<Self, String> {
        fs::create_dir_all(home.join("state")).map_err(|error| error.to_string())?;
        restrict(&home, 0o700)?;
        restrict(&home.join("state"), 0o700)?;
        let profile_path = home.join("profiles.json");
        let mut document: ProfileDocument = if profile_path.exists() {
            serde_json::from_slice(&fs::read(&profile_path).map_err(|error| error.to_string())?)
                .map_err(|error| format!("Could not read profiles.json: {error}"))?
        } else {
            ProfileDocument::default()
        };
        let mut normalized_legacy_profiles = false;
        for profile in &mut document.profiles {
            if profile.auth.r#type == "noauth" {
                profile.auth.r#type = "oauth".into();
                normalized_legacy_profiles = true;
            }
            if profile.runtime.permission_mode != "host"
                && profile.runtime.permission_mode != "trusted"
            {
                profile.runtime.permission_mode = "trusted".into();
                normalized_legacy_profiles = true;
            }
        }
        normalized_legacy_profiles |= normalize_legacy_file_access(&mut document.profiles);

        let legacy = Self::legacy_secrets(&home)?;
        let profile_ids = document
            .profiles
            .iter()
            .map(|profile| profile.id.as_str())
            .collect::<std::collections::HashSet<_>>();
        let mut migrated_all = !legacy.is_empty()
            && legacy
                .keys()
                .all(|profile_id| profile_ids.contains(profile_id.as_str()));
        for profile in &mut document.profiles {
            let can_repair_secret = match load_secrets(&profile.id) {
                Ok(Some(secrets)) => {
                    secrets.apply(profile);
                    true
                }
                Ok(None) => {
                    if let Some(secrets) = legacy.get(&profile.id) {
                        secrets.clone().apply(profile);
                        if save_secrets(profile).is_err() {
                            migrated_all = false;
                        }
                    }
                    true
                }
                Err(_) => {
                    migrated_all = false;
                    if let Some(secrets) = legacy.get(&profile.id) {
                        secrets.clone().apply(profile);
                        true
                    } else {
                        false
                    }
                }
            };
            if can_repair_secret && profile.auth.repair_oauth_token_secret() {
                save_secrets(profile)?;
            }
        }
        if migrated_all {
            let _ = fs::remove_file(home.join("secrets.json"));
        }
        let migrating_gateway = document.gateway.is_none();
        let mut gateway = document.gateway.take().unwrap_or_else(|| {
            document
                .profiles
                .first()
                .map(GatewayConfig::from_workspace_profile)
                .unwrap_or_default()
        });
        let migration_warning = if migrating_gateway
            && document
                .profiles
                .iter()
                .skip(1)
                .any(|profile| gateway_settings_differ(&gateway, profile))
        {
            Some(
                "Legacy workspaces used different MCP/Tunnel settings. The first workspace was chosen as the shared Gateway configuration; review Gateway settings before the next start."
                    .to_string(),
            )
        } else {
            None
        };
        match load_gateway_secrets() {
            Ok(Some(secrets)) => secrets.apply(&mut gateway),
            Ok(None) => {
                save_gateway_secrets(&gateway)?;
            }
            Err(error) => {
                return Err(format!(
                    "Could not load Gateway secrets from the system keychain: {error}"
                ));
            }
        }
        if gateway.repair_oauth_token_secret() {
            save_gateway_secrets(&gateway)?;
        }
        for profile in &mut document.profiles {
            gateway.apply_to_workspace_profile(profile);
        }
        let store = Self {
            home,
            language: normalize_language(&document.language).to_string(),
            gateway,
            profiles: document.profiles,
            migration_warning,
        };
        if normalized_legacy_profiles || migrating_gateway {
            store.persist()?;
        }
        Ok(store)
    }

    fn legacy_secrets(home: &Path) -> Result<HashMap<String, ProfileSecrets>, String> {
        let path = home.join("secrets.json");
        if !path.exists() {
            return Ok(HashMap::new());
        }
        serde_json::from_slice(&fs::read(path).map_err(|error| error.to_string())?)
            .map_err(|error| format!("Could not migrate secrets.json: {error}"))
    }

    pub fn profiles(&self) -> Vec<WorkspaceProfile> {
        self.profiles
            .iter()
            .cloned()
            .map(|mut profile| {
                self.gateway.apply_to_workspace_profile(&mut profile);
                profile
            })
            .collect()
    }

    pub fn gateway(&self) -> GatewayConfig {
        self.gateway.clone()
    }

    pub fn migration_warning(&self) -> Option<String> {
        self.migration_warning.clone()
    }

    pub fn project_registry_path(&self) -> PathBuf {
        self.home.join("project-registry.json")
    }

    pub fn language(&self) -> &str {
        &self.language
    }

    pub fn set_language(&mut self, language: &str) -> Result<(), String> {
        self.language = normalize_language(language).to_string();
        self.persist()
    }

    pub fn get(&self, id: &str) -> Option<WorkspaceProfile> {
        self.profiles
            .iter()
            .find(|profile| profile.id == id)
            .cloned()
            .map(|mut profile| {
                self.gateway.apply_to_workspace_profile(&mut profile);
                profile
            })
    }

    pub fn prepare_for_start(&mut self, id: &str) -> Result<WorkspaceProfile, String> {
        let candidate = self.get(id).ok_or("Workspace profile was not found.")?;
        validate_profile_uniqueness(&self.profiles, &candidate)?;
        if self.gateway.repair_oauth_token_secret() {
            save_gateway_secrets(&self.gateway)?;
        }
        let profile = self
            .profiles
            .iter_mut()
            .find(|profile| profile.id == id)
            .ok_or("Workspace profile was not found.")?;
        if profile.auth.repair_oauth_token_secret() {
            save_secrets(profile)?;
        }
        let mut prepared = profile.clone();
        self.gateway.apply_to_workspace_profile(&mut prepared);
        Ok(prepared)
    }

    pub fn next_port(&self) -> u16 {
        self.gateway.local_port
    }

    pub fn insert(&mut self, mut profile: WorkspaceProfile) -> Result<WorkspaceProfile, String> {
        self.gateway.apply_to_workspace_profile(&mut profile);
        profile.validate()?;
        if self.profiles.iter().any(|item| item.id == profile.id) {
            return Err("Workspace profile already exists.".into());
        }
        let candidate_path =
            std::fs::canonicalize(&profile.path).unwrap_or_else(|_| PathBuf::from(&profile.path));
        if self.profiles.iter().any(|existing| {
            std::fs::canonicalize(&existing.path).unwrap_or_else(|_| PathBuf::from(&existing.path))
                == candidate_path
        }) {
            return Err("This workspace folder has already been added.".into());
        }
        validate_profile_uniqueness(&self.profiles, &profile)?;
        save_secrets(&profile)?;
        self.profiles.push(profile.clone());
        self.persist()?;
        Ok(profile)
    }

    pub fn update(&mut self, mut profile: WorkspaceProfile) -> Result<WorkspaceProfile, String> {
        profile.validate()?;
        validate_profile_uniqueness(&self.profiles, &profile)?;
        let mut gateway = GatewayConfig::from_workspace_profile(&profile);
        gateway.local_capability_roots = self.gateway.local_capability_roots.clone();
        save_gateway_secrets(&gateway)?;
        self.gateway = gateway;
        self.gateway.apply_to_workspace_profile(&mut profile);
        let target = self
            .profiles
            .iter_mut()
            .find(|item| item.id == profile.id)
            .ok_or("Workspace profile was not found.")?;
        save_secrets(&profile)?;
        *target = profile.clone();
        self.persist()?;
        Ok(profile)
    }

    pub fn set_local_capability_roots(
        &mut self,
        roots: Vec<String>,
    ) -> Result<GatewayConfig, String> {
        self.gateway.local_capability_roots = roots;
        self.persist()?;
        Ok(self.gateway.clone())
    }

    pub fn remove(&mut self, id: &str) -> Result<(), String> {
        let length = self.profiles.len();
        self.profiles.retain(|profile| profile.id != id);
        if self.profiles.len() == length {
            return Err("Workspace profile was not found.".into());
        }
        if let Ok(entry) = Entry::new(keyring_service(), id) {
            let _ = entry.delete_credential();
        }
        let _ = fs::remove_dir_all(self.log_dir(id)?);
        self.persist()
    }

    pub fn log_dir(&self, id: &str) -> Result<PathBuf, String> {
        if id.len() != 32 || !id.chars().all(|character| character.is_ascii_hexdigit()) {
            return Err("Invalid workspace profile ID.".into());
        }
        let path = self.home.join("state").join(id);
        fs::create_dir_all(&path).map_err(|error| error.to_string())?;
        restrict(&path, 0o700)?;
        Ok(path)
    }

    fn persist(&self) -> Result<(), String> {
        let mut public_profiles = self.profiles.clone();
        for profile in &mut public_profiles {
            self.gateway.apply_to_workspace_profile(profile);
            profile.tunnel.cloudflare_token.clear();
            profile.auth.oauth_password.clear();
            profile.auth.oauth_token_secret.clear();
            profile.auth.bearer_token.clear();
            for variable in &mut profile.runtime.environment_variables {
                variable.value.clear();
            }
        }
        let mut public_gateway = self.gateway.clone();
        public_gateway.tunnel.cloudflare_token.clear();
        public_gateway.auth.oauth_password.clear();
        public_gateway.auth.oauth_token_secret.clear();
        public_gateway.auth.bearer_token.clear();
        atomic_json(
            &self.home.join("profiles.json"),
            &ProfileDocument {
                language: self.language.clone(),
                gateway: Some(public_gateway),
                profiles: public_profiles,
            },
        )
    }
}

fn normalize_legacy_file_access(profiles: &mut [WorkspaceProfile]) -> bool {
    let home = user_home_directory();
    let mut changed = false;
    for profile in profiles {
        if profile.runtime.file_access_scope != "workspace" {
            profile.runtime.file_access_scope = "workspace".into();
            changed = true;
        }
        let workspace = std::fs::canonicalize(&profile.path).ok();
        let full_access = profile.runtime.permission_mode == "host";
        let mut seen = HashSet::new();
        let before = profile.runtime.allowed_paths.len();
        profile.runtime.allowed_paths.retain(|raw| {
            let Ok(path) = std::fs::canonicalize(raw) else {
                return true;
            };
            workspace
                .as_ref()
                .is_none_or(|workspace| !path.starts_with(workspace))
                && (full_access || home.as_ref() != Some(&path))
                && seen.insert(path)
        });
        changed |= profile.runtime.allowed_paths.len() != before;
    }
    changed
}

fn normalize_language(language: &str) -> &'static str {
    if language.eq_ignore_ascii_case("zh-CN") || language.to_ascii_lowercase().starts_with("zh") {
        "zh-CN"
    } else {
        "en"
    }
}

fn validate_profile_uniqueness(
    profiles: &[WorkspaceProfile],
    candidate: &WorkspaceProfile,
) -> Result<(), String> {
    for existing in profiles
        .iter()
        .filter(|existing| existing.id != candidate.id)
    {
        let existing_path =
            std::fs::canonicalize(&existing.path).unwrap_or_else(|_| PathBuf::from(&existing.path));
        let candidate_path = std::fs::canonicalize(&candidate.path)
            .unwrap_or_else(|_| PathBuf::from(&candidate.path));
        if existing_path == candidate_path {
            return Err("This workspace folder has already been added.".into());
        }
    }
    Ok(())
}

fn gateway_settings_differ(gateway: &GatewayConfig, profile: &WorkspaceProfile) -> bool {
    gateway.server_name_prefix != profile.runtime.server_name_prefix
        || gateway.local_port != profile.runtime.local_port
        || gateway.tunnel.r#type != profile.tunnel.r#type
        || gateway.tunnel.domain != profile.tunnel.domain
        || gateway.tunnel.public_url != profile.tunnel.public_url
        || gateway.tunnel.frp_server != profile.tunnel.frp_server
        || gateway.tunnel.frp_subdomain != profile.tunnel.frp_subdomain
        || gateway.tunnel.cloudflare_mode != profile.tunnel.cloudflare_mode
        || gateway.tunnel.cloudflare_token != profile.tunnel.cloudflare_token
        || gateway.auth.r#type != profile.auth.r#type
        || gateway.auth.oauth_password != profile.auth.oauth_password
        || gateway.auth.oauth_token_secret != profile.auth.oauth_token_secret
        || gateway.auth.bearer_token != profile.auth.bearer_token
}

fn save_gateway_secrets(gateway: &GatewayConfig) -> Result<(), String> {
    let encoded = serde_json::to_string(&GatewaySecrets::from_gateway(gateway))
        .map_err(|error| error.to_string())?;
    Entry::new(keyring_service(), "__gateway__")
        .map_err(|error| format!("Could not open the system keychain: {error}"))?
        .set_password(&encoded)
        .map_err(|error| format!("Could not save Gateway secrets to the system keychain: {error}"))
}

fn load_gateway_secrets() -> Result<Option<GatewaySecrets>, String> {
    let entry = Entry::new(keyring_service(), "__gateway__").map_err(|error| error.to_string())?;
    match entry.get_password() {
        Ok(encoded) => serde_json::from_str(&encoded)
            .map(Some)
            .map_err(|error| error.to_string()),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(error) => Err(error.to_string()),
    }
}

fn save_secrets(profile: &WorkspaceProfile) -> Result<(), String> {
    let encoded = serde_json::to_string(&ProfileSecrets::from_profile(profile))
        .map_err(|error| error.to_string())?;
    Entry::new(keyring_service(), &profile.id)
        .map_err(|error| format!("Could not open the system keychain: {error}"))?
        .set_password(&encoded)
        .map_err(|error| format!("Could not save secrets to the system keychain: {error}"))
}

fn load_secrets(id: &str) -> Result<Option<ProfileSecrets>, String> {
    let entry = Entry::new(keyring_service(), id).map_err(|error| error.to_string())?;
    match entry.get_password() {
        Ok(encoded) => serde_json::from_str(&encoded)
            .map(Some)
            .map_err(|error| error.to_string()),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(error) => Err(error.to_string()),
    }
}

fn atomic_json(path: &Path, value: &impl Serialize) -> Result<(), String> {
    let parent = path.parent().ok_or("Invalid storage path.")?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let temporary = parent.join(format!(
        ".{}.tmp-{}",
        path.file_name().unwrap_or_default().to_string_lossy(),
        std::process::id()
    ));
    let mut file = fs::File::create(&temporary).map_err(|error| error.to_string())?;
    file.write_all(
        serde_json::to_string_pretty(value)
            .map_err(|error| error.to_string())?
            .as_bytes(),
    )
    .map_err(|error| error.to_string())?;
    file.write_all(b"\n").map_err(|error| error.to_string())?;
    file.sync_all().map_err(|error| error.to_string())?;
    restrict(&temporary, 0o600)?;
    fs::rename(&temporary, path).map_err(|error| error.to_string())
}

#[cfg(unix)]
fn restrict(path: &Path, mode: u32) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).map_err(|error| error.to_string())
}

#[cfg(not(unix))]
fn restrict(_path: &Path, _mode: u32) -> Result<(), String> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_home_access_and_workspace_duplicates_are_removed_from_standard_profiles() {
        let Some(home) = user_home_directory() else {
            return;
        };
        let root = std::env::current_dir().unwrap();
        let mut profile =
            WorkspaceProfile::new(root.to_string_lossy().into_owned(), 28766).unwrap();
        profile.runtime.file_access_scope = "home".into();
        profile.runtime.allowed_paths = vec![
            home.to_string_lossy().into_owned(),
            root.to_string_lossy().into_owned(),
        ];
        assert!(normalize_legacy_file_access(std::slice::from_mut(
            &mut profile
        )));
        assert_eq!(profile.runtime.file_access_scope, "workspace");
        assert!(profile.runtime.allowed_paths.is_empty());
    }

    #[test]
    fn explicitly_allowed_home_is_preserved_for_full_access_profiles() {
        let Some(home) = user_home_directory() else {
            return;
        };
        let root = tempfile::tempdir().unwrap();
        let mut profile =
            WorkspaceProfile::new(root.path().to_string_lossy().into_owned(), 28766).unwrap();
        profile.runtime.permission_mode = "host".into();
        profile.runtime.allowed_paths = vec![home.to_string_lossy().into_owned()];

        assert!(!normalize_legacy_file_access(std::slice::from_mut(
            &mut profile
        )));
        assert_eq!(profile.runtime.allowed_paths, vec![home.to_string_lossy()]);
    }

    #[test]
    fn profile_secrets_restore_environment_variable_values() {
        let root = std::env::current_dir().unwrap();
        let mut profile =
            WorkspaceProfile::new(root.to_string_lossy().into_owned(), 28766).unwrap();
        profile.runtime.environment_variables = vec![EnvironmentVariable {
            name: "OPENAI_API_KEY".into(),
            value: "secret".into(),
        }];
        let secrets = ProfileSecrets::from_profile(&profile);
        profile.runtime.environment_variables.clear();
        secrets.apply(&mut profile);
        assert_eq!(
            profile.runtime.environment_variables[0].name,
            "OPENAI_API_KEY"
        );
        assert_eq!(profile.runtime.environment_variables[0].value, "secret");
    }

    #[test]
    fn persisted_profiles_keep_environment_names_but_redact_values() {
        let temporary = tempfile::tempdir().unwrap();
        let root = std::env::current_dir().unwrap();
        let mut profile =
            WorkspaceProfile::new(root.to_string_lossy().into_owned(), 28766).unwrap();
        profile.runtime.environment_variables = vec![EnvironmentVariable {
            name: "OPENAI_API_KEY".into(),
            value: "secret".into(),
        }];
        let store = ProfileStore {
            home: temporary.path().to_path_buf(),
            language: "en".into(),
            gateway: GatewayConfig::from_workspace_profile(&profile),
            profiles: vec![profile],
            migration_warning: None,
        };
        store.persist().unwrap();
        let saved: serde_json::Value =
            serde_json::from_slice(&std::fs::read(temporary.path().join("profiles.json")).unwrap())
                .unwrap();
        let variable = &saved["profiles"][0]["runtime"]["environment_variables"][0];
        assert_eq!(variable["name"], "OPENAI_API_KEY");
        assert_eq!(variable["value"], "");
        assert!(
            !std::fs::read_to_string(temporary.path().join("profiles.json"))
                .unwrap()
                .contains("\"value\": \"secret\"")
        );
    }

    #[test]
    fn rejects_profile_id_traversal() {
        let temporary = tempfile::tempdir().unwrap();
        let store = ProfileStore {
            home: temporary.path().to_path_buf(),
            language: "en".into(),
            gateway: GatewayConfig::default(),
            profiles: vec![],
            migration_warning: None,
        };
        assert!(store.log_dir("../../outside").is_err());
    }

    #[test]
    fn menu_language_defaults_to_english_and_persists_chinese() {
        let document: ProfileDocument = serde_json::from_value(serde_json::json!({
            "profiles": []
        }))
        .unwrap();
        assert_eq!(normalize_language(&document.language), "en");

        let temporary = tempfile::tempdir().unwrap();
        let mut store = ProfileStore {
            home: temporary.path().to_path_buf(),
            language: "en".into(),
            gateway: GatewayConfig::default(),
            profiles: vec![],
            migration_warning: None,
        };
        store.set_language("zh-CN").unwrap();
        let saved: serde_json::Value =
            serde_json::from_slice(&std::fs::read(temporary.path().join("profiles.json")).unwrap())
                .unwrap();
        assert_eq!(saved["language"], "zh-CN");
    }

    #[test]
    fn old_runtime_command_fields_are_ignored() {
        let value = serde_json::json!({"local_port": 28766, "permission_mode": "trusted", "runtime_command": "unsafe"});
        let runtime: crate::models::RuntimeConfig = serde_json::from_value(value).unwrap();
        assert_eq!(runtime.local_port, 28766);
    }

    #[test]
    fn projects_share_gateway_port_url_and_tunnel_token_but_require_unique_paths() {
        let first_root = tempfile::tempdir().unwrap();
        let second_root = tempfile::tempdir().unwrap();
        let mut first =
            WorkspaceProfile::new(first_root.path().to_string_lossy().into_owned(), 28766).unwrap();
        first.tunnel.cloudflare_mode = "named".into();
        first.tunnel.public_url = "https://tax-mcp.example.com".into();
        first.tunnel.cloudflare_token = "tax-token".into();

        let mut second =
            WorkspaceProfile::new(second_root.path().to_string_lossy().into_owned(), 28766)
                .unwrap();
        second.tunnel.cloudflare_mode = "named".into();
        second.tunnel.public_url = "https://tax-mcp.example.com/".into();
        second.tunnel.cloudflare_token = "tax-token".into();
        assert!(validate_profile_uniqueness(&[first.clone()], &second).is_ok());

        second.path = first.path.clone();
        assert!(validate_profile_uniqueness(&[first], &second).is_err());
    }
}
