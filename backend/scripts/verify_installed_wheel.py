"""验证构建产物的模块、入口点与源码所有权边界。"""

from __future__ import annotations

import importlib
import logging
import sys
from importlib import metadata
from pathlib import Path
from zipfile import ZipFile

_PACKAGES = (
    "answer_readiness",
    "chat",
    "conversation",
    "data_sync",
    "ddl_metadata",
    "infrastructure",
    "memory",
    "models",
    "persistence",
)
_MODULES = (
    "main",
    "application",
    "settings",
    "app_logging",
    "errors",
    "identifiers",
)
_ENTRY_POINTS = {
    "data-agent-api": "main:main",
    "data-agent-cdc": "data_sync.worker:main",
}


def _verify_wheel_members(wheel_path: Path) -> None:
    """确认 wheel 不包含退役命名空间或前端资源。"""
    with ZipFile(wheel_path) as wheel:
        members = tuple(wheel.namelist())

    forbidden = tuple(
        member
        for member in members
        if member.startswith("data_agent/")
        or "/frontend/" in f"/{member}"
        or member == "logging.py"
    )
    if forbidden:
        raise AssertionError(f"wheel 包含禁止路径：{forbidden}")


def _verify_imports() -> None:
    """确认声明的顶层包和模块均来自已安装分发。"""
    source_root = Path(__file__).resolve().parents[1] / "src"
    for name in (*_PACKAGES, *_MODULES):
        module = importlib.import_module(name)
        origin = getattr(module, "__file__", None)
        if origin is None:
            raise AssertionError(f"{name} 没有可核验的模块来源")
        if Path(origin).resolve().is_relative_to(source_root):
            raise AssertionError(f"{name} 意外从源码树导入：{origin}")

    logging_path = Path(logging.__file__).resolve()
    if logging_path.is_relative_to(source_root):
        raise AssertionError(f"标准库 logging 被后端源码遮蔽：{logging_path}")


def _verify_entry_points() -> None:
    """确认稳定 CLI 名称指向新的顶层模块。"""
    distribution = metadata.distribution("data-agent")
    actual = {
        entry.name: entry.value
        for entry in distribution.entry_points
        if entry.group == "console_scripts" and entry.name in _ENTRY_POINTS
    }
    if actual != _ENTRY_POINTS:
        raise AssertionError(f"console scripts 不匹配：{actual}")
    for entry in distribution.entry_points:
        if entry.group == "console_scripts" and entry.name in _ENTRY_POINTS:
            entry.load()


def main(wheel_path: str) -> None:
    """执行 installed-wheel 边界烟雾检查。"""
    _verify_wheel_members(Path(wheel_path).resolve())
    _verify_imports()
    _verify_entry_points()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_installed_wheel.py <wheel-path>")
    main(sys.argv[1])
