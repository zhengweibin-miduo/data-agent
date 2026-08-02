"""Data Sync application 与 adapter 依赖方向检查。"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]
_PACKAGE = _ROOT / "src" / "data_agent" / "data_sync"
_APPLICATION = _PACKAGE / "application"


def test_application_imports_only_inner_contracts() -> None:
    """Application 不得导入数据库、配置或具体 adapter implementation。"""
    forbidden = (
        "sqlalchemy",
        "data_agent.infrastructure",
        "data_agent.settings",
        "data_agent.data_sync.adapters",
        "data_agent.data_sync.backfill",
        "data_agent.data_sync.binlog",
        "data_agent.data_sync.repository",
        "data_agent.data_sync.schema_sync",
        "data_agent.ddl_metadata.meta_projection.adapters",
    )

    imports = {
        imported for path in _APPLICATION.rglob("*.py") for imported in _imports(path)
    }

    assert not tuple(
        imported for imported in sorted(imports) if imported.startswith(forbidden)
    )


def test_concrete_projection_input_is_selected_outside_backfill() -> None:
    """低层 DW materialization 只依赖中立 projection input interface。"""
    imports = _imports(_PACKAGE / "backfill.py")

    assert (
        "data_agent.ddl_metadata.meta_projection.adapters.mysql_value_input"
        not in imports
    )


def test_retired_service_and_metadata_indexing_paths_are_absent() -> None:
    """硬迁移后不保留旧 service shim 或 metadata_indexing implementation 引用。"""
    assert not (_PACKAGE / "service.py").exists()
    active_text = "\n".join(
        path.read_text(encoding="utf-8") for path in _PACKAGE.rglob("*.py")
    )
    assert "data_agent.metadata_indexing" not in active_text


def _imports(path: Path) -> set[str]:
    """返回 Python 文件中的绝对 import module 集合。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules
