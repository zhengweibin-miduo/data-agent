"""DDL 元数据流程的稳定业务错误。"""


class DdlMetadataError(Exception):
    """跨 worker/API 边界传播的安全错误。"""

    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        retryable: bool = False,
        http_status: int = 422,
        details: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.retryable = retryable
        self.http_status = http_status
        self.details = details or {"message": message}
