"""测试统一的 Trellis 任务工作树创建契约。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.task_store import cmd_create  # noqa: E402
from common.tasks import iter_active_tasks  # noqa: E402
from common.workflow_phase import filter_platform, get_step  # noqa: E402


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


def _task_cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """在一次性仓库中运行真实的 task.py CLI。"""
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "task.py"), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


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
    """为测试创建一个主仓库和一个 Trellis linked worktree。"""

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
            "fix/test-worktree",
            str(self.linked),
            "HEAD",
        )
        return self

    def __exit__(self, *exc_info: object) -> None:
        """删除一次性仓库目录树。"""
        self._temporary_directory.cleanup()


class TaskCreationContractTests(unittest.TestCase):
    """验证 CLI、元数据和写入前门禁。"""

    def _args(self, **overrides: object) -> argparse.Namespace:
        """构造直接调用 cmd_create 所需的参数。"""
        values: dict[str, object] = {
            "title": "Trellis worktree metadata",
            "slug": "trellis-worktree-metadata",
            "assignee": "tester",
            "priority": "P2",
            "description": "Verify deterministic task metadata.",
            "parent": None,
            "package": None,
            "no_start": True,
            "base_branch": "master",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_create_help_requires_base_and_exposes_no_platform_selector(self) -> None:
        """CLI 帮助只公开平台无关的显式 PR base 契约。"""
        result = _task_cli(SCRIPTS_DIR.parents[1], "create", "--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn("--base-branch", result.stdout)
        self.assertNotIn("--platform", result.stdout)

    def test_legacy_platform_argument_fails_before_task_files_are_written(self) -> None:
        """旧 Codex ownership 参数由 argparse 失败关闭。"""
        with DisposableWorktree() as repository:
            result = _task_cli(
                repository.linked,
                "create",
                "Legacy host route",
                "--slug",
                "legacy-host-route",
                "--base-branch",
                "master",
                "--platform",
                "codex",
                "--no-start",
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("unrecognized arguments: --platform codex", result.stderr)
            self.assertFalse((repository.linked / ".trellis" / "tasks").exists())

    def test_missing_base_fails_before_task_files_are_written(self) -> None:
        """缺少显式 PR base 时不创建任务文件。"""
        with DisposableWorktree() as repository:
            with _working_directory(repository.linked):
                result = cmd_create(self._args(base_branch=None))

            self.assertEqual(result, 1)
            self.assertFalse((repository.linked / ".trellis" / "tasks").exists())

    def test_detached_head_fails_before_task_files_are_written(self) -> None:
        """Detached HEAD 在任何任务写入前失败。"""
        with DisposableWorktree() as repository:
            _git(repository.linked, "checkout", "--detach")
            with _working_directory(repository.linked):
                result = cmd_create(self._args())

            self.assertEqual(result, 1)
            self.assertFalse((repository.linked / ".trellis" / "tasks").exists())

    def test_create_writes_actual_trellis_metadata_and_preserves_unknown_fields(
        self,
    ) -> None:
        """重复创建刷新权威字段且保留未知扩展字段。"""
        with DisposableWorktree() as repository:
            with _working_directory(repository.linked):
                result = cmd_create(self._args())
                self.assertEqual(result, 0)
                task_dir = next(
                    path
                    for path in (repository.linked / ".trellis" / "tasks").glob(
                        "*-trellis-worktree-metadata"
                    )
                    if path.is_dir()
                )
                task_json = task_dir / "task.json"
                data = json.loads(task_json.read_text(encoding="utf-8"))
                self.assertEqual(data["branch"], "fix/test-worktree")
                self.assertEqual(data["base_branch"], "master")
                self.assertEqual(
                    Path(data["worktree_path"]).resolve(strict=False),
                    repository.linked.resolve(strict=False),
                )
                self.assertEqual(data["meta"]["worktree_owner"], "trellis")
                self.assertEqual(
                    data["meta"]["task_creation_policy"],
                    "trellis_managed",
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
            self.assertEqual(recreated["meta"]["external_reference"], "keep-me")
            self.assertEqual(recreated["custom_top_level"], {"keep": True})

    def test_child_without_parent_metadata_fails_before_creation(self) -> None:
        """任何平台的孤立 child 都在写入前失败。"""
        with DisposableWorktree() as repository:
            with _working_directory(repository.linked):
                result = cmd_create(
                    self._args(slug="orphan-child", parent="07-30-missing-parent")
                )

            self.assertEqual(result, 1)
            self.assertFalse((repository.linked / ".trellis" / "tasks").exists())

    def test_child_with_unreadable_parent_metadata_fails_before_creation(self) -> None:
        """损坏的父元数据不能在子任务写入后才暴露。"""
        for payload in (
            "not-json\n",
            "[]\n",
            json.dumps({"id": "parent", "children": "invalid"}) + "\n",
        ):
            with self.subTest(payload=payload):
                with DisposableWorktree() as repository:
                    tasks_dir = repository.linked / ".trellis" / "tasks"
                    parent_dir = tasks_dir / "07-30-broken-parent"
                    parent_dir.mkdir(parents=True)
                    parent_json = parent_dir / "task.json"
                    parent_json.write_text(payload, encoding="utf-8")

                    with _working_directory(repository.linked):
                        result = cmd_create(
                            self._args(
                                slug="blocked-child",
                                parent=parent_dir.name,
                            )
                        )

                    self.assertEqual(result, 1)
                    self.assertEqual(
                        parent_json.read_text(encoding="utf-8"),
                        payload,
                    )
                    self.assertEqual(list(tasks_dir.glob("*-blocked-child")), [])

    def test_child_with_parent_metadata_writes_bidirectional_link(self) -> None:
        """存在父元数据时同时写入 parent 和 child 两侧关系。"""
        with DisposableWorktree() as repository:
            tasks_dir = repository.linked / ".trellis" / "tasks"
            parent_dir = tasks_dir / "07-30-parent"
            parent_dir.mkdir(parents=True)
            parent_json = parent_dir / "task.json"
            parent_json.write_text(
                json.dumps({"id": "parent", "children": []}) + "\n",
                encoding="utf-8",
            )

            with _working_directory(repository.linked):
                result = cmd_create(
                    self._args(slug="linked-child", parent=parent_dir.name)
                )

            self.assertEqual(result, 0)
            child_dir = next(tasks_dir.glob("*-linked-child"))
            child_data = json.loads(
                (child_dir / "task.json").read_text(encoding="utf-8")
            )
            parent_data = json.loads(parent_json.read_text(encoding="utf-8"))
            self.assertEqual(child_data["parent"], parent_dir.name)
            self.assertIn(child_dir.name, parent_data["children"])

    def test_historical_codex_metadata_survives_read_and_archive_paths(self) -> None:
        """通用生命周期路径接受历史 Codex owner/policy 且不迁移。"""
        with DisposableWorktree() as repository:
            tasks_dir = repository.linked / ".trellis" / "tasks"
            legacy_dir = tasks_dir / "07-30-legacy-codex-task"
            legacy_dir.mkdir(parents=True)
            legacy_json = legacy_dir / "task.json"
            legacy_json.write_text(
                json.dumps(
                    {
                        "id": "legacy-codex-task",
                        "name": "legacy-codex-task",
                        "title": "Legacy Codex task",
                        "status": "planning",
                        "assignee": "tester",
                        "children": [],
                        "parent": None,
                        "meta": {
                            "worktree_owner": "codex",
                            "task_creation_policy": "codex_host_managed",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            before = legacy_json.read_bytes()

            tasks = list(iter_active_tasks(tasks_dir))
            list_result = _task_cli(repository.linked, "list")

            self.assertEqual([task.dir_name for task in tasks], [legacy_dir.name])
            self.assertEqual(list_result.returncode, 0, list_result.stderr)
            self.assertIn(legacy_dir.name, list_result.stdout)
            self.assertEqual(legacy_json.read_bytes(), before)

            with patch.dict(
                os.environ,
                {"TRELLIS_CONTEXT_ID": "legacy-codex-metadata-test"},
            ):
                start_result = _task_cli(
                    repository.linked,
                    "start",
                    legacy_dir.name,
                )
                self.assertEqual(start_result.returncode, 0, start_result.stderr)
                started_data = json.loads(legacy_json.read_text(encoding="utf-8"))
                self.assertEqual(started_data["meta"]["worktree_owner"], "codex")
                self.assertEqual(
                    started_data["meta"]["task_creation_policy"],
                    "codex_host_managed",
                )
                after_start = legacy_json.read_bytes()

                current_result = _task_cli(
                    repository.linked,
                    "current",
                    "--source",
                )
                self.assertEqual(current_result.returncode, 0, current_result.stderr)
                self.assertIn(legacy_dir.name, current_result.stdout)
                self.assertEqual(legacy_json.read_bytes(), after_start)

                validate_result = _task_cli(
                    repository.linked,
                    "validate",
                    legacy_dir.name,
                )

            self.assertEqual(validate_result.returncode, 0, validate_result.stderr)
            self.assertEqual(legacy_json.read_bytes(), after_start)

            archive_result = _task_cli(
                repository.linked,
                "archive",
                legacy_dir.name,
                "--no-commit",
            )

            self.assertEqual(archive_result.returncode, 0, archive_result.stderr)
            archived_jsons = list(
                (tasks_dir / "archive").glob(f"*/{legacy_dir.name}/task.json")
            )
            self.assertEqual(len(archived_jsons), 1)
            archived_data = json.loads(
                archived_jsons[0].read_text(encoding="utf-8")
            )
            self.assertEqual(archived_data["meta"]["worktree_owner"], "codex")
            self.assertEqual(
                archived_data["meta"]["task_creation_policy"],
                "codex_host_managed",
            )


class WorkflowContractTests(unittest.TestCase):
    """验证所有平台共享 Trellis Phase 1.0。"""

    def test_phase_one_uses_trellis_worktree_flow_for_every_platform(self) -> None:
        """Codex 与非 Codex 都只暴露 Trellis-managed 创建流程。"""
        step = get_step("1.0")

        for platform in ("codex-inline", "codex-sub-agent", "Claude Code"):
            with self.subTest(platform=platform):
                rendered = filter_platform(step, platform)
                self.assertIn('git worktree add -b "', rendered)
                self.assertNotIn("create_thread", rendered)
                self.assertNotIn("target.environment.type", rendered)
                self.assertNotIn("trellis-create-task", rendered)
                self.assertNotIn("--platform codex", rendered)
