"""Accepted Snapshot 发布命令与端口。"""

from dataclasses import dataclass
from typing import Protocol

from models.memory import MemoryCandidate
from models.physical import PhysicalSchema
from models.semantic import (
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticMetadata,
)


@dataclass(frozen=True)
class AcceptedSnapshot:
    """一次通过验证、可原子发布的 Meta Snapshot。"""

    schema: PhysicalSchema
    metadata: SemanticMetadata
    questions: tuple[MetricQuestion, ...]
    answers: tuple[MetricAnswer, ...]
    metrics: tuple[MetricMetadata, ...]
    candidates: tuple[MemoryCandidate, ...]


class AcceptedSnapshotPublisher(Protocol):
    """工作流发布 accepted snapshot 的最小接口。"""

    async def publish(self, snapshot: AcceptedSnapshot) -> None:
        """原子发布一个通过验证的 snapshot。"""
        ...
