"""DDL 元数据工作流节点。"""

from __future__ import annotations

from langgraph.types import interrupt
from loguru import logger

from data_agent.ddl_metadata.application.accepted_snapshot import AcceptedSnapshot
from data_agent.ddl_metadata.jobs.identifiers import question_set_id
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.ddl_metadata.validation import (
    finalize_and_validate_metrics,
    validate_metadata,
    validate_metric_questions,
)
from data_agent.ddl_metadata.workflow.contracts import DDLGraphDependencies
from data_agent.ddl_metadata.workflow.state import DDLGraphState
from data_agent.errors import DataAgentError
from data_agent.memory.domain.candidates import MemoryVersions, build_accepted_memories
from data_agent.models.jobs import (
    JobError,
    JobResult,
    JobStatus,
)
from data_agent.models.memory import (
    MEMORY_CONTENT_ADAPTER,
    MemoryCandidate,
)
from data_agent.models.physical import PhysicalSchema
from data_agent.models.semantic import (
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticMetadata,
    TableRole,
    ValidationIssue,
)
from data_agent.settings import app_config


def _state_string(state: DDLGraphState, key: str) -> str:
    """读取图状态中的必需字符串。"""
    value = state.get(key)
    if not isinstance(value, str):
        raise ValueError(f"图状态缺少字符串字段 {key}")
    return value


def _error_update(error: DataAgentError) -> DDLGraphState:
    """把业务拒绝转为安全终态。"""
    return {
        "status": JobStatus.REJECTED.value,
        "route": "rejected",
        "error": JobError(
            code=error.code,
            stage=error.stage,
            retryable=error.retryable,
            details=error.details,
        ).model_dump(mode="json"),
    }


