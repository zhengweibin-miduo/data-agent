"""语义分类、指标与校验契约。"""

from enum import StrEnum

from pydantic import Field

from data_agent.ddl_metadata.models.base import ContractModel


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

    table_id: str
    role: TableRole
    description: str = Field(min_length=1, max_length=4000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=20)


class SemanticColumn(ContractModel):
    """模型返回并待确定性校验的列语义。"""

    column_id: str
    role: ColumnRole
    description: str = Field(min_length=1, max_length=4000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=20)


class SemanticMetadata(ContractModel):
    """结构化表列语义响应。"""

    tables: list[SemanticTable]
    columns: list[SemanticColumn]


class MetricQuestion(ContractModel):
    """一次指标澄清问题。"""

    question_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=2000)
    fact_table_id: str
    column_ids: list[str] = Field(default_factory=list, max_length=50)
    required: bool = True


class MetricQuestionSet(ContractModel):
    """模型生成的一轮指标问题。"""

    questions: list[MetricQuestion] = Field(max_length=100)


class MetricAnswer(ContractModel):
    """用户对单个指标问题的回答。"""

    question_id: str
    answer: str = Field(min_length=1, max_length=8000)


class MetricMetadata(ContractModel):
    """最终校验通过的指标元数据。"""

    id: str = ""
    name: str = Field(min_length=1, max_length=128)
    fact_table_id: str
    definition: str = Field(min_length=1, max_length=8000)
    relevant_column_ids: list[str] = Field(min_length=1, max_length=100)
    answer_question_ids: list[str] = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class MetricOutput(ContractModel):
    """结构化指标响应。"""

    metrics: list[MetricMetadata] = Field(max_length=100)
    missing_business_meaning: list[str] = Field(default_factory=list, max_length=50)


class ValidationIssue(ContractModel):
    """确定性校验问题。"""

    code: str
    path: str
    message: str
    repairable: bool = True
