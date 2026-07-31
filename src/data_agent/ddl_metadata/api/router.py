"""DDL 元数据聚合路由。"""

from fastapi import APIRouter

from data_agent.ddl_metadata.api.jobs import router as jobs_router
from data_agent.ddl_metadata.api.memories import router as memories_router
from data_agent.ddl_metadata.api.preview import router as preview_router

router = APIRouter()
router.include_router(jobs_router)
router.include_router(memories_router)
router.include_router(preview_router)
