"""Live integration resources shared by MySQL-backed test modules."""

from collections.abc import AsyncIterator

import pytest

from infrastructure.generation_locks import GenerationLockManager
from settings import app_config


@pytest.fixture
async def generation_lock_manager() -> AsyncIterator[GenerationLockManager]:
    """Provide one probed dedicated generation owner pool per integration test."""
    manager = GenerationLockManager(
        app_config.mysql.url,
        pool_size=app_config.mysql.generation_lock_pool_size,
        pool_timeout_seconds=app_config.mysql.generation_lock_pool_timeout_seconds,
    )
    await manager.initialize()
    try:
        await manager.check_capability()
        yield manager
    finally:
        await manager.close()
