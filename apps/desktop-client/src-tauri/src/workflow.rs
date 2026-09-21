use rusqlite::{Connection, OpenFlags};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const SUMMARY_LIMIT: i64 = 12;

#[derive(Clone, Serialize)]
pub struct WorkflowTask {
    pub task_id: String,
    pub title: String,
    pub status: String,
    pub revision: i64,
    pub updated_at: f64,
    pub plan_completed: usize,
    pub plan_total: usize,
}

#[derive(Clone, Serialize)]
pub struct WorkflowCheckpoint {
    pub checkpoint_id: String,
    pub label: String,
    pub file_count: i64,
    pub created_at: f64,
}

#[derive(Clone, Serialize)]
pub struct WorkflowCheck {
    pub check_run_id: String,
    pub check_id: String,
    pub status: String,
    pub task_id: Option<String>,
    pub created_at: f64,
}

#[derive(Clone, Serialize)]
pub struct WorkflowReview {
    pub review_id: String,
    pub status: String,
    pub finding_count: i64,
    pub task_id: Option<String>,
    pub updated_at: f64,
}

#[derive(Clone, Serialize)]
pub struct WorkflowApproval {
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
pub struct WorkflowWorktree {
    pub worktree_id: String,
    pub path: String,
}


#[derive(Clone, Serialize)]
pub struct WorkflowSnapshot {
    pub available: bool,
    pub workspace_id: String,
    pub tasks: Vec<WorkflowTask>,
    pub checkpoints: Vec<WorkflowCheckpoint>,
    pub checks: Vec<WorkflowCheck>,
    pub reviews: Vec<WorkflowReview>,
    pub approvals: Vec<WorkflowApproval>,
    pub worktrees: Vec<WorkflowWorktree>,
    pub warning: Option<String>,
}

impl WorkflowSnapshot {
    fn empty(workspace_id: String) -> Self {
        Self {
            available: false,
            workspace_id,
            tasks: Vec::new(),
            checkpoints: Vec::new(),
            checks: Vec::new(),
            reviews: Vec::new(),
            approvals: Vec::new(),
            worktrees: Vec::new(),
            warning: None,
        }
    }

