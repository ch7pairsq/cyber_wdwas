"""HarnessAdapter 抽象与实现选择（§12、附录 B.1）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from .schema import ChallengeInput, HarnessContext, RunSummary, Submission, SubmitReceipt


class HarnessUnavailable(Exception):
    pass


class HarnessAdapter(ABC):
    @abstractmethod
    def startup(self) -> HarnessContext: ...

    @abstractmethod
    def read_tasks(self) -> List[ChallengeInput]: ...

    def read_attachment(self, ref: str, *, max_bytes: int):
        raise HarnessUnavailable("attachments not supported by this adapter")

    def emit_progress(self, event: dict) -> None:
        pass

    def ensure_environment(self, challenge_id: str):
        """按需确保题目环境可用；返回 None 或环境信息 dict（含 container_addr）。

        不支持的平台 no-op；编排层在每个意图执行前调用，失败可抛异常
        （由编排层记事件，不阻塞调度）。
        """
        return None

    def task_description(self, challenge_id: str) -> Optional[str]:
        """返回该题目的最新描述文本（含运行环境信息）；不支持时返回 None。"""
        return None

    @abstractmethod
    def submit_answer(self, submission: Submission) -> SubmitReceipt: ...

    def get_receipt(self, idempotency_key: str) -> Optional[SubmitReceipt]:
        """UNKNOWN 时只查询回执，不重发（附录 E）。"""
        return None

    @abstractmethod
    def finalize(self, summary: RunSummary) -> dict: ...

    def shutdown(self) -> None:
        pass
