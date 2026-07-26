"""生成 Python Docstring、普通注释和待办标记的确定性清单。"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OUTPUT = Path(__file__).with_name("python-comment-inventory.md")
MARKER_PATTERN = re.compile(r"\b(?:TODO|FIXME|NOTE|HACK|XXX)\b", re.IGNORECASE)


@dataclass(frozen=True)
class FileInventory:
    """单个 Python 文件的注释统计。"""

    path: str
    docstrings: int
    comments: tuple[tuple[int, str], ...]
    markers: tuple[tuple[int, str], ...]


def count_docstrings(tree: ast.AST) -> int:
    """统计模块、类、函数和异步函数的 Docstring。"""
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, owners) and ast.get_docstring(node, clean=False) is not None
    )


def inspect_file(path: Path) -> FileInventory:
    """读取一个 Python 文件并生成注释清单。"""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    comments: list[tuple[int, str]] = []
    markers: list[tuple[int, str]] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT:
            continue
        value = token.string.strip()
        comments.append((token.start[0], value))
        if MARKER_PATTERN.search(value):
            markers.append((token.start[0], value))
    return FileInventory(
        path=path.relative_to(ROOT).as_posix(),
        docstrings=count_docstrings(tree),
        comments=tuple(comments),
        markers=tuple(markers),
    )


def render(inventories: list[FileInventory]) -> str:
    """渲染 Markdown 清单。"""
    source = [item for item in inventories if item.path.startswith("src/")]
    tests = [item for item in inventories if item.path.startswith("tests/")]
    lines = [
        "# Python 注释确定性清单",
        "",
        "由 `generate_comment_inventory.py` 基于 AST 与 tokenize 生成。",
        "",
        "## 汇总",
        "",
        f"- `src/`：{len(source)} 个文件，"
        f"{sum(item.docstrings for item in source)} 处 Docstring，"
        f"{sum(len(item.comments) for item in source)} 处普通注释。",
        f"- `tests/`：{len(tests)} 个文件，"
        f"{sum(item.docstrings for item in tests)} 处 Docstring，"
        f"{sum(len(item.comments) for item in tests)} 处普通注释。",
        f"- 待办标记：{sum(len(item.markers) for item in inventories)} 处。",
        "",
        "## 逐文件统计",
        "",
        "| 文件 | Docstring | 普通注释 | 待办标记 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for item in inventories:
        lines.append(
            f"| `{item.path}` | {item.docstrings} | {len(item.comments)} | "
            f"{len(item.markers)} |"
        )
    lines.extend(["", "## 普通注释明细", ""])
    comment_count = 0
    for item in inventories:
        for line, text in item.comments:
            comment_count += 1
            lines.append(f"- `{item.path}:{line}`：`{text}`")
    if comment_count == 0:
        lines.append("- 未发现。")
    lines.extend(["", "## 待办标记明细", ""])
    marker_count = 0
    for item in inventories:
        for line, text in item.markers:
            marker_count += 1
            lines.append(f"- `{item.path}:{line}`：`{text}`")
    if marker_count == 0:
        lines.append("- 未发现。")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """生成并保存清单。"""
    paths = sorted(
        [
            *ROOT.glob("src/**/*.py"),
            *ROOT.glob("tests/**/*.py"),
        ]
    )
    inventories = [inspect_file(path) for path in paths]
    OUTPUT.write_text(render(inventories), encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
