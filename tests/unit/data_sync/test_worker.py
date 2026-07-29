"""数据同步 worker 启动门禁检查。"""

from unittest.mock import AsyncMock, Mock

import pytest

from data_agent.data_sync.worker import _check_dw_table_name_case_sensitivity


@pytest.mark.parametrize("mode", [1, 2])
async def test_worker_rejects_case_insensitive_dw_table_names(mode: int) -> None:
    """目标表名大小写不敏感时拒绝启动，避免物理表身份别名碰撞。"""
    result = Mock()
    result.scalar_one.return_value = mode
    session = AsyncMock()
    session.execute.return_value = result

    with pytest.raises(RuntimeError, match="lower_case_table_names=0"):
        await _check_dw_table_name_case_sensitivity(session)


async def test_worker_accepts_case_sensitive_dw_table_names() -> None:
    """目标表名大小写敏感时允许启动。"""
    result = Mock()
    result.scalar_one.return_value = 0
    session = AsyncMock()
    session.execute.return_value = result

    await _check_dw_table_name_case_sensitivity(session)