class DDLWorkflowNodes:
    """绑定工作流依赖的 DDL 元数据节点集合。"""

    def __init__(self, dependencies: DDLGraphDependencies) -> None:
        """绑定不进入检查点的进程内依赖。"""
        self._dependencies = dependencies

    async def parse_node(self, state: DDLGraphState) -> DDLGraphState:
        """解析 DDL 并初始化工作流状态。"""
        # 步骤一：记录稳定公开阶段，再调用线程外解析边界生成确定性物理 Schema。
        logger.info("DDL 解析已开始，完成后将加载并校验可复用记忆")
        try:
            schema = await parse_ddl(
                _state_string(state, "source"),
                _state_string(state, "ddl"),
            )
        except DataAgentError as error:
            return _error_update(error)
        # 步骤二：解析成功后初始化可序列化的修复预算、问答证据和路由状态。
        return {
            "physical_schema": schema.model_dump(mode="json"),
            "semantic_attempts": 0,
            "metric_attempts": 0,
            "question_round": 0,
            "metric_questions": [],
            "metric_answers": [],
            "validation_errors": [],
            "status": JobStatus.RUNNING.value,
            "route": "continue",
        }

    async def load_memory_node(self, state: DDLGraphState) -> DDLGraphState:
        """加载并重校验兼容的长期记忆。"""
        # 步骤一：恢复物理 Schema，并让记忆加载器按当前 AST 重新裁决兼容内容。
        logger.info("记忆加载与兼容性校验已开始，完成后将分类元数据")
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        try:
            context = await self._dependencies.memory_context.load(schema)
        except DataAgentError as error:
            return _error_update(error)
        # 步骤二：把安全复用内容写回 checkpoint；完整语义走缓存校验，否则继续模型分类。
        update: DDLGraphState = {
            "memory_capsule": [
                content.model_dump(mode="json") for content in context.semantic_capsule
            ],
            "reused_memory": [
                content.model_dump(mode="json") for content in context.reused_memory
            ],
            "metric_questions": [
                question.model_dump(mode="json") for question in context.questions
            ],
            "metric_answers": [
                answer.model_dump(mode="json") for answer in context.answers
            ],
            "metrics": [metric.model_dump(mode="json") for metric in context.metrics],
            "reused_metrics": bool(context.metrics),
            "route": "continue",
        }
        if context.complete_semantic is not None:
            update["semantic_metadata"] = context.complete_semantic.model_dump(
                mode="json"
            )
            update["route"] = "validate_cached"
        return update

    async def classify_node(self, state: DDLGraphState) -> DDLGraphState:
        """生成或修复表列语义分类。"""
        # 步骤一：恢复确定性物理结构、上轮校验问题和兼容记忆，再把三类有界输入
        # 交给模型；模型只能补充语义，不能改写解析器已经确定的表列身份。
        logger.info("元数据分类已开始，完成后将校验分类结果")
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        issues = [
            ValidationIssue.model_validate(issue)
            for issue in state.get("validation_errors", [])
        ]
        capsule = [
            MEMORY_CONTENT_ADAPTER.validate_python(value)
            for value in state.get("memory_capsule", [])
        ]
        try:
            metadata = await self._dependencies.model.classify(
                schema,
                issues,
                capsule,
            )
        except (TypeError, ValueError):
            # 步骤二：semantic_attempts 存在 checkpoint 中，并由结构化解析与确定性校验
            # 共享一次修复预算；再次失败必须转业务拒绝，不能形成无限模型回环。
            attempts = state.get("semantic_attempts", 0)
            if attempts < 1:
                issue = ValidationIssue(
                    code="invalid_structured_output",
                    path="semantic_metadata",
                    message="模型语义响应未通过结构化解析",
                )
                return {
                    "validation_errors": [issue.model_dump(mode="json")],
                    "semantic_attempts": attempts + 1,
                    "route": "repair",
                }
            return _error_update(
                DataAgentError(
                    "invalid_semantic_output",
                    "classify_metadata",
                    "模型语义响应在修复后仍无法解析",
                )
            )
        return {
            "semantic_metadata": metadata.model_dump(mode="json"),
            "validation_errors": [],
            "route": "validate",
        }

    async def validate_metadata_node(self, state: DDLGraphState) -> DDLGraphState:
        """确定性校验语义元数据。"""
        # 步骤一：模型结果必须重新对照物理 Schema 校验，模型输出本身不是可信事实。
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        issues = validate_metadata(schema, metadata)
        if not issues:
            return {"validation_errors": [], "route": "valid"}
        # 步骤二：只有全部问题可修复时才共享一次 checkpoint 预算，其余情况
        # 收敛为业务拒绝。
        attempts = state.get("semantic_attempts", 0)
        if attempts < 1 and all(issue.repairable for issue in issues):
            return {
                "validation_errors": [
                    issue.model_dump(mode="json") for issue in issues
                ],
                "semantic_attempts": attempts + 1,
                "route": "repair",
            }
        error = DataAgentError(
            "invalid_semantic_metadata",
            "validate_metadata",
            "语义元数据未通过确定性校验",
            details={"codes": ",".join(sorted({issue.code for issue in issues}))},
        )
        return _error_update(error)

    async def plan_questions_node(self, state: DDLGraphState) -> DDLGraphState:
        """规划当前轮次的指标澄清问题。"""
        # 步骤一：指标只服务于事实表；没有事实表时直接跳过问答与指标生成，避免模型
        # 为不适用的表结构臆造业务指标。
        logger.info("指标问题规划已开始，完成后将等待回答或继续生成指标")
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        fact_ids = {
            table.table_id for table in metadata.tables if table.role == TableRole.FACT
        }
        if not fact_ids:
            return {
                "metrics": [],
                "current_questions": [],
                "route": "no_metrics",
            }
        # 步骤二：后续轮次携带历史回答，让模型只补齐仍缺失的业务含义。
        round_number = state.get("question_round", 0) + 1
        answers = [
            MetricAnswer.model_validate(answer)
            for answer in state.get("metric_answers", [])
        ]
        try:
            output = await self._dependencies.model.plan_questions(
                schema,
                metadata,
                answers,
                round_number,
            )
        except (TypeError, ValueError):
            return _error_update(
                DataAgentError(
                    "invalid_metric_questions",
                    "plan_metric_questions",
                    "模型指标问题未通过结构化解析",
                )
            )
        # 步骤三：对模型问题做确定性引用校验，并要求事实表场景至少产生一个有效问题；
        # 无效问题不能进入 interrupt，更不能形成可提交的外部回答契约。
        issues = validate_metric_questions(
            schema,
            metadata,
            output.questions,
        )
        if issues or not output.questions:
            error = DataAgentError(
                "invalid_metric_questions",
                "plan_metric_questions",
                "事实表指标问题无效或为空",
                details={"codes": ",".join(sorted({issue.code for issue in issues}))},
            )
            return _error_update(error)
        # 步骤四：保留历史问题作为指标证据，仅按 question_id 覆盖本轮同一问题。
        history = [
            MetricQuestion.model_validate(question)
            for question in state.get("metric_questions", [])
        ]
        merged = {question.question_id: question for question in history}
        merged.update({question.question_id: question for question in output.questions})
        return {
            "metric_questions": [
                question.model_dump(mode="json") for question in merged.values()
            ],
            "current_questions": [
                question.model_dump(mode="json") for question in output.questions
            ],
            "question_round": round_number,
            "route": "await",
        }

    async def await_answers_node(self, state: DDLGraphState) -> DDLGraphState:
        """中断图并接收当前问题回答。"""
        # 步骤一：恢复当前问题并以 question_set_id 锚定 interrupt 的外部回答契约。
        current = [
            MetricQuestion.model_validate(question)
            for question in state.get("current_questions", [])
        ]
        resumed = interrupt(
            {
                "questions": [question.model_dump(mode="json") for question in current],
                "question_set_id": question_set_id(current),
                "question_round": state.get("question_round", 0),
            }
        )
        # 步骤二：恢复值仍需通过类型契约；同一 question_id 的新回答覆盖历史值，其余
        # 已确认回答继续随 checkpoint 进入指标生成阶段。
        new_answers = [MetricAnswer.model_validate(answer) for answer in resumed]
        existing = {
            answer.question_id: answer
            for answer in (
                MetricAnswer.model_validate(answer)
                for answer in state.get("metric_answers", [])
            )
        }
        existing.update({answer.question_id: answer for answer in new_answers})
        return {
            "metric_answers": [
                answer.model_dump(mode="json") for answer in existing.values()
            ],
            "route": "generate",
        }

    async def generate_metrics_node(self, state: DDLGraphState) -> DDLGraphState:
        """根据语义与回答生成指标。"""
        # 步骤一：从 checkpoint 恢复完整语义、跨轮问题/回答及上轮校验问题，使修复调用
        # 只针对已知缺陷，不丢失用户已经提供的业务证据。
        logger.info("指标生成已开始，完成后将校验指标定义")
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        questions = [
            MetricQuestion.model_validate(question)
            for question in state.get("metric_questions", [])
        ]
        answers = [
            MetricAnswer.model_validate(answer)
            for answer in state.get("metric_answers", [])
        ]
        issues = [
            ValidationIssue.model_validate(issue)
            for issue in state.get("validation_errors", [])
        ]
        try:
            output = await self._dependencies.model.generate_metrics(
                schema,
                metadata,
                questions,
                answers,
                issues,
            )
        except (TypeError, ValueError):
            # 步骤二：metric_attempts 同样随 checkpoint 恢复且只允许一次可修复回环；
            # 持续无效的模型输出必须收敛为结构化拒绝，而不是重复消耗模型调用。
            attempts = state.get("metric_attempts", 0)
            if attempts < 1:
                issue = ValidationIssue(
                    code="invalid_structured_output",
                    path="metrics",
                    message="模型指标响应未通过结构化解析",
                )
                return {
                    "validation_errors": [issue.model_dump(mode="json")],
                    "metric_attempts": attempts + 1,
                    "route": "repair",
                }
            return _error_update(
                DataAgentError(
                    "invalid_metric_output",
                    "generate_metrics",
                    "模型指标响应在修复后仍无法解析",
                )
            )
        # 步骤三：模型明确报告业务含义不足时，首轮回到问题规划；第二轮仍不完整则拒绝，
        # 不能用猜测填补定义后继续持久化。
        if output.missing_business_meaning:
            if state.get("question_round", 0) < 2:
                return {
                    "validation_errors": [
                        ValidationIssue(
                            code="missing_business_meaning",
                            path="metrics",
                            message=value,
                            repairable=False,
                        ).model_dump(mode="json")
                        for value in output.missing_business_meaning
                    ],
                    "route": "followup",
                }
            return _error_update(
                DataAgentError(
                    "metric_ambiguity",
                    "generate_metrics",
                    "第二轮回答后指标定义仍不完整",
                )
            )
        return {
            "metrics": [metric.model_dump(mode="json") for metric in output.metrics],
            "validation_errors": [],
            "route": "validate",
        }

    async def validate_metrics_node(self, state: DDLGraphState) -> DDLGraphState:
        """确定性校验并完成指标。"""
        # 步骤一：先把模型指标与物理结构、语义分类及问答证据共同 finalize，再执行引用、
        # 表达式和业务契约校验；模型输出本身不是可持久化事实。
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        questions = [
            MetricQuestion.model_validate(question)
            for question in state.get("metric_questions", [])
        ]
        answers = [
            MetricAnswer.model_validate(answer)
            for answer in state.get("metric_answers", [])
        ]
        metrics = [
            MetricMetadata.model_validate(metric) for metric in state.get("metrics", [])
        ]
        finalized, issues = finalize_and_validate_metrics(
            _state_string(state, "source"),
            schema,
            metadata,
            questions,
            answers,
            metrics,
        )
        if not issues:
            return {
                "metrics": [metric.model_dump(mode="json") for metric in finalized],
                "validation_errors": [],
                "route": "valid",
            }
        # 步骤二：仅当所有问题均可修复且预算尚存时回到模型，否则终止在安全业务拒绝。
        attempts = state.get("metric_attempts", 0)
        if attempts < 1 and all(issue.repairable for issue in issues):
            return {
                "validation_errors": [
                    issue.model_dump(mode="json") for issue in issues
                ],
                "metric_attempts": attempts + 1,
                "route": "repair",
            }
        return _error_update(
            DataAgentError(
                "invalid_metrics",
                "validate_metrics",
                "指标未通过确定性校验",
                details={"codes": ",".join(sorted({issue.code for issue in issues}))},
            )
        )

    async def build_memories_node(self, state: DDLGraphState) -> DDLGraphState:
        """从已接受结果构建记忆候选。"""
        # 步骤一：恢复已通过两类确定性校验的 Schema、语义、问答和指标。
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        questions = [
            MetricQuestion.model_validate(question)
            for question in state.get("metric_questions", [])
        ]
        answers = [
            MetricAnswer.model_validate(answer)
            for answer in state.get("metric_answers", [])
        ]
        metrics = [
            MetricMetadata.model_validate(metric) for metric in state.get("metrics", [])
        ]
        # 步骤二：连同兼容记忆统一构建权威候选，此节点不执行任何数据库写入。
        candidates = build_accepted_memories(
            schema,
            metadata,
            questions,
            answers,
            metrics,
            [
                MEMORY_CONTENT_ADAPTER.validate_python(content)
                for content in state.get("reused_memory", [])
            ],
            versions=MemoryVersions(
                content=app_config.memory.ddl_semantic_content_version,
                projection=app_config.memory.projection_version,
            ),
            job_id=_state_string(state, "job_id"),
        )
        return {
            "memory_candidates": [
                candidate.model_dump(mode="json") for candidate in candidates
            ],
            "route": "persist",
        }

    async def persist_node(self, state: DDLGraphState) -> DDLGraphState:
        """原子持久化最终快照。"""
        # 步骤一：持久化入口再次恢复所有强类型产物，确保 checkpoint 中的 JSON 在进入
        # 唯一写出口前仍满足当前契约。
        logger.info("元数据快照持久化已开始，完成后将发布任务终态")
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        questions = [
            MetricQuestion.model_validate(question)
            for question in state.get("metric_questions", [])
        ]
        answers = [
            MetricAnswer.model_validate(answer)
            for answer in state.get("metric_answers", [])
        ]
        metrics = [
            MetricMetadata.model_validate(metric) for metric in state.get("metrics", [])
        ]
        candidates = [
            MemoryCandidate.model_validate(candidate)
            for candidate in state.get("memory_candidates", [])
        ]
        # 步骤二：记忆候选只来自已 finalized 的语义和指标；snapshot 事务成功后才返回
        # SUCCEEDED，异常则交由 runner 统一分类，避免出现成功投影但快照未提交。
        await self._dependencies.snapshot_publisher.publish(
            AcceptedSnapshot(
                schema=schema,
                metadata=metadata,
                questions=tuple(questions),
                answers=tuple(answers),
                metrics=tuple(metrics),
                candidates=tuple(candidates),
            )
        )
        # 步骤三：事务提交完成后才构造安全结果并把图状态标记为成功。
        result = JobResult(
            ddl_hash=schema.ddl_hash,
            table_count=len(schema.tables),
            column_count=sum(len(table.columns) for table in schema.tables),
            metric_count=len(metrics),
        )
        return {
            "status": JobStatus.SUCCEEDED.value,
            "result": result.model_dump(mode="json"),
            "route": "succeeded",
        }
