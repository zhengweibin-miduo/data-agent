"""数据同步值编码和主键身份的确定性检查。"""

import json
from datetime import datetime, timedelta
from decimal import Decimal

from tests.helpers.checks import check_equal

from data_agent.data_sync.models import (
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
