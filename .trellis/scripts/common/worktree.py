#!/usr/bin/env python3
"""提供任务创建策略与只读 Git 工作树校验。"""

from __future__ import annotations

import ntpath
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .git import run_git

TaskCreationPolicy = Literal["codex_host_managed", "trellis_managed"]

_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class WorktreeVerificationError(RuntimeError):
    """表示声明的 Codex 工作树不是合规的宿主管理检出。"""


@dataclass(frozen=True)
class GitWorktreeState:
    """保存当前 Git 检出与工作树登记表的只读事实。"""

    current_root: Path
    primary_root: Path
    registered_roots: tuple[Path, ...]

    @property
    def is_registered(self) -> bool:
        """返回当前检出是否已出现在 Git 工作树登记表中。"""
        return any(
            paths_equivalent(self.current_root, candidate)
            for candidate in self.registered_roots
        )

    @property
    def is_linked(self) -> bool:
        """返回当前检出是否为已登记的非主工作树。"""
        return self.is_registered and not paths_equivalent(
            self.current_root, self.primary_root
        )


def normalize_platform(platform: str | None) -> str | None:
    """规范化显式任务创建平台标记。"""
    if platform is None:
        return None
    normalized = platform.strip().lower().replace("_", "-")
    return normalized or None


def resolve_task_creation_platform(
    explicit_platform: str | None,
) -> str | None:
    """仅解析 CLI 传入的显式任务创建标记。

    ``CODEX_SESSION_ID``、``TRELLIS_TASK_PLATFORM`` 等环境变量不会选择
    Codex 宿主管理模式，确保会话身份或残留环境与任务工作树所有权这一
    生命周期决策相互隔离。
    """
    return normalize_platform(explicit_platform)


def resolve_task_creation_policy(platform: str | None) -> TaskCreationPolicy:
    """仅在显式 Codex 标记存在时选择 Codex 宿主所有权。"""
    if normalize_platform(platform) == "codex":
        return "codex_host_managed"
    return "trellis_managed"


def _comparison_key(path: str | Path) -> str:
    raw = str(path).strip()
    if _WINDOWS_ABSOLUTE_RE.match(raw) or "\\" in raw:
        return f"windows:{ntpath.normcase(ntpath.normpath(raw))}"
    try:
        resolved = Path(raw).resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = Path(os.path.abspath(raw))
    return f"native:{os.path.normcase(os.path.normpath(str(resolved)))}"


def paths_equivalent(left: str | Path, right: str | Path) -> bool:
    """稳定比较原生路径或 Windows 风格路径。"""
    return _comparison_key(left) == _comparison_key(right)


def _git_output(args: list[str], repo_root: Path, description: str) -> str:
    returncode, stdout, stderr = run_git(args, cwd=repo_root)
    if returncode != 0:
        detail = stderr.strip() or stdout.strip() or "unknown Git error"
        raise WorktreeVerificationError(f"Cannot {description}: {detail}")
    return stdout.strip("\0\r\n ")


def _resolve_git_path(raw_path: str, repo_root: Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve(strict=False)


def parse_worktree_roots(porcelain_output: str) -> tuple[Path, ...]:
    """从 ``git worktree list --porcelain -z`` 输出解析工作树根目录。"""
    roots: list[Path] = []
    for field in porcelain_output.split("\0"):
        if field.startswith("worktree "):
            roots.append(Path(field.removeprefix("worktree ")))
    return tuple(roots)


def inspect_git_worktree(repo_root: Path) -> GitWorktreeState:
    """检查当前检出，但不创建或删除工作树。"""
    requested_root = repo_root.resolve(strict=False)
    current_raw = _git_output(
        ["rev-parse", "--show-toplevel"],
        requested_root,
        "resolve the current repository root",
    )
    current_root = _resolve_git_path(current_raw, requested_root)
    if not paths_equivalent(requested_root, current_root):
        raise WorktreeVerificationError(
            "The requested repository root does not match Git's current checkout: "
            f"{requested_root} != {current_root}"
        )

    common_raw = _git_output(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        current_root,
        "resolve Git's common directory",
    )
    common_dir = _resolve_git_path(common_raw, current_root)
    registry_raw = _git_output(
        ["worktree", "list", "--porcelain", "-z"],
        current_root,
        "read Git's worktree registry",
    )
    registered_roots = tuple(
        _resolve_git_path(str(path), current_root)
        for path in parse_worktree_roots(registry_raw)
    )
    # Git lists the main worktree first. Keep the common-directory parent only
    # as a fallback for an unexpectedly empty registry.
    primary_root = registered_roots[0] if registered_roots else common_dir.parent
    return GitWorktreeState(
        current_root=current_root,
        primary_root=primary_root,
        registered_roots=registered_roots,
    )


def validate_codex_host_worktree(state: GitWorktreeState) -> GitWorktreeState:
    """仅允许已登记的 linked worktree。"""
    if paths_equivalent(state.current_root, state.primary_root):
        raise WorktreeVerificationError(
            "Codex host-managed task creation cannot run in the primary checkout. "
            "Return to the main Codex session and load the `trellis-create-task` "
            "skill so Codex can call create_thread with a worktree environment."
        )
    if not state.is_registered:
        raise WorktreeVerificationError(
            "The current checkout is absent from `git worktree list --porcelain`. "
            "Return to the main Codex session and create the task through the "
            "`trellis-create-task` skill."
        )
    return state


def verify_codex_host_worktree(repo_root: Path) -> GitWorktreeState:
    """检查并验证 Codex 宿主提供的 linked worktree。"""
    return validate_codex_host_worktree(inspect_git_worktree(repo_root))
