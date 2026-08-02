"""应用层禁止依赖基础设施与具体持久化实现。"""

from __future__ import annotations

import ast
from pathlib import Path


def test_conversation_application_has_no_concrete_infrastructure_imports() -> None:
    """验证 Conversation application 只依赖抽象 interface 和领域值。"""
    root = Path("src/conversation/application")
    forbidden = (
        "infrastructure",
        "memory.mysql",
        "sqlalchemy",
        "settings",
    )
    violations: list[str] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            for imported in imports:
                if imported.startswith(forbidden):
                    violations.append(f"{path}:{node.lineno}:{imported}")

    assert violations == []


def test_memory_application_has_no_concrete_adapter_imports() -> None:
    """验证 Memory application 只依赖 ports、领域值与稳定共享契约。"""
    root = Path("src/memory/application")
    forbidden = (
        "infrastructure",
        "memory.adapters",
        "memory.indexing",
        "memory.mysql",
        "settings",
        "elasticsearch",
        "huggingface_hub",
        "qdrant_client",
        "sqlalchemy",
    )
    violations: list[str] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            else:
                continue
            for imported in imports:
                if imported.startswith(forbidden):
                    violations.append(f"{path}:{node.lineno}:{imported}")

    assert violations == []
