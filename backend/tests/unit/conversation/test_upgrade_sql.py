"""锁定保留数据卷的 Conversation 兼容升级契约。"""

from pathlib import Path


def test_conversation_upgrade_adds_runtime_columns_before_precision_change() -> None:
    """父版本表必须先补齐新版必读列，再修改租约时间精度。"""
    migration = (
        Path(__file__).parents[4]
        / "docs"
        / "docker"
        / "mysql"
        / "migrations"
        / "20260808_conversation_lease_microseconds.sql"
    ).read_text(encoding="utf-8")

    claim_column = "ADD COLUMN active_turn_claim_token CHAR(32) NULL"
    abandoned_column = "ADD COLUMN turn_abandoned_at DATETIME(6) NULL"
    fingerprint_column = "ADD COLUMN semantic_fingerprint CHAR(64) NULL"
    precision_change = "MODIFY updated_at DATETIME(6) NOT NULL"

    assert claim_column in migration
    assert abandoned_column in migration
    assert fingerprint_column in migration
    assert migration.index(claim_column) < migration.index(precision_change)
    assert migration.index(abandoned_column) < migration.index(precision_change)
