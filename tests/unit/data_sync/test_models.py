"""数据同步值编码和主键身份的确定性检查。"""

import json
from datetime import datetime, timedelta
from decimal import Decimal

from tests.helpers.checks import check_equal

from data_agent.data_sync.models import (
    DesiredColumn,
    DesiredSyncTable,
    canonical_primary_key,
    decode_row_value,
    encode_row_value,
)


def test_row_value_codec_and_primary_key_are_stable() -> None:
    """特殊 MySQL 值可逆编码，复合主键文档不依赖字典插入顺序。"""
    values = [
        Decimal("12.30"),
        datetime(2026, 7, 27, 12, 34, 56, 123456),
        b"\x00\xff",
        timedelta(days=-2, seconds=3, microseconds=456),
        timedelta(hours=49, microseconds=7),
        None,
        "文本",
    ]
    check_equal(
        "MySQL 特殊值编码后可逆",
        [decode_row_value(encode_row_value(value)) for value in values],
        values,
    )
    first = canonical_primary_key(
        ["tenant_id", "order_id"],
        {"order_id": 7, "tenant_id": "a"},
    )
    second = canonical_primary_key(
        ["tenant_id", "order_id"],
        {"tenant_id": "a", "order_id": 7},
    )
    check_equal("复合主键身份稳定", first, second)


def test_json_row_value_codec_returns_bindable_canonical_text() -> None:
    """嵌套 MySQL JSON 值编码后可恢复为驱动可绑定的规范文本。"""
    value = {"nested": [1, None, {"enabled": True}], "name": "示例"}
    encoded = encode_row_value(value)
    decoded = decode_row_value(encoded)
    check_equal("JSON 解码结果可由 MySQL JSON 列接收", isinstance(decoded, str), True)
    if not isinstance(decoded, str):
        raise AssertionError("JSON codec 必须返回可绑定文本")
    check_equal("JSON 嵌套值语义可逆", json.loads(decoded), value)


def test_decimal_primary_key_uses_mysql_numeric_equivalence() -> None:
    """不同 scale 的数值相等 DECIMAL 生成相同 ownership 身份。"""
    first = canonical_primary_key(["id"], {"id": encode_row_value(Decimal("1.0"))})
    second = canonical_primary_key(["id"], {"id": encode_row_value(Decimal("1.00"))})
    check_equal("DECIMAL 主键忽略无意义 scale", first, second)


def test_decimal_codec_preserves_mysql_maximum_precision() -> None:
    """移除尾零时不受进程 Decimal context 精度影响。"""
    first = Decimal("123456789012345678901234567890.00")
    second = Decimal("123456789012345678901234567891.00")
    check_equal(
        "高精度 DECIMAL 无损往返",
        [decode_row_value(encode_row_value(value)) for value in (first, second)],
        [first, second],
    )
    check_equal(
        "不同高精度 DECIMAL 保持不同身份",
        encode_row_value(first) != encode_row_value(second),
        True,
    )


def test_json_scalar_codec_preserves_json_identity() -> None:
    """JSON 标量与普通 SQL 标量使用不同的可逆编码。"""
    for value in ("paid", 3, True, None):
        encoded = encode_row_value(value, json_value=True)
        decoded = decode_row_value(encoded)
        if not isinstance(decoded, str):
            raise AssertionError("JSON 标量必须恢复为可绑定 JSON 文本")
        check_equal("JSON 标量语义可逆", json.loads(decoded), value)


def test_set_row_value_codec_is_stable_and_bindable() -> None:
    """MySQL SET 值按稳定顺序编码并恢复为驱动可绑定文本。"""
    check_equal(
        "多成员 SET 稳定排序",
        decode_row_value(encode_row_value({"beta", "alpha"})),
        "alpha,beta",
    )
    check_equal("空 SET 可逆", decode_row_value(encode_row_value(set())), "")


def test_desired_hash_is_scoped_to_one_table_contract() -> None:
    """全局 schema 指纹变化不重建结构未变化的单表任务。"""
    column = DesiredColumn(id="id", name="id", data_type="BIGINT", nullable=False)
    first = DesiredSyncTable(
        source="local",
        source_schema="business",
        source_table="orders",
        target_table="orders",
        columns=[column],
        primary_key=["id"],
        schema_fingerprint="a" * 64,
    )
    second = first.model_copy(update={"schema_fingerprint": "b" * 64})
    check_equal(
        "无关全局指纹不改变 generation", first.desired_hash(), second.desired_hash()
    )
    third = first.model_copy(update={"metric_dependency_column_ids": ["id"]})
    check_equal(
        "指标依赖不改变数据同步 generation", first.desired_hash(), third.desired_hash()
    )
