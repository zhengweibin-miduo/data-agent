"""DDL 元数据严格契约的共享基类。"""

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    """严格且可安全序列化的业务契约基类。"""

    model_config = ConfigDict(extra="forbid")
