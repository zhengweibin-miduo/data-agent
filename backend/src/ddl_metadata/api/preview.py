"""只读 DDL 结构预览路由。"""

from fastapi import APIRouter

from ddl_metadata.parsing import parse_ddl_preview
from models.jobs import DDLJobRequest
from models.physical import DDLPreview

router = APIRouter(prefix="/api/v1/metadata", tags=["ddl-metadata"])


@router.post("/ddl-preview", response_model=DDLPreview)
async def preview_ddl(body: DDLJobRequest) -> DDLPreview:
    """返回确定性表、字段和外键投影，不创建任务或写入数据。"""
    return await parse_ddl_preview(body.source, body.ddl)
