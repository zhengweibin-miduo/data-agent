"""Meta Projection 包所有权与依赖方向测试。"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = PROJECT_ROOT / "src"
PROJECTION_ROOT = SOURCE_ROOT / "ddl_metadata" / "meta_projection"


def _imported_modules(path: Path) -> set[str]:
    """收集一个 Python 模块的静态导入名称。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_meta_projection_is_owned_by_ddl_metadata() -> None:
    """活动 Meta Projection 只存在于 DDL Metadata 下。"""
    assert PROJECTION_ROOT.is_dir()
    assert not (SOURCE_ROOT / "metadata_indexing").exists()


def test_inner_projection_modules_do_not_import_outer_implementations() -> None:
    """domain/application 不导入跨上下文持久化与外部实现。"""
    forbidden_prefixes = (
        "data_sync",
        "infrastructure",
        "settings",
        "elasticsearch",
        "qdrant_client",
        "sqlalchemy",
    )
    violations: list[str] = []

    inner_paths = [PROJECTION_ROOT / "domain.py"]
    inner_paths.extend((PROJECTION_ROOT / "application").glob("*.py"))
    for path in inner_paths:
        if not path.exists():
            continue
        for module in sorted(_imported_modules(path)):
            if module.startswith(forbidden_prefixes):
                violations.append(f"{path.relative_to(PROJECT_ROOT)}: {module}")

    assert violations == []
