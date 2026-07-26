"""语义分类、指标与校验契约。"""

from enum import StrEnum

from pydantic import Field

from data_agent.models.base import ContractModel


class TableRole(StrEnum):
    """表语义角色。"""

    FACT = "fact"
    DIM = "dim"


class ColumnRole(StrEnum):
    """列角色。"""

    PRIMARY_KEY = "primary_key"
    FOREIGN_KEY = "foreign_key"
    MEASURE = "measure"
    DIMENSION = "dimension"


class SemanticTable(ContractModel):
    """模型返回并待确定性校验的表语义。"""

    table_id: str = Field(description="表唯一标识。")
    role: TableRole = Field(description="对象角色。")
    description: str = Field(
        min_length=1, max_length=4000, description="对象业务描述。"
    )
    aliases: list[str] = Field(
        default_factory=list, max_length=20, description="对象别名列表。"
    )
    confidence: float = Field(ge=0, le=1, description="模型判断置信度。")
    evidence: list[str] = Field(
        default_factory=list, max_length=20, description="判断依据列表。"
    )


class SemanticColumn(ContractModel):
    """模型返回并待确定性校验的列语义。"""

    column_id: str = Field(description="列唯一标识。")
    role: ColumnRole = Field(description="对象角色。")
    description: str = Field(
        min_length=1, max_length=4000, description="对象业务描述。"
    )
    aliases: list[str] = Field(
        default_factory=list, max_length=20, description="对象别名列表。"
    )
    confidence: float = Field(ge=0, le=1, description="模型判断置信度。")
    evidence: list[str] = Field(
        default_factory=list, max_length=20, description="判断依据列表。"
    )


class SemanticMetadata(ContractModel):
    """结构化表列语义响应。"""

    tables: list[SemanticTable] = Field(description="表列表。")
    columns: list[SemanticColumn] = Field(description="列列表。")


class MetricQuestion(ContractModel):
    """一次指标澄清问题。"""

    question_id: str = Field(min_length=1, max_length=128, description="问题唯一标识。")
    prompt: str = Field(min_length=1, max_length=2000, description="问题文本。")
    fact_table_id: str = Field(description="事实表标识。")
    column_ids: list[str] = Field(
        default_factory=list, max_length=50, description="列标识列表。"
    )
    required: bool = Field(default=True, description="是否必须回答。")


class MetricQuestionSet(ContractModel):
    """模型生成的一轮指标问题。"""

    questions: list[MetricQuestion] = Field(max_length=100, description="问题列表。")


class MetricAnswer(ContractModel):
    """用户对单个指标问题的回答。"""

    question_id: str = Field(description="问题唯一标识。")
    answer: str = Field(min_length=1, max_length=8000, description="用户回答文本。")


class MetricMetadata(ContractModel):
    """最终校验通过的指标元数据。"""

    id: str = Field(default="", description="记录唯一标识。")
    name: str = Field(min_length=1, max_length=128, description="对象名称。")
    fact_table_id: str = Field(description="事实表标识。")
    definition: str = Field(min_length=1, max_length=8000, description="业务定义。")
    relevant_column_ids: list[str] = Field(
        min_length=1, max_length=100, description="相关列标识列表。"
    )
    answer_question_ids: list[str] = Field(
        min_length=1, max_length=100, description="支撑问题标识列表。"
    )
    aliases: list[str] = Field(
        default_factory=list, max_length=20, description="对象别名列表。"
    )


class MetricOutput(ContractModel):
    """结构化指标响应。"""

    metrics: list[MetricMetadata] = Field(max_length=100, description="指标列表。")
    missing_business_meaning: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="未补充的业务含义问题。",
    )


class ValidationIssue(ContractModel):
    """确定性校验问题。"""

    code: str = Field(description="代码或错误代码。")
    path: str = Field(description="字段路径。")
    message: str = Field(description="消息文本。")
    repairable: bool = Field(default=True, description="是否可自动修复。")
