"""跨业务功能共享的稳定应用错误。"""


class DataAgentError(Exception):
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
        """构造可安全投影的业务错误。

        Args:
            code: 稳定错误码。
            stage: 失败的工作流阶段。
            message: 仅供内部使用的安全错误消息。
            retryable: 是否允许基础设施层重试。
            http_status: API 映射的 HTTP 状态码。
            details: 可跨进程边界传播的有界详情。
        """
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.retryable = retryable
        self.http_status = http_status
        # details 会经 API 响应与公开事件流投影给调用方，因此不回填内部 message；
        # 未显式声明可公开详情的错误只暴露稳定的 code 与 stage。
        self.details = dict(details) if details else {}
