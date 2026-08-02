"""回答数据依赖、意图和门禁结果契约。"""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from models.base import ContractModel


class AnswerDataDependency(ContractModel):
    """一次回答依赖的 DW 目标表。"""

    target_table: str = Field(
        min_length=1,
        max_length=64,
        description="回答依赖的不带来源前缀的 DW 目标表名称。",
    )
    source: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="问题明确限定的数据源；未限定来源或汇总查询时为空。",
    )


class AnswerDataTarget(ContractModel):
    """允许意图识别使用的一张 DW 目标表及其来源。"""

    target_table: str = Field(
        min_length=1,
        max_length=64,
        description="允许回答依赖的 DW 目标表名称。",
    )
    sources: list[str] = Field(
        min_length=1,
        max_length=50,
        description="允许该目标表使用的命名数据源。",
    )

    @model_validator(mode="after")
    def validate_unique_sources(self) -> Self:
        """拒绝重复来源，保持提示目录确定且有界。"""
        if len(self.sources) != len(set(self.sources)):
            raise ValueError("目标表来源不得重复")
        if any(not source or len(source) > 128 for source in self.sources):
            raise ValueError("目标表来源长度必须在 1 到 128 之间")
        return self


class AnswerTargetCatalog(ContractModel):
    """当前问题可使用的有界 DW 目标目录。"""

    targets: list[AnswerDataTarget] = Field(
        max_length=50,
        description="允许意图识别引用的 DW 目标表目录。",
    )

    @model_validator(mode="after")
    def validate_unique_targets(self) -> Self:
        """拒绝重复目标表，避免同一依赖出现互相冲突的来源目录。"""
        names = [target.target_table for target in self.targets]
        if len(names) != len(set(names)):
            raise ValueError("目标表目录不得重复")
        return self

    def accepts(self, dependency: AnswerDataDependency) -> bool:
        """判断依赖是否完全来自当前允许目录。"""
        for target in self.targets:
            if target.target_table != dependency.target_table:
                continue
            return dependency.source is None or dependency.source in target.sources
        return False


class AnswerReadinessIntent(ContractModel):
    """独立意图识别节点产生的数据同步等待判断。"""

    requires_sync_completion: bool = Field(
        description="当前问题是否必须等待依赖的 DW 数据全部同步完成。",
    )
    dependencies: list[AnswerDataDependency] = Field(
        max_length=20,
        description="回答完整性依赖的去重 DW 目标表。",
    )
    reason: str = Field(
        min_length=1,
        max_length=512,
        description="仅供内部诊断使用的简短判断依据。",
    )

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        """校验等待标记、依赖数量和目标唯一性。"""
        if self.requires_sync_completion != bool(self.dependencies):
            raise ValueError("等待标记必须与依赖列表是否为空一致")
        targets = [dependency.target_table for dependency in self.dependencies]
        if len(targets) != len(set(targets)):
            raise ValueError("同一目标表只能声明一次依赖")
        return self

    def validate_catalog(self, catalog: AnswerTargetCatalog) -> None:
        """拒绝模型臆造的目标表或来源。"""
        if any(not catalog.accepts(dependency) for dependency in self.dependencies):
            raise ValueError("回答依赖不在允许目录中")


class DataReadinessToolInput(ContractModel):
    """数据就绪工具的有界输入。"""

    dependencies: list[AnswerDataDependency] = Field(
        min_length=1,
        max_length=1000,
        description="本次回答必须满足的全部 DW 数据依赖。",
    )


class DataReadinessToolResult(ContractModel):
    """数据就绪工具返回给确定性路由的最小结果。"""

    ready: bool = Field(description="全部必需同步任务是否均已进入实时同步阶段。")


class AnswerGateDecision(StrEnum):
    """回答门禁的确定性路由结果。"""

    PROCEED = "proceed"
    DATA_PREPARING = "data_preparing"
    INTENT_UNRESOLVED = "intent_unresolved"


class AnswerGateResult(ContractModel):
    """供未来回答入口消费的安全门禁结果。"""

    decision: AnswerGateDecision = Field(description="确定性的后续回答路由。")
    user_message: str | None = Field(
        default=None,
        max_length=64,
        description="拒绝继续回答时可直接展示的固定安全提示。",
    )
