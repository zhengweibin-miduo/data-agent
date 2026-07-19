"""DDL 任务 Redis 键空间与成员标识。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JobKeysStore:
    """不可变地生成任务、租约和 outbox 键。"""

    prefix: str

    def job(self, job_id: str) -> str:
        """返回任务 Hash 键。"""
        return f"{self.prefix}:job:{job_id}"

    def source(self, source: str) -> str:
        """返回逻辑数据源租约键。"""
        return f"{self.prefix}:source:{source}"

    @property
    def dispatch(self) -> str:
        """返回 dispatch outbox 键。"""
        return f"{self.prefix}:dispatch"

    @property
    def waiting(self) -> str:
        """返回等待截止时间有序集合键。"""
        return f"{self.prefix}:waiting"

    @property
    def checkpoint_cleanup(self) -> str:
        """返回终态检查点清理 outbox 键。"""
        return f"{self.prefix}:checkpoint_cleanup"

    @staticmethod
    def activation_member(job_id: str, revision: int | str) -> str:
        """返回修订感知的 outbox 成员。"""
        return f"{job_id}:{revision}"

    def arq_job_id(self, job_id: str, revision: int | str) -> str:
        """返回确定性的 arq 任务 ID。"""
        return f"{self.prefix}:{job_id}:{revision}"
