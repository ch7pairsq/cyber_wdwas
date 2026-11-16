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

    @abstractmethod
    def submit_answer(self, submission: Submission) -> SubmitReceipt: ...

    def get_receipt(self, idempotency_key: str) -> Optional[SubmitReceipt]:
        """UNKNOWN 时只查询回执，不重发（附录 E）。"""
        return None

    @abstractmethod
    def finalize(self, summary: RunSummary) -> dict: ...

    def shutdown(self) -> None:
        pass


class OfficialHarnessAdapter(HarnessAdapter):
    """占位：官方 SDK 未拿到前禁止任何实现细节（§12.5 待官方材料）。"""

    def startup(self) -> HarnessContext:
        raise HarnessUnavailable(
            "official harness SDK not available; see design doc section 12 "
            "(startup entry, IO schema, submission API all pending)"
        )

    def read_tasks(self):
        raise HarnessUnavailable("official harness SDK not available")

    def submit_answer(self, submission: Submission) -> SubmitReceipt:
        raise HarnessUnavailable("official harness SDK not available")

    def finalize(self, summary: RunSummary) -> dict:
        raise HarnessUnavailable("official harness SDK not available")
