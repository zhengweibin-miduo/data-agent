"""日志 AOP 外部织入与业务零标注静态契约检查。"""

from pathlib import Path

from tests.helpers.checks import check_equal

_PROJECT_ROOT = Path(__file__).parents[3]
_SOURCE_ROOT = _PROJECT_ROOT / "src"
_AOP_SEAMS = {
    Path("application.py"),
    Path("data_sync/worker.py"),
    Path("ddl_metadata/worker/settings.py"),
    Path("ddl_metadata/workflow/graph.py"),
    Path("app_logging.py"),
}


def _source_occurrences(fragment: str) -> list[str]:
    """返回指定源码片段的稳定相对路径和行号。"""
    occurrences: list[str] = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if fragment in line:
                occurrences.append(
                    f"{path.relative_to(_SOURCE_ROOT).as_posix()}:{line_number}"
                )
    return occurrences


def test_business_modules_have_no_logging_annotations_or_manual_context() -> None:
    """业务模块不使用日志装饰器、ContextVar primitive 或 Loguru bind。"""
    check_equal(
        "业务源码不存在 @logging_boundary",
        _source_occurrences("@logging_boundary"),
        [],
    )
    check_equal(
        "业务源码不存在 logger.bind",
        _source_occurrences("logger.bind("),
        [],
    )
    direct_context = [
        occurrence
        for occurrence in _source_occurrences("logging_context(")
        if not occurrence.startswith("app_logging.py:")
    ]
    check_equal("业务源码不直接调用 logging_context", direct_context, [])


def test_logging_boundary_is_woven_only_at_framework_seams() -> None:
    """logging_boundary 调用只存在于日志核心和框架注册 seam。"""
    unexpected: list[str] = []
    for occurrence in _source_occurrences("logging_boundary("):
        relative_path = Path(occurrence.rsplit(":", maxsplit=1)[0])
        if relative_path not in _AOP_SEAMS:
            unexpected.append(occurrence)
    check_equal("AOP 仅在允许的框架 seam 外部织入", unexpected, [])
