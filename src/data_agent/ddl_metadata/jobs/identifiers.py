"""DDL 任务载荷标识。"""

import hashlib
import json
from collections.abc import Sequence

from data_agent.ddl_metadata.models.semantic import MetricQuestion


def question_set_id(questions: Sequence[MetricQuestion]) -> str:
    """生成问题集合的规范 SHA-256 标识。

    Args:
        questions: 当前等待轮次的问题列表。

    Returns:
        与既有协议兼容的 ``sha256:`` 标识。
    """
    value = json.dumps(
        [question.model_dump(mode="json") for question in questions],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"
