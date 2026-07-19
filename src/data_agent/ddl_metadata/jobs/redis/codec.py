"""DDL 任务 Hash 投影与规范载荷编解码。"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime

from data_agent.ddl_metadata.jobs.identifiers import question_set_id
from data_agent.ddl_metadata.models.jobs import (
    JobError,
    JobRecord,
    JobResult,
    JobStatus,
)
from data_agent.ddl_metadata.models.semantic import (
    MetricAnswer,
    MetricQuestion,
)


class JobCodec:
    """集中处理任务公开投影和问题回答规范载荷。"""

    @staticmethod
    def project(values: Mapping[str, str]) -> JobRecord:
        """从内部 Hash 字段构造公开契约。

        Args:
            values: 解码后的 Redis Hash 字段。

        Returns:
            不包含 DDL 和内部回答的公开任务投影。
        """
        return JobRecord(
            job_id=values["job_id"],
            source=values["source"],
            status=JobStatus(values["status"]),
            revision=int(values.get("revision", "0")),
            attempt=int(values.get("attempt", "0")),
            question_round=int(values.get("question_round", "0")),
            question_set_id=values.get("question_set_id") or None,
            questions=(
                [
                    MetricQuestion.model_validate(question)
                    for question in json.loads(values["questions_json"])
                ]
                if values.get("questions_json")
                else None
            ),
            result=(
                JobResult.model_validate_json(values["result_json"])
                if values.get("result_json")
                else None
            ),
            error=(
                JobError.model_validate_json(values["error_json"])
                if values.get("error_json")
                else None
            ),
            created_at=datetime.fromisoformat(values["created_at"]),
            updated_at=datetime.fromisoformat(values["updated_at"]),
            expires_at=(
                datetime.fromisoformat(values["expires_at"])
                if values.get("expires_at")
                else None
            ),
            graph_version=values["graph_version"],
        )

    @staticmethod
    def questions_json(questions: Sequence[MetricQuestion]) -> str:
        """序列化供任务 Hash 保存的问题列表。"""
        return json.dumps(
            [question.model_dump(mode="json") for question in questions],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def question_set_id(questions: Sequence[MetricQuestion]) -> str:
        """生成问题集合的规范 SHA-256 标识。"""
        return question_set_id(questions)

    @staticmethod
    def answers_payload(answers: Sequence[MetricAnswer]) -> tuple[str, str]:
        """生成回答规范 JSON 与 SHA-256 摘要。"""
        answer_json = json.dumps(
            [answer.model_dump(mode="json") for answer in answers],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return answer_json, hashlib.sha256(answer_json.encode()).hexdigest()
