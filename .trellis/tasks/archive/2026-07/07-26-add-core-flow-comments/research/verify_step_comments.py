"""验证生产代码说明性行内注释统一使用中文编号步骤。"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = ROOT / "src" / "data_agent"
STEP_PATTERN = re.compile(r"#\s*步骤(?:[一二三四五六七八九十]+|\d+)[：:]")
DIRECTIVE_PATTERN = re.compile(
    r"#\s*(?:noqa\b|type:\s*ignore\b|pragma:\b|coverage:\b|fmt:\b|ruff:\b)",
    re.IGNORECASE,
)


def comment_groups(path: Path) -> list[list[tokenize.TokenInfo]]:
    """按缩进和连续行聚合注释 token。"""
    source = path.read_bytes()
    comments = [
        token
        for token in tokenize.tokenize(io.BytesIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    groups: list[list[tokenize.TokenInfo]] = []
    for token in comments:
        if (
            groups
            and groups[-1][-1].start[1] == token.start[1]
            and groups[-1][-1].end[0] + 1 == token.start[0]
        ):
            groups[-1].append(token)
        else:
            groups.append([token])
    return groups


def main() -> None:
    """报告未编号说明性注释和不允许的行尾说明注释。"""
    violations: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for group in comment_groups(path):
            first = group[0]
            text = first.string
            relative = path.relative_to(ROOT).as_posix()
            prefix = lines[first.start[0] - 1][: first.start[1]]
            if prefix.strip():
                if not DIRECTIVE_PATTERN.match(text):
                    violations.append(
                        f"{relative}:{first.start[0]} 行尾说明注释必须删除或改为编号步骤"
                    )
                continue
            if DIRECTIVE_PATTERN.match(text) or STEP_PATTERN.match(text):
                continue
            violations.append(
                f"{relative}:{first.start[0]} 说明注释组首行缺少步骤编号"
            )

    if violations:
        raise SystemExit("FAIL step comments:\n" + "\n".join(violations))
    print("PASS: production explanatory comments use numbered steps.")


if __name__ == "__main__":
    main()
