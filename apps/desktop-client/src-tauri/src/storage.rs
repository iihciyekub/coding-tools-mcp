use crate::models::WorkspaceProfile;
use keyring::Entry;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

const KEYRING_SERVICE: &str = "coding-tools-mcp-desktop";

#[derive(Default, Deserialize, Serialize)]
struct ProfileDocument {
    profiles: Vec<WorkspaceProfile>,
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
}

impl ProfileSecrets {
    fn from_profile(profile: &WorkspaceProfile) -> Self {
        Self {
            cloudflare_token: profile.tunnel.cloudflare_token.clone(),
            oauth_password: profile.auth.oauth_password.clone(),
            oauth_token_secret: profile.auth.oauth_token_secret.clone(),
            bearer_token: profile.auth.bearer_token.clone(),
        }
    }

    fn apply(self, profile: &mut WorkspaceProfile) {
        profile.tunnel.cloudflare_token = self.cloudflare_token;
        profile.auth.oauth_password = self.oauth_password;
        profile.auth.oauth_token_secret = self.oauth_token_secret;
        profile.auth.bearer_token = self.bearer_token;
    }
}

pub struct ProfileStore {
    home: PathBuf,
    profiles: Vec<WorkspaceProfile>,
}

impl ProfileStore {
    pub fn open_default() -> Result<Self, String> {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .ok_or("Could not resolve the user home directory.")?
            .join(".coding-tools-mcp-desktop");
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
        let mut normalized_legacy_auth = false;
        for profile in &mut document.profiles {
            if profile.auth.r#type == "noauth" {
                profile.auth.r#type = "oauth".into();
                normalized_legacy_auth = true;
            }
        }

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
            match load_secrets(&profile.id) {
                Ok(Some(secrets)) => secrets.apply(profile),
                Ok(None) => {
                    if let Some(secrets) = legacy.get(&profile.id) {
                        secrets.clone().apply(profile);
                        if save_secrets(profile).is_err() {
                            migrated_all = false;
                        }
                    }
                }
                Err(_) => {
                    migrated_all = false;
                    if let Some(secrets) = legacy.get(&profile.id) {
                        secrets.clone().apply(profile);
                    }
                }
            }
        }
        if migrated_all {
            let _ = fs::remove_file(home.join("secrets.json"));
        }
        let store = Self {
            home,
            profiles: document.profiles,
        };
        if normalized_legacy_auth {
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
        self.profiles.clone()
    }
    pub fn get(&self, id: &str) -> Option<WorkspaceProfile> {
        self.profiles
            .iter()
            .find(|profile| profile.id == id)
            .cloned()
    }

    pub fn next_port(&self) -> u16 {
        (28766..=65535)
            .find(|port| {
                !self
                    .profiles
                    .iter()
                    .any(|profile| profile.runtime.local_port == *port)
            })
            .unwrap_or(28766)
    }

    pub fn insert(&mut self, profile: WorkspaceProfile) -> Result<WorkspaceProfile, String> {
        profile.validate()?;
        if self.profiles.iter().any(|item| item.id == profile.id) {
            return Err("Workspace profile already exists.".into());
        }
        save_secrets(&profile)?;
        self.profiles.push(profile.clone());
        self.persist()?;
        Ok(profile)
    }

    pub fn update(&mut self, profile: WorkspaceProfile) -> Result<WorkspaceProfile, String> {
        profile.validate()?;
        if self.profiles.iter().any(|item| {
            item.id != profile.id && item.runtime.local_port == profile.runtime.local_port
        }) {
            return Err("Another workspace already uses this local port.".into());
        }
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

    pub fn remove(&mut self, id: &str) -> Result<(), String> {
        let length = self.profiles.len();
        self.profiles.retain(|profile| profile.id != id);
        if self.profiles.len() == length {
            return Err("Workspace profile was not found.".into());
        }
        if let Ok(entry) = Entry::new(KEYRING_SERVICE, id) {
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
            profile.tunnel.cloudflare_token.clear();
            profile.auth.oauth_password.clear();
            profile.auth.oauth_token_secret.clear();
            profile.auth.bearer_token.clear();
        }
        atomic_json(
            &self.home.join("profiles.json"),
            &ProfileDocument {
                profiles: public_profiles,
            },
        )
    }
}

fn save_secrets(profile: &WorkspaceProfile) -> Result<(), String> {
    let encoded = serde_json::to_string(&ProfileSecrets::from_profile(profile))
        .map_err(|error| error.to_string())?;
    Entry::new(KEYRING_SERVICE, &profile.id)
        .map_err(|error| format!("Could not open the system keychain: {error}"))?
        .set_password(&encoded)
        .map_err(|error| format!("Could not save secrets to the system keychain: {error}"))
}

fn load_secrets(id: &str) -> Result<Option<ProfileSecrets>, String> {
    let entry = Entry::new(KEYRING_SERVICE, id).map_err(|error| error.to_string())?;
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
    fn rejects_profile_id_traversal() {
        let temporary = tempfile::tempdir().unwrap();
        let store = ProfileStore {
            home: temporary.path().to_path_buf(),
            profiles: vec![],
        };
        assert!(store.log_dir("../../outside").is_err());
    }

    #[test]
    fn old_runtime_command_fields_are_ignored() {
        let value = serde_json::json!({"local_port": 28766, "permission_mode": "trusted", "runtime_command": "unsafe"});
        let runtime: crate::models::RuntimeConfig = serde_json::from_value(value).unwrap();
        assert_eq!(runtime.local_port, 28766);
    }
}
