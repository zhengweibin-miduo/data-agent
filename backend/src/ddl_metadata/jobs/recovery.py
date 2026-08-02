"""停滞 DDL 任务的纯裁决规则。"""

from enum import StrEnum

from models.jobs import JobStatus


class StallAction(StrEnum):
    """对一个停滞候选任务应执行的恢复动作。"""

    DROP = "drop"
    SKIP = "skip"
    REACTIVATE = "reactivate"
    RESET = "reset"
    FAIL = "fail"


def stall_action(
    status: JobStatus,
    attempt: int,
    max_attempts: int,
) -> StallAction:
    """裁决停滞候选任务的恢复动作。

    该判定只依赖权威记录中的状态与尝试次数，不触碰任何 IO，因此恢复策略可以
    脱离 Redis 单独验证。

    Args:
        status: 权威记录中的当前状态。
        attempt: 权威记录中已累计的激活次数。
        max_attempts: 允许的累计激活次数上限。

    Returns:
        调用方应执行的恢复动作。
    """
    # 步骤一：终态任务不需要恢复，只需把它们从活动索引中摘除。
    if status in _TERMINAL:
        return StallAction.DROP
    # 步骤二：等待人工回答由截止时间扫描负责，停滞巡检不介入其生命周期。
    if status == JobStatus.WAITING_INPUT:
        return StallAction.SKIP
    # 步骤三：已受理但未被执行的任务只需重新投递激活请求。
    if status == JobStatus.PENDING:
        return StallAction.REACTIVATE
    # 步骤四：执行中任务超过累计激活上限时进入终态，避免无限重新激活。
    if attempt >= max_attempts:
        return StallAction.FAIL
    # 步骤五：其余执行中任务先回退为待执行，再由调用方重新投递。
    return StallAction.RESET


_TERMINAL = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.REJECTED,
        JobStatus.FAILED,
    }
)
