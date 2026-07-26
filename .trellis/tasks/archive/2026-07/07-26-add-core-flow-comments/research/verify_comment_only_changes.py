"""验证产品 Python diff 只改变注释和 Docstring，不改变可执行结构。"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
BASE = "origin/master"


class DocstringStripper(ast.NodeTransformer):
    """移除 AST 中不影响执行契约的 Docstring 节点。"""

    def _strip(self, node: ast.AST) -> ast.AST:
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)
        return node

    def visit_Module(self, node: ast.Module) -> ast.AST:
        """移除模块 Docstring。"""
        return self.generic_visit(self._strip(node))

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        """移除类 Docstring。"""
        return self.generic_visit(self._strip(node))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        """移除同步函数 Docstring。"""
        return self.generic_visit(self._strip(node))

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        """移除异步函数 Docstring。"""
        return self.generic_visit(self._strip(node))


def git_text(revision: str, path: str) -> str:
    """读取 Git revision 中的 UTF-8 文本。"""
    result = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def changed_python_paths() -> list[str]:
    """列出相对基线发生变化的产品 Python 文件。"""
    result = subprocess.run(
        ["git", "diff", "--name-only", BASE, "--", "src"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [
        line
        for line in result.stdout.splitlines()
        if line and line.endswith(".py")
    ]


def executable_ast(text: str, path: str) -> str:
    """返回移除 Docstring 后的稳定 AST 表示。"""
    tree = ast.parse(text, filename=path)
    stripped = DocstringStripper().visit(tree)
    ast.fix_missing_locations(stripped)
    return ast.dump(stripped, include_attributes=False)


def main() -> None:
    """校验所有发生变化的产品 Python 文件。"""
    paths = changed_python_paths()
    for path in paths:
        before = executable_ast(git_text(BASE, path), path)
        after = executable_ast((ROOT / path).read_text(encoding="utf-8"), path)
        if before != after:
            raise SystemExit(f"FAIL executable AST changed: {path}")

    print(f"PASS: {len(paths)} Python ASTs unchanged after stripping docstrings.")


if __name__ == "__main__":
    main()