    pub fn failure(warning: String) -> Self {
        Self {
            available: false,
            workspace_id: String::new(),
            tasks: Vec::new(),
            checkpoints: Vec::new(),
            checks: Vec::new(),
            reviews: Vec::new(),
            approvals: Vec::new(),
            worktrees: Vec::new(),
            warning: Some(warning),
        }
    }
}

pub fn read_snapshot(state_root: &Path, workspace: &Path) -> Result<WorkflowSnapshot, String> {
    let database = workflow_database(state_root, workspace)?;
    let workspace_id = database
        .parent()
        .and_then(Path::file_name)
        .and_then(|value| value.to_str())
        .ok_or_else(|| "Workflow state path is invalid.".to_string())?
        .to_string();
    if !database.is_file() {
        return Ok(WorkflowSnapshot::empty(workspace_id));
    }
    let connection = Connection::open_with_flags(
        &database,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| format!("Could not open workflow state: {error}"))?;
    connection
        .busy_timeout(Duration::from_secs(1))
        .map_err(|error| format!("Could not configure workflow state: {error}"))?;

    let tasks = read_tasks(&connection)?;
    let checkpoints = read_checkpoints(&connection)?;
    let checks = if table_exists(&connection, "check_runs")? {
        read_checks(&connection)?
    } else {
        Vec::new()
    };
    let reviews = if table_exists(&connection, "reviews")? {
        read_reviews(&connection)?
    } else {
        Vec::new()
    };
    let approvals = if table_exists(&connection, "approvals")? {
        expire_and_read_approvals(&database)?
    } else {
        Vec::new()
    };
    let worktrees = read_managed_worktrees(
        database
            .parent()
            .ok_or_else(|| "Workflow state path is invalid.".to_string())?,
    )?;
    Ok(WorkflowSnapshot {
        available: true,
        workspace_id,
        tasks,
        checkpoints,
        checks,
        reviews,
        approvals,
        worktrees,
        warning: None,
    })
}

fn read_managed_worktrees(workspace_state: &Path) -> Result<Vec<WorkflowWorktree>, String> {
    let root = workspace_state.join("worktrees");
    if !root.is_dir() {
        return Ok(Vec::new());
    }
    let mut worktrees = Vec::new();
    for entry in std::fs::read_dir(&root)
        .map_err(|error| format!("Could not list managed worktrees: {error}"))?
    {
        let entry =
            entry.map_err(|error| format!("Could not inspect managed worktree: {error}"))?;
        if !entry
            .file_type()
            .map_err(|error| format!("Could not inspect managed worktree type: {error}"))?
            .is_dir()
        {
            continue;
        }
        worktrees.push(WorkflowWorktree {
            worktree_id: entry.file_name().to_string_lossy().into_owned(),
            path: entry.path().to_string_lossy().into_owned(),
        });
    }
    worktrees.sort_by(|left, right| left.worktree_id.cmp(&right.worktree_id));
    Ok(worktrees)
}

pub fn decide_approval(
    state_root: &Path,
    workspace: &Path,
    approval_id: &str,
    approved: bool,
) -> Result<(), String> {
    let database = workflow_database(state_root, workspace)?;
    if !database.is_file() {
        return Err("Workflow state is not available for this workspace.".to_string());
    }
    let connection = Connection::open(database)
        .map_err(|error| format!("Could not open workflow state: {error}"))?;
    connection
        .busy_timeout(Duration::from_secs(2))
        .map_err(|error| format!("Could not configure workflow state: {error}"))?;
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

fn workflow_database(state_root: &Path, workspace: &Path) -> Result<std::path::PathBuf, String> {
    let canonical = workspace
        .canonicalize()
        .map_err(|error| format!("Could not resolve workspace state: {error}"))?;
    let canonical_text = canonical
        .to_str()
        .ok_or_else(|| "Workspace path is not valid UTF-8.".to_string())?;
    Ok(state_root
        .join("workspaces")
        .join(workspace_id(canonical_text))
        .join("workflow.sqlite3"))
}

fn unix_time() -> Result<f64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .map_err(|error| format!("System clock is invalid: {error}"))
}

fn workspace_id(canonical_workspace: &str) -> String {
    let digest = Sha256::digest(canonical_workspace.as_bytes());
    digest[..12]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn table_exists(connection: &Connection, name: &str) -> Result<bool, String> {
    connection
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
            [name],
            |row| row.get::<_, bool>(0),
        )
        .map_err(|error| format!("Could not inspect workflow schema: {error}"))
}

fn read_tasks(connection: &Connection) -> Result<Vec<WorkflowTask>, String> {
    let mut statement = connection
        .prepare(concat!(
            "SELECT task_id, title, status, revision, updated_at, details_json FROM tasks ",
            "ORDER BY updated_at DESC LIMIT ?1"
        ))
        .map_err(|error| format!("Could not read workflow tasks: {error}"))?;
    let rows = statement
        .query_map([SUMMARY_LIMIT], |row| {
            let details: String = row.get(5)?;
            let plan = serde_json::from_str::<serde_json::Value>(&details)
                .ok()
                .and_then(|value| value.get("plan").and_then(|plan| plan.as_array()).cloned())
                .unwrap_or_default();
            Ok(WorkflowTask {
                task_id: row.get(0)?,
                title: row.get(1)?,
                status: row.get(2)?,
                revision: row.get(3)?,
                updated_at: row.get(4)?,
                plan_completed: plan
                    .iter()
                    .filter(|step| {
                        step.get("status").and_then(|value| value.as_str()) == Some("completed")
                    })
                    .count(),
                plan_total: plan.len(),
            })
        })
        .map_err(|error| format!("Could not query workflow tasks: {error}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("Could not decode workflow tasks: {error}"))
}

fn read_checkpoints(connection: &Connection) -> Result<Vec<WorkflowCheckpoint>, String> {
    let mut statement = connection
        .prepare(concat!(
            "SELECT c.checkpoint_id, c.label, c.created_at, ",
            "(SELECT COUNT(*) FROM checkpoint_files f WHERE f.checkpoint_id=c.checkpoint_id) ",
            "FROM checkpoints c ORDER BY c.created_at DESC LIMIT ?1"
        ))
        .map_err(|error| format!("Could not read workflow checkpoints: {error}"))?;
    let rows = statement
        .query_map([SUMMARY_LIMIT], |row| {
            Ok(WorkflowCheckpoint {
                checkpoint_id: row.get(0)?,
                label: row.get(1)?,
                created_at: row.get(2)?,
                file_count: row.get(3)?,
            })
        })
        .map_err(|error| format!("Could not query workflow checkpoints: {error}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("Could not decode workflow checkpoints: {error}"))
}

fn read_checks(connection: &Connection) -> Result<Vec<WorkflowCheck>, String> {
    let mut statement = connection
        .prepare(concat!(
            "SELECT check_run_id, check_id, status, task_id, created_at FROM check_runs ",
            "ORDER BY created_at DESC LIMIT ?1"
        ))
        .map_err(|error| format!("Could not read workflow checks: {error}"))?;
    let rows = statement
        .query_map([SUMMARY_LIMIT], |row| {
            Ok(WorkflowCheck {
                check_run_id: row.get(0)?,
                check_id: row.get(1)?,
                status: row.get(2)?,
                task_id: row.get(3)?,
                created_at: row.get(4)?,
            })
        })
        .map_err(|error| format!("Could not query workflow checks: {error}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("Could not decode workflow checks: {error}"))
}

fn read_reviews(connection: &Connection) -> Result<Vec<WorkflowReview>, String> {
    let mut statement = connection
        .prepare(concat!(
            "SELECT review_id, status, json_array_length(findings_json), task_id, updated_at FROM reviews ",
            "ORDER BY updated_at DESC LIMIT ?1"
        ))
        .map_err(|error| format!("Could not read workflow reviews: {error}"))?;
    let rows = statement
        .query_map([SUMMARY_LIMIT], |row| {
            Ok(WorkflowReview {
                review_id: row.get(0)?,
                status: row.get(1)?,
                finding_count: row.get(2)?,
                task_id: row.get(3)?,
                updated_at: row.get(4)?,
            })
        })
        .map_err(|error| format!("Could not query workflow reviews: {error}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("Could not decode workflow reviews: {error}"))
}

fn expire_and_read_approvals(database: &Path) -> Result<Vec<WorkflowApproval>, String> {
    let connection = Connection::open(database)
        .map_err(|error| format!("Could not open approval state: {error}"))?;
    connection
        .busy_timeout(Duration::from_secs(1))
        .map_err(|error| format!("Could not configure approval state: {error}"))?;
    connection
        .execute(
            "UPDATE approvals SET status='expired' WHERE status IN ('pending','approved') AND expires_at<=?1",
            [unix_time()?],
        )
        .map_err(|error| format!("Could not expire approval state: {error}"))?;
    let mut statement = connection
        .prepare(concat!(
            "SELECT approval_id, tool_name, permission, reason, arguments_json, status, expires_at, created_at ",
            "FROM approvals ORDER BY created_at DESC LIMIT ?1"
        ))
        .map_err(|error| format!("Could not read workflow approvals: {error}"))?;
    let rows = statement
        .query_map([SUMMARY_LIMIT], |row| {
            Ok(WorkflowApproval {
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
        .map_err(|error| format!("Could not query workflow approvals: {error}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("Could not decode workflow approvals: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_database_returns_an_empty_snapshot() {
        let state = tempfile::tempdir().unwrap();
        let workspace = tempfile::tempdir().unwrap();
        let snapshot = read_snapshot(state.path(), workspace.path()).unwrap();
        assert!(!snapshot.available);
        assert!(snapshot.tasks.is_empty());
        assert_eq!(snapshot.workspace_id.len(), 24);
    }

    #[test]
    fn approval_decision_only_updates_live_pending_request() {
        let state = tempfile::tempdir().unwrap();
        let workspace = tempfile::tempdir().unwrap();
        let database = workflow_database(state.path(), workspace.path()).unwrap();
        std::fs::create_dir_all(database.parent().unwrap()).unwrap();
        let connection = Connection::open(&database).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE approvals (
                    approval_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, permission TEXT NOT NULL,
                    reason TEXT NOT NULL, arguments_hash TEXT NOT NULL, arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL, expires_at REAL NOT NULL, consumed_at REAL,
                    created_at REAL NOT NULL, decided_at REAL
                );",
            )
            .unwrap();
        let now = unix_time().unwrap();
        connection
            .execute(
                "INSERT INTO approvals VALUES ('approval_live','exec_command','network','test','hash','{}','pending',?1,NULL,?2,NULL)",
                (now + 60.0, now),
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO approvals VALUES ('approval_expired','exec_command','network','test','hash','{}','pending',?1,NULL,?2,NULL)",
                (now - 1.0, now),
            )
            .unwrap();
        drop(connection);

        decide_approval(state.path(), workspace.path(), "approval_live", true).unwrap();
        let connection = Connection::open(&database).unwrap();
        let status: String = connection
            .query_row(
                "SELECT status FROM approvals WHERE approval_id='approval_live'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(status, "approved");
        drop(connection);
        assert!(decide_approval(state.path(), workspace.path(), "approval_live", false).is_err());
        assert!(decide_approval(state.path(), workspace.path(), "approval_expired", true).is_err());
    }

    #[test]
    fn managed_worktree_scan_ignores_files() {
        let state = tempfile::tempdir().unwrap();
        let root = state.path().join("worktrees");
        std::fs::create_dir(&root).unwrap();
        std::fs::create_dir(root.join("task-one")).unwrap();
        std::fs::write(root.join("note.txt"), "ignored").unwrap();
        let worktrees = read_managed_worktrees(state.path()).unwrap();
        assert_eq!(worktrees.len(), 1);
        assert_eq!(worktrees[0].worktree_id, "task-one");
    }
}
