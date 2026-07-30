"""共享 Pydantic 领域模型字段说明检查。"""

from data_agent.answer_readiness import models as answer_readiness_models
from data_agent.conversation import models as conversation_models
from data_agent.models import jobs, memory, physical, semantic
from data_agent.models.base import ContractModel
from tests.helpers.checks import check_condition, check_equal


def _model_classes() -> list[type[ContractModel]]:
    """收集领域模型模块中的所有契约模型。"""
    modules = (
        answer_readiness_models,
        conversation_models,
        jobs,
        memory,
        physical,
        semantic,
    )
    classes: set[type[ContractModel]] = set()
    for module in modules:
        classes.update(
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and issubclass(value, ContractModel)
            and value is not ContractModel
            and value.__module__ == module.__name__
        )
    return sorted(classes, key=lambda model: model.__name__)


def test_all_domain_model_fields_have_chinese_descriptions() -> None:
    """递归验证领域契约模型的每个字段都有非空中文说明。"""
    missing: list[str] = []
    descriptions: list[str] = []
    for model in _model_classes():
        for name, field in model.model_fields.items():
            field_path = f"{model.__name__}.{name}"
            description = field.description
            if not isinstance(description, str) or not description.strip():
                missing.append(field_path)
            else:
                descriptions.append(description)

    check_equal(
        "test_all_domain_model_fields_have_chinese_descriptions 缺失字段",
        sorted(missing),
        [],
    )
    check_condition(
        "test_all_domain_model_fields_have_chinese_descriptions 中文说明",
        all(
            any("\u4e00" <= character <= "\u9fff" for character in description)
            for description in descriptions
        ),
        actual=len(descriptions),
        expected="每条字段说明至少包含一个中文字符",
    )
