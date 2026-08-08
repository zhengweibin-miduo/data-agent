"""专用 generation owner 在真实 MySQL Locking Service 上的并发契约。"""

from uuid import uuid4

import pytest

from infrastructure.generation_locks import GenerationLockManager
from infrastructure.mysql import AdvisoryLockUnavailableError
from settings import app_config

pytestmark = pytest.mark.integration


async def test_live_read_sharing_and_write_exclusion() -> None:
    """同 target READ 可共享，WRITE 在 READ 存续时失败且释放后成功。"""
    manager = GenerationLockManager(
        app_config.mysql.url,
        pool_size=3,
        pool_timeout_seconds=1,
    )
    await manager.initialize()
    await manager.check_capability()
    name = f"integration:{uuid4().hex}"
    try:
        async with manager.read([name], 0):
            async with manager.read([name], 0):
                with pytest.raises(AdvisoryLockUnavailableError):
                    async with manager.write([name], 0):
                        pytest.fail("共享 READ 存续时 WRITE 不得进入")
        async with manager.write([name], 0):
            pass
    finally:
        await manager.close()


async def test_live_dedicated_pool_checkout_is_bounded() -> None:
    """长 owner 占满专用池时，下一 owner 在一秒 checkout 预算内稳定失败。"""
    manager = GenerationLockManager(
        app_config.mysql.url,
        pool_size=1,
        pool_timeout_seconds=1,
    )
    await manager.initialize()
    await manager.check_capability()
    name = f"integration:{uuid4().hex}"
    try:
        async with manager.read([name], 0):
            with pytest.raises(AdvisoryLockUnavailableError, match="owner 池已满"):
                async with manager.read([name], 0):
                    pytest.fail("专用池无空闲连接时不得进入")
    finally:
        await manager.close()
