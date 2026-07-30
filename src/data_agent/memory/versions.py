"""按记忆类别隔离内容契约版本。"""

from data_agent.models.memory import BuiltinMemoryCategory
from data_agent.settings import app_config

_DDL_CATEGORIES = {
    BuiltinMemoryCategory.DDL_SEMANTIC.value,
    BuiltinMemoryCategory.DDL_METRIC.value,
}
_ALL_CATEGORIES = {category.value for category in BuiltinMemoryCategory}


def category_content_version(category: str) -> str:
    """返回类别当前使用的内容版本。"""
    if category in _DDL_CATEGORIES:
        return app_config.memory.ddl_semantic_content_version
    return app_config.memory.content_version


def search_content_versions(categories: set[str] | None) -> set[str]:
    """返回一次类别检索允许进入候选池的内容版本。"""
    selected = categories or _ALL_CATEGORIES
    return {category_content_version(category) for category in selected}
