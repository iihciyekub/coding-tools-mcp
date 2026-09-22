use rusqlite::{Connection, OpenFlags};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const SUMMARY_LIMIT: i64 = 50;

#[derive(Clone, Serialize)]
pub struct RuntimeApproval {
    pub approval_id: String,
    pub tool_name: String,
    pub permission: String,
    pub reason: String,
    pub arguments: String,
    pub status: String,
    pub expires_at: f64,
    pub created_at: f64,
}

#[derive(Clone, Serialize)]
pub struct RuntimeStateSnapshot {
    pub available: bool,
    pub workspace_id: String,
    pub approvals: Vec<RuntimeApproval>,
    pub warning: Option<String>,
}

impl RuntimeStateSnapshot {
    pub fn failure(warning: String) -> Self {
        Self {
            available: false,
            workspace_id: String::new(),
            approvals: Vec::new(),
            warning: Some(warning),
        }
    }
}

pub fn read_snapshot(state_root: &Path, workspace: &Path) -> Result<RuntimeStateSnapshot, String> {
    let root = project_state_root(state_root, workspace)?;
    let workspace_id = root
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| "Runtime state path is invalid.".to_string())?
        .to_string();
    let database = root.join("runtime.sqlite3");
    let approvals = if database.is_file() {
        let connection = Connection::open_with_flags(
            &database,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|error| format!("Could not open runtime state: {error}"))?;
        connection
            .busy_timeout(std::time::Duration::from_secs(1))
            .map_err(|error| format!("Could not configure runtime state: {error}"))?;
        expire_and_read_approvals(&connection)?
    } else {
        Vec::new()
    };
    Ok(RuntimeStateSnapshot {
        available: database.is_file() || root.is_dir(),
        workspace_id,
        approvals,
        warning: None,
    })
}

pub fn decide_approval(
    state_root: &Path,
    workspace: &Path,
    approval_id: &str,
    approved: bool,
) -> Result<(), String> {
    let database = project_state_root(state_root, workspace)?.join("runtime.sqlite3");
    if !database.is_file() {
        return Err("Runtime approval state is not available for this project.".to_string());
    }
    let connection = Connection::open(database)
        .map_err(|error| format!("Could not open runtime state: {error}"))?;
    connection
        .busy_timeout(std::time::Duration::from_secs(2))
        .map_err(|error| format!("Could not configure runtime state: {error}"))?;
    let now = unix_time()?;
    connection
        .execute(
            "UPDATE approvals SET status=?1, decided_at=?2 WHERE approval_id=?3 AND status='pending' AND expires_at>?2",
            (if approved { "approved" } else { "denied" }, now, approval_id),
        )
        .map_err(|error| format!("Could not decide approval: {error}"))?
        .eq(&1)
        .then_some(())
        .ok_or_else(|| "Approval is missing, expired, or already decided.".to_string())
}

fn project_state_root(state_root: &Path, workspace: &Path) -> Result<PathBuf, String> {
    let canonical = workspace
        .canonicalize()
        .map_err(|error| format!("Could not resolve project state: {error}"))?;
    let canonical_text = canonical
        .to_str()
        .ok_or_else(|| "Project path is not valid UTF-8.".to_string())?;
    let digest = Sha256::digest(canonical_text.as_bytes());
    let workspace_id = digest[..12]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(state_root.join("projects").join(workspace_id))
}

fn expire_and_read_approvals(connection: &Connection) -> Result<Vec<RuntimeApproval>, String> {
    let now = unix_time()?;
    connection
        .execute(
            "UPDATE approvals SET status='expired' WHERE status IN ('pending','approved') AND expires_at <= ?1",
            [now],
        )
        .map_err(|error| format!("Could not expire runtime approvals: {error}"))?;
    let mut statement = connection
        .prepare(
            "SELECT approval_id, tool_name, permission, reason, arguments_json, status, expires_at, created_at FROM approvals ORDER BY created_at DESC LIMIT ?1",
        )
        .map_err(|error| format!("Could not read runtime approvals: {error}"))?;
    let rows = statement
        .query_map([SUMMARY_LIMIT], |row| {
            Ok(RuntimeApproval {
                approval_id: row.get(0)?,
                tool_name: row.get(1)?,
                permission: row.get(2)?,
                reason: row.get(3)?,
                arguments: row.get(4)?,
                status: row.get(5)?,
                expires_at: row.get(6)?,
                created_at: row.get(7)?,
            })
        })
        .map_err(|error| format!("Could not query runtime approvals: {error}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("Could not decode runtime approvals: {error}"))
}

fn unix_time() -> Result<f64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .map_err(|error| format!("System clock is invalid: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn create_approval_db(state_root: &Path, workspace: &Path) -> PathBuf {
        let project_root = project_state_root(state_root, workspace).unwrap();
        std::fs::create_dir_all(&project_root).unwrap();
        let database = project_root.join("runtime.sqlite3");
        let connection = Connection::open(&database).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE approvals (
                    approval_id TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL,
                    created_at REAL NOT NULL,
                    decided_at REAL
                );",
            )
            .unwrap();
        database
    }

    #[test]
    fn approvals_can_be_decided_and_expired() {
        let state = tempfile::tempdir().unwrap();
        let workspace = tempfile::tempdir().unwrap();
        let database = create_approval_db(state.path(), workspace.path());
        let now = unix_time().unwrap();
        let connection = Connection::open(&database).unwrap();
        connection
            .execute(
                "INSERT INTO approvals VALUES (?1,'exec_command','network','test','hash','{}','pending',?2,NULL,?3,NULL)",
                ("approval-live", now + 60.0, now),
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO approvals VALUES (?1,'exec_command','network','expired','hash','{}','pending',?2,NULL,?3,NULL)",
                ("approval-expired", now - 1.0, now - 2.0),
            )
            .unwrap();
        drop(connection);

        decide_approval(state.path(), workspace.path(), "approval-live", true).unwrap();
        let snapshot = read_snapshot(state.path(), workspace.path()).unwrap();
        let live = snapshot
            .approvals
            .iter()
            .find(|item| item.approval_id == "approval-live")
            .unwrap();
        let expired = snapshot
            .approvals
            .iter()
            .find(|item| item.approval_id == "approval-expired")
            .unwrap();
        assert_eq!(live.status, "approved");
        assert_eq!(expired.status, "expired");
    }
}
