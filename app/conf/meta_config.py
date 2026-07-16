"""元数据同步配置模型。"""

from pathlib import Path
from typing import Annotated, Literal, Self
from unicodedata import combining, normalize

import yaml
from pydantic import Field, StringConstraints, model_validator

from app.conf.app_config import ConfigModel

Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
MetricName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
ColumnReference = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$"),
]


def _mysql_general_ci_key(value: str) -> str:
    """生成与 utf8mb4_general_ci 大小写及重音规则一致的比较键。"""
    return "".join(
        character
        for character in normalize("NFKD", value.casefold())
        if not combining(character)
    )


class ColumnConfig(ConfigModel):
    """字段元数据配置。"""

    name: Identifier
    role: Literal["primary_key", "foreign_key", "dimension", "measure"]
    description: NonEmptyText
    alias: list[NonEmptyText]
    sync: bool


class TableConfig(ConfigModel):
    """表元数据配置。"""

    name: Identifier
    role: Literal["dim", "fact"]
    description: NonEmptyText
    columns: list[ColumnConfig] = Field(min_length=1)


class MetricConfig(ConfigModel):
    """指标元数据配置。"""

    name: MetricName
    description: NonEmptyText
    relevant_columns: list[ColumnReference] = Field(min_length=1)
    alias: list[NonEmptyText]


class MetaConfig(ConfigModel):
    """元数据同步根配置。"""

    tables: list[TableConfig] = Field(default_factory=list)
    metrics: list[MetricConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        """拒绝重复项和不存在的指标字段引用。"""
        table_names = [table.name for table in self.tables]
        if len(table_names) != len(set(table_names)):
            raise ValueError("tables 中存在重复表名")

        for table in self.tables:
            column_names = [column.name for column in table.columns]
            if len(column_names) != len(set(column_names)):
                raise ValueError(f"表 {table.name} 中存在重复字段名")

        configured_columns = {
            f"{table.name}.{column.name}"
            for table in self.tables
            for column in table.columns
        }

        # meta.metric_info.id 使用 utf8mb4_general_ci；配置侧也必须按其
        # 大小写和重音不敏感规则拒绝冲突，避免各存储产生不同实体数。
        metric_names = [_mysql_general_ci_key(metric.name) for metric in self.metrics]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("metrics 中存在重复指标名")

        for metric in self.metrics:
            if len(metric.relevant_columns) != len(set(metric.relevant_columns)):
                raise ValueError(f"指标 {metric.name} 中存在重复相关字段")
            missing = set(metric.relevant_columns) - configured_columns
            if missing:
                raise ValueError(
                    f"指标 {metric.name} 引用了未配置字段: {', '.join(sorted(missing))}"
                )

        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> "MetaConfig":
        """从 UTF-8 YAML 文件加载并严格校验配置。"""
        with Path(path).open(encoding="utf-8") as file:
            return cls.model_validate(yaml.safe_load(file))
