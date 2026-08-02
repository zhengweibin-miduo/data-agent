"""应用配置文件路径解析与加载入口检查。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import settings as settings_module
from errors import DataAgentError
from settings import (
    CONFIG_PATH_ENV,
    AppSettings,
    app_config,
    get_settings,
    reset_settings,
    resolve_config_path,
)
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)


def _repository_config() -> Path:
    """返回仓库内真实配置文件路径。"""
    return Path(settings_module.__file__).parents[1] / "conf" / "app_config.yaml"


def _restore_process_settings() -> None:
    """把进程内配置缓存恢复为导入时的实例，避免污染同进程其它用例。"""
    reset_settings()
    settings_module._settings = app_config


def test_environment_variable_selects_config_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """DATA_AGENT_CONFIG 指定的文件必须被优先加载。"""
    target = tmp_path / "custom.yaml"
    shutil.copyfile(_repository_config(), target)
    monkeypatch.setenv(CONFIG_PATH_ENV, str(target))

    check_equal("环境变量命中路径", resolve_config_path(), target.resolve())
    check_equal(
        "按环境变量路径加载的配置可校验通过",
        AppSettings.from_yaml().redis.key_prefix,
        app_config.redis.key_prefix,
    )


def test_missing_explicit_config_fails_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """显式指定的路径不存在时必须直接失败，不得静默回退到其它候选。"""
    missing = tmp_path / "absent.yaml"
    monkeypatch.setenv(CONFIG_PATH_ENV, str(missing))
    try:
        resolve_config_path()
    except DataAgentError as error:
        check_exception("显式路径缺失捕获业务错误", error, DataAgentError)
        check_equal("错误码", error.code, "config_not_found")
        check_equal("阶段", error.stage, "settings")
        check_condition(
            "报错指向显式指定的路径",
            str(missing.absolute()) in error.details["searched"],
            actual=error.details["searched"],
            expected=f"包含 {missing.absolute()}",
        )
        check_condition(
            "不回退到仓库内配置",
            str(_repository_config().absolute()) not in error.details["searched"],
            actual=error.details["searched"],
            expected="不包含仓库内配置路径",
        )
    else:
        fail_check(
            "显式路径缺失",
            actual="解析成功",
            expected="抛出 config_not_found",
        )


def test_working_directory_precedes_source_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """未设环境变量时工作目录下的配置优先于源码树配置。"""
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)
    deployment = tmp_path / "conf"
    deployment.mkdir()
    target = deployment / "app_config.yaml"
    shutil.copyfile(_repository_config(), target)
    monkeypatch.chdir(tmp_path)

    check_equal("工作目录配置优先", resolve_config_path(), target.resolve())


def test_all_candidates_missing_reports_searched_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """全部候选缺失时报错必须列出查找过的绝对路径并提示环境变量。"""
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        settings_module,
        "_SOURCE_TREE_CONFIG",
        tmp_path / "absent" / "app_config.yaml",
    )
    try:
        resolve_config_path()
    except DataAgentError as error:
        check_equal("错误码", error.code, "config_not_found")
        check_condition(
            "报错列出工作目录候选",
            str((tmp_path / "conf" / "app_config.yaml").absolute())
            in error.details["searched"],
            actual=error.details["searched"],
            expected="包含工作目录候选绝对路径",
        )
        check_equal("报错提示环境变量名", error.details["env"], CONFIG_PATH_ENV)
    else:
        fail_check(
            "全部候选缺失",
            actual="解析成功",
            expected="抛出 config_not_found",
        )


def test_get_settings_caches_until_reset() -> None:
    """get_settings 在进程内只解析一次，reset_settings 后重新解析。"""
    try:
        first = get_settings()
        check_condition(
            "重复调用复用同一实例",
            get_settings() is first,
            expected="两次调用返回同一对象",
        )
        reset_settings()
        second = get_settings()
        check_condition(
            "重置后重新解析",
            second is not first,
            expected="重置后返回新对象",
        )
        check_equal(
            "重新解析结果与导入时一致",
            second.redis.key_prefix,
            first.redis.key_prefix,
        )
    finally:
        _restore_process_settings()
