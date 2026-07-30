"""测试任务创建策略与 Git 工作树校验。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.task_store import cmd_create  # noqa: E402
from common.workflow_phase import filter_platform, get_step  # noqa: E402
from common.worktree import (  # noqa: E402
    WorktreeVerificationError,
    inspect_git_worktree,
    paths_equivalent,
    resolve_task_creation_platform,
    resolve_task_creation_policy,
    validate_codex_host_worktree,
    verify_codex_host_worktree,
)


def _git(repo: Path, *args: str) -> str:
    """在一次性仓库中运行 Git 并返回标准输出。"""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout.strip()


@contextmanager
def _working_directory(path: Path):
    """临时切换进程工作目录。"""
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class DisposableWorktree:
    """为测试创建一个主仓库和一个 linked worktree。"""

    def __init__(self) -> None:
        """分配临时的主检出与 linked worktree 路径。"""
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.primary = self.root / "primary"
        self.linked = self.root / "linked"

    def __enter__(self) -> "DisposableWorktree":
        """初始化并返回一次性 linked-worktree 仓库。"""
        self.primary.mkdir()
        _git(self.primary, "init", "-b", "master")
        _git(self.primary, "config", "user.email", "trellis@example.test")
        _git(self.primary, "config", "user.name", "Trellis Test")
        trellis_dir = self.primary / ".trellis"
        trellis_dir.mkdir()
        (trellis_dir / ".developer").write_text("name=tester\n", encoding="utf-8")
        (trellis_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
        (self.primary / "tracked.txt").write_text("initial\n", encoding="utf-8")
        _git(self.primary, "add", ".")
        _git(self.primary, "commit", "-m", "initial")
        _git(
            self.primary,
            "worktree",
            "add",
            "-b",
            "feature/test-worktree",
            str(self.linked),
            "HEAD",
        )
        return self

    def __exit__(self, *exc_info: object) -> None:
        """删除一次性仓库目录树。"""
        self._temporary_directory.cleanup()


class WorktreePolicyTests(unittest.TestCase):
    """验证显式平台策略与只读工作树守卫。"""

    def test_codex_session_identity_does_not_select_host_ownership(self) -> None:
        """确保会话身份不会选择任务创建所有权。"""
        with patch.dict(
            os.environ,
            {
                "CODEX_SESSION_ID": "session-only",
                "TRELLIS_TASK_PLATFORM": "codex",
            },
        ):
            platform = resolve_task_creation_platform(None)
            self.assertIsNone(platform)
            self.assertEqual(
                resolve_task_creation_policy(platform),
                "trellis_managed",
            )

    def test_explicit_codex_marker_selects_host_ownership(self) -> None:
        """仅通过专用标记选择 Codex 所有权。"""
        platform = resolve_task_creation_platform(" CoDeX ")
        self.assertEqual(platform, "codex")
        self.assertEqual(
            resolve_task_creation_policy(platform),
            "codex_host_managed",
        )

    def test_primary_checkout_is_rejected(self) -> None:
        """拒绝在项目主检出中执行 Codex 引导。"""
        with DisposableWorktree() as repository:
            with self.assertRaises(WorktreeVerificationError):
                verify_codex_host_worktree(repository.primary)

    def test_registered_linked_worktree_is_accepted(self) -> None:
        """接受 Git 登记表中不由 Trellis 管理的 linked worktree。"""
        with DisposableWorktree() as repository:
            state = verify_codex_host_worktree(repository.linked)
            self.assertTrue(state.is_linked)
            self.assertTrue(paths_equivalent(state.current_root, repository.linked))

    def test_registry_mismatch_is_rejected(self) -> None:
        """当登记表缺少当前路径时拒绝伪 linked 状态。"""
        with DisposableWorktree() as repository:
            state = inspect_git_worktree(repository.linked)
            missing_registry = replace(state, registered_roots=())
            with self.assertRaises(WorktreeVerificationError):
                validate_codex_host_worktree(missing_registry)

    def test_windows_style_paths_compare_stably(self) -> None:
        """稳定兼容斜杠方向与盘符大小写差异。"""
        self.assertTrue(
            paths_equivalent(
                r"C:\Projects\Data-Agent\Task",
                "c:/projects/data-agent/task",
            )
        )
        self.assertFalse(
            paths_equivalent(
                r"C:\Projects\Data-Agent\Task",
                r"D:\Projects\Data-Agent\Task",
            )
        )


class TaskMetadataTests(unittest.TestCase):
    """验证任务创建写入真实检出与所有权元数据。"""

    def _args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "title": "Host worktree metadata",
            "slug": "host-worktree-metadata",
            "assignee": "tester",
            "priority": "P2",
            "description": "Verify deterministic task metadata.",
            "parent": None,
            "package": None,
            "no_start": True,
            "platform": "codex",
            "base_branch": "master",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_codex_create_writes_actual_metadata_and_preserves_unknown_meta(
        self,
    ) -> None:
        """写入真实 Git 事实并在重复创建时保留未知元数据。"""
        with DisposableWorktree() as repository:
            with _working_directory(repository.linked):
                result = cmd_create(self._args())
                self.assertEqual(result, 0)
                task_dir = next(
                    path
                    for path in (repository.linked / ".trellis" / "tasks").glob(
                        "*-host-worktree-metadata"
                    )
                    if path.is_dir()
                )
                task_json = task_dir / "task.json"
                data = json.loads(task_json.read_text(encoding="utf-8"))
                self.assertEqual(data["branch"], "feature/test-worktree")
                self.assertEqual(data["base_branch"], "master")
                self.assertTrue(
                    paths_equivalent(data["worktree_path"], repository.linked)
                )
                self.assertEqual(data["meta"]["worktree_owner"], "codex")
                self.assertEqual(
                    data["meta"]["task_creation_policy"],
                    "codex_host_managed",
                )

                data["meta"]["external_reference"] = "keep-me"
                data["custom_top_level"] = {"keep": True}
                task_json.write_text(
                    json.dumps(data, indent=2) + "\n",
                    encoding="utf-8",
                )
                second_result = cmd_create(self._args())
                self.assertEqual(second_result, 0)
                recreated = json.loads(task_json.read_text(encoding="utf-8"))
                self.assertEqual(
                    recreated["meta"]["external_reference"],
                    "keep-me",
                )
                self.assertEqual(
                    recreated["custom_top_level"],
                    {"keep": True},
                )

    def test_non_codex_create_records_trellis_ownership(self) -> None:
        """确保所有非 Codex 平台都使用 Trellis 所有权策略。"""
        with DisposableWorktree() as repository:
            with _working_directory(repository.linked):
                result = cmd_create(
                    self._args(
                        slug="claude-worktree-metadata",
                        platform="claude",
                        base_branch="master",
                    )
                )
                self.assertEqual(result, 0)
                task_dir = next(
                    path
                    for path in (repository.linked / ".trellis" / "tasks").glob(
                        "*-claude-worktree-metadata"
                    )
                    if path.is_dir()
                )
                data = json.loads(
                    (task_dir / "task.json").read_text(encoding="utf-8")
                )
                self.assertEqual(data["meta"]["worktree_owner"], "trellis")
                self.assertEqual(
                    data["meta"]["task_creation_policy"],
                    "trellis_managed",
                )
                self.assertEqual(data["base_branch"], "master")

    def test_codex_primary_checkout_fails_before_task_files_are_written(
        self,
    ) -> None:
        """在主检出中失败关闭且不创建元数据。"""
        with DisposableWorktree() as repository:
            with _working_directory(repository.primary):
                result = cmd_create(self._args())
                self.assertEqual(result, 1)
                self.assertFalse((repository.primary / ".trellis" / "tasks").exists())

    def test_codex_missing_base_fails_before_task_files_are_written(self) -> None:
        """缺少显式 PR base 时失败关闭且不创建元数据。"""
        with DisposableWorktree() as repository:
            with _working_directory(repository.linked):
                result = cmd_create(self._args(base_branch=None))
                self.assertEqual(result, 1)
                self.assertFalse((repository.linked / ".trellis" / "tasks").exists())

    def test_codex_child_without_parent_metadata_fails_before_creation(self) -> None:
        """父任务未进入 starting state 时拒绝创建孤立 child。"""
        with DisposableWorktree() as repository:
            with _working_directory(repository.linked):
                result = cmd_create(
                    self._args(
                        slug="orphan-child",
                        parent="07-30-uncommitted-parent",
                    )
                )
                self.assertEqual(result, 1)
                tasks_dir = repository.linked / ".trellis" / "tasks"
                self.assertFalse(tasks_dir.exists())

    def test_codex_verifier_never_invokes_worktree_add(self) -> None:
        """确保 Codex 校验过程只使用只读 Git 命令。"""
        with DisposableWorktree() as repository:
            recorded_args: list[list[str]] = []

            from common import worktree as worktree_module

            real_run_git = worktree_module.run_git

            def recording_run_git(
                args: list[str],
                cwd: Path | None = None,
            ) -> tuple[int, str, str]:
                recorded_args.append(args)
                return real_run_git(args, cwd=cwd)

            with patch.object(worktree_module, "run_git", recording_run_git):
                state = verify_codex_host_worktree(repository.linked)
            self.assertTrue(state.is_linked)
            self.assertFalse(
                any(args[:2] == ["worktree", "add"] for args in recorded_args)
            )


class CodexWorkflowContractTests(unittest.TestCase):
    """验证工作流路由与宿主工具 Skill 保持同步。"""

    def test_phase_one_routes_codex_and_non_codex_separately(self) -> None:
        """仅向 Codex 暴露宿主委托，其他平台保留本地创建。"""
        step = get_step("1.0")
        codex = filter_platform(step, "codex-sub-agent")
        claude = filter_platform(step, "Claude Code")

        self.assertIn("trellis-create-task", codex)
        self.assertIn("create_thread", codex)
        self.assertNotIn('git worktree add -b "', codex)

        self.assertIn('git worktree add -b "', claude)
        self.assertNotIn("target.environment.type", claude)

    def test_codex_skill_records_verified_host_schema_and_handoff(self) -> None:
        """确保本地 Skill 与 Codex 宿主契约一致。"""
        repo_root = SCRIPTS_DIR.parents[1]
        skill_path = (
            repo_root
            / ".agents"
            / "skills"
            / "trellis-create-task"
            / "SKILL.md"
        )
        skill = skill_path.read_text(encoding="utf-8")

        self.assertIn("Call `list_projects` with no arguments.", skill)
        self.assertIn('"type": "project"', skill)
        self.assertIn('"type": "worktree"', skill)
        self.assertIn('"type": "working-tree"', skill)
        self.assertIn('"type": "branch"', skill)
        self.assertIn("Do not pass `model` or `thinking`", skill)
        self.assertIn(
            "the selected `startingState` must contain the parent",
            skill,
        )
        self.assertIn(
            "verify the parent `task.json` exists before running",
            skill,
        )
        self.assertIn('::created-thread{threadId="<threadId>"}', skill)
        self.assertIn(
            '::created-thread{clientThreadId="<clientThreadId>"}',
            skill,
        )
