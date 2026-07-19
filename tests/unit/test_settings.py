"""应用配置加载与字段说明检查。"""

from pathlib import Path

from data_agent.settings import AppSettings, SettingsModel, app_config
from tests.helpers.checks import check_condition, check_equal


def _collect_settings_descriptions(
    model: type[SettingsModel],
    path: str = "AppSettings",
) -> tuple[list[str], list[str]]:
    """递归收集配置模型中缺少的字段和已有说明。"""
    missing: list[str] = []
    descriptions: list[str] = []
    for name, field in model.model_fields.items():
        field_path = f"{path}.{name}"
        description = field.description
        if not isinstance(description, str) or not description.strip():
            missing.append(field_path)
        else:
            descriptions.append(description)

        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, SettingsModel):
            nested_missing, nested_descriptions = _collect_settings_descriptions(
                annotation,
                field_path,
            )
            missing.extend(nested_missing)
            descriptions.extend(nested_descriptions)
    return missing, descriptions


def test_default_app_config_loads_expected_values() -> None:
    """验证仓库默认 YAML 配置能够加载为预期的运行时值。"""
    check_equal(
        "test_default_app_config_loads_expected_values 文件日志目录",
        app_config.logging.file.path,
        Path("logs"),
    )
    check_equal(
        "test_default_app_config_loads_expected_values Qdrant 地址",
        app_config.qdrant.url,
        "http://localhost:6333",
    )
    check_equal(
        "test_default_app_config_loads_expected_values Elasticsearch 地址",
        app_config.elasticsearch.url,
        "http://localhost:9200",
    )
    check_equal(
        "test_default_app_config_loads_expected_values TEI 地址",
        app_config.tei.url,
        "http://localhost:8080",
    )
    check_condition(
        "test_default_app_config_loads_expected_values MySQL 驱动",
        app_config.mysql.url.startswith("mysql+asyncmy://"),
        actual=app_config.mysql.url,
        expected="以 mysql+asyncmy:// 开头",
    )
    check_equal(
        "test_default_app_config_loads_expected_values 记忆数据库",
        app_config.memory.database,
        "data_agent",
    )
    check_equal(
        "test_default_app_config_loads_expected_values API 监听地址",
        app_config.api.host,
        "127.0.0.1",
    )
    check_condition(
        "test_default_app_config_loads_expected_values Redis 协议",
        app_config.redis.url.startswith("redis://"),
        actual=app_config.redis.url,
        expected="以 redis:// 开头",
    )
    check_condition(
        "test_default_app_config_loads_expected_values 结构化输出方式",
        app_config.llm.structured_output_method in {"json_schema", "function_calling"},
        actual=app_config.llm.structured_output_method,
        expected="json_schema 或 function_calling",
    )


def test_all_settings_fields_have_chinese_descriptions() -> None:
    """递归验证根配置及嵌套配置的每个字段都有中文说明。"""
    missing, descriptions = _collect_settings_descriptions(AppSettings)
    check_equal(
        "test_all_settings_fields_have_chinese_descriptions 缺失字段",
        sorted(set(missing)),
        [],
    )
    check_condition(
        "test_all_settings_fields_have_chinese_descriptions 说明集合非空",
        bool(descriptions),
        actual=len(descriptions),
        expected="大于 0",
    )
    check_condition(
        "test_all_settings_fields_have_chinese_descriptions 递归覆盖嵌套模型",
        len(descriptions) > len(AppSettings.model_fields),
        actual=len(descriptions),
        expected=f"大于根配置字段数 {len(AppSettings.model_fields)}",
    )
    check_condition(
        "test_all_settings_fields_have_chinese_descriptions 中文说明",
        all(
            any("\u4e00" <= character <= "\u9fff" for character in description)
            for description in descriptions
        ),
        actual=descriptions,
        expected="每条字段说明至少包含一个中文字符",
    )
