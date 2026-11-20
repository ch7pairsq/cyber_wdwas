"""HarnessAdapter 抽象与实现选择（§12、附录 B.1）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from .schema import ChallengeInput, HarnessContext, RunSummary, Submission, SubmitReceipt


class HarnessUnavailable(Exception):
    """适配器能力不可用（无附件/无凭证/平台不可达）时的统一异常。"""
    pass


class HarnessAdapter(ABC):
    """唯一平台协议边界：题目读取、附件、提交与回执全部经此抽象。"""
    @abstractmethod
    def startup(self) -> HarnessContext:
        """建立协议上下文（必须先于 read_tasks）。"""
        ...

    @abstractmethod
    def read_tasks(self) -> List[ChallengeInput]:
        """拉取并标准化全部题目输入。"""
        ...

    def read_attachment(self, ref: str, *, max_bytes: int):
        """按引用读取附件；不支持附件的平台默认拒绝。"""
        raise HarnessUnavailable("attachments not supported by this adapter")

    def emit_progress(self, event: dict) -> None:
        """进度上报钩子（可选实现；默认丢弃）。"""
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
    def submit_answer(self, submission: Submission) -> SubmitReceipt:
        """提交候选答案并返回回执（实现方自行保证幂等）。"""
        ...

    def get_receipt(self, idempotency_key: str) -> Optional[SubmitReceipt]:
        """UNKNOWN 时只查询回执，不重发（附录 E）。"""
        return None

    @abstractmethod
    def finalize(self, summary: RunSummary) -> dict:
        """上报整轮汇总并结束本次参赛。"""
        ...

    def shutdown(self) -> None:
        """释放平台资源（默认无操作）。"""
        pass
