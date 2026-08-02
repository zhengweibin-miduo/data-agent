"""Accepted Snapshot 应用接口测试。"""

from ddl_metadata.application.accepted_snapshot import AcceptedSnapshot
from models.physical import PhysicalSchema
from models.semantic import SemanticMetadata


def test_accepted_snapshot_owns_immutable_publication_input() -> None:
    """发布命令把一次 accepted snapshot 固化为不可变输入。"""
    snapshot = AcceptedSnapshot(
        schema=PhysicalSchema(
            source="local",
            canonical_ddl="",
            ddl_hash="d" * 64,
            tables=[],
            schema_fingerprint="f" * 64,
        ),
        metadata=SemanticMetadata(tables=[], columns=[]),
        questions=(),
        answers=(),
        metrics=(),
        candidates=(),
    )

    assert snapshot.questions == ()
    assert snapshot.answers == ()
    assert snapshot.metrics == ()
    assert snapshot.candidates == ()
