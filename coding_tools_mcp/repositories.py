"""Per-call Git identity and small, process-local write coordination.

No current-project state is stored here. Paths are resolved by the runtime's
existing workspace boundary before being passed into this module.
"""

from __future__ import annotations

import subprocess
import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .errors import ToolFailure


GIT_LOCATION_ENV = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES", "GIT_PREFIX", "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
})
_locks_guard = threading.Lock()
_write_locks: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()


def git_environment(env: dict[str, str]) -> dict[str, str]:
    """Keep credentials/configuration, but never inherit another repo's cwd."""
    result = {key: value for key, value in env.items() if key not in GIT_LOCATION_ENV}
    result["GIT_LITERAL_PATHSPECS"] = "1"
    return result


@dataclass(frozen=True)
class RepositoryContext:
    root: Path
    git_dir: Path
    common_dir: Path

    def metadata(self) -> dict[str, str]:
        return {"repo_root": str(self.root), "path_base": "repo_root"}


def discover_repository(
    workspace: Path, target: Path, *, git: str, env: dict[str, str], required: bool = False,
) -> RepositoryContext | None:
    """Find the actual worktree, including deleted paths and .git files."""
    candidate = target if target.is_dir() else target.parent
    while not candidate.exists() and candidate != workspace:
        candidate = candidate.parent
    if not candidate.is_relative_to(workspace):
        raise ToolFailure("GIT_REPOSITORY_OUTSIDE_WORKSPACE", "Repository target is outside the workspace.", category="security")
    def query(option: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [git, "-C", str(candidate), "rev-parse", option],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolFailure("GIT_ERROR", f"Could not resolve Git repository: {exc}", category="runtime") from exc

    completed = query("--show-toplevel")
    if completed.returncode != 0:
        if required:
            raise ToolFailure(
                "GIT_NOT_REPOSITORY", completed.stderr.strip() or "Target is not a Git worktree.",
                category="validation", details={"path": str(target), "retry_hint": "Pass repo_path for one existing repository inside the workspace."},
            )
        return None
    # Separate queries preserve directory names containing newlines.
    root = Path(completed.stdout.removesuffix("\n")).resolve()
    if not root.is_relative_to(workspace):
        raise ToolFailure("GIT_REPOSITORY_OUTSIDE_WORKSPACE", "Git worktree root is outside the configured workspace.", category="security")
    git_dir_result = query("--absolute-git-dir")
    common = query("--git-common-dir")
    if git_dir_result.returncode != 0 or common.returncode != 0:
        raise ToolFailure("GIT_ERROR", "Could not resolve Git metadata directories.", category="runtime")
    git_dir = Path(git_dir_result.stdout.removesuffix("\n")).resolve()
    common_path = Path(common.stdout.removesuffix("\n"))
    return RepositoryContext(root, git_dir, (candidate / common_path).resolve())


@contextmanager
def repository_write_lock(repo: RepositoryContext) -> Iterator[None]:
    """Coordinate MCP check-and-write calls, not arbitrary external writers."""
    key = str(repo.common_dir)
    with _locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _write_locks[key] = lock
    with lock:
        yield
