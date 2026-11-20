"""Mock Harness：一条内置题目 + 固定 flag，打通输入→求解→输出（附录 F-4）。

提交幂等：同一 idempotency_key 重复提交返回首次回执，不产生第二次判定。
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

from .adapter import HarnessAdapter
from .schema import (
    ChallengeInput,
    HarnessContext,
    RunSummary,
    Submission,
    SubmitReceipt,
    parse_challenge,
)


def default_practice_challenges():
    """内置练习题目：附件是 base64，解码后得到 flag（F-4 端到端用）。"""
    import base64
    flag = "flag{mock_base64_001}"
    encoded = base64.b64encode(
        ("练习赛热身题\n本题只需解码附件即可得到提交答案。\n" + flag + "\n").encode("utf-8")
    ).decode("ascii")
    return [{
        "challenge_id": "ch-mock-1",
        "name": "base64 starter",
        "category": "misc",
        "description": "附件是一段 base64 文本，解码后可见 flag。",
        "attachments": ["mock://ch-mock-1/note.b64"],
        "points_hint": 950.0,
        "difficulty_level": 1,
        "metadata": {"files": {"mock://ch-mock-1/note.b64": encoded}},
        "flag": flag,
    }]


class MockHarnessAdapter(HarnessAdapter):
    """内置练习题适配器：固定 flag 判定 + 提交幂等，用于端到端链路验证。"""
    def __init__(self, challenges: List[Dict[str, object]], competition_id: str = "practice-mock"):
        """challenges 为原始题目 dict（含 metadata.files 附件与 flag 判定键）。"""
        self._raw = challenges
        self._competition_id = competition_id
        self._ctx: Optional[HarnessContext] = None
        self._tasks: List[ChallengeInput] = []
        self._receipts: Dict[str, SubmitReceipt] = {}

    def startup(self) -> HarnessContext:
        """建立 v1-mock 上下文（必须先于 read_tasks）。"""
        self._ctx = HarnessContext(protocol="v1-mock", competition_id=self._competition_id)
        return self._ctx

    def read_tasks(self) -> List[ChallengeInput]:
        """原始题目 dict → 标准化 ChallengeInput 列表。"""
        if not self._ctx:
            raise RuntimeError("startup() must be called before read_tasks()")
        self._tasks = [parse_challenge(raw, protocol="v1-mock") for raw in self._raw]
        return list(self._tasks)

    def submit_answer(self, submission: Submission) -> SubmitReceipt:
        """幂等判定：同 key 返回首次回执；答案精确匹配内置 flag 才 ACCEPTED。"""
        key = submission.idempotency_key
        if key in self._receipts:  # 幂等：不重复判定（附录 E）
            return self._receipts[key]
        task = next((t for t in self._tasks if t.challenge_id == submission.challenge_id), None)
        if task is None:
            receipt = SubmitReceipt(status="REJECTED", detail="unknown challenge")
        else:
            expected = str(self._raw[[t.challenge_id for t in self._tasks].index(task.challenge_id)].get("flag", ""))
            if submission.answer.strip() == expected.strip():
                receipt = SubmitReceipt(status="ACCEPTED", receipt_id="rcpt-" + key[:8])
            else:
                receipt = SubmitReceipt(status="REJECTED", detail="flag mismatch")
        self._receipts[key] = receipt
        return receipt

    def read_attachment(self, ref: str, *, max_bytes: int):
        """按 ref 返回附件字节；缺失即失败（不伪造内容）。"""
        for raw in self._raw:
            files = (raw.get("metadata") or {}).get("files") or {}
            if ref in files:
                data = str(files[ref]).encode("utf-8")
                if len(data) > max_bytes:
                    raise ValueError("attachment {} exceeds {} bytes".format(ref, max_bytes))
                return data
        raise FileNotFoundError(ref)

    def get_receipt(self, idempotency_key: str) -> Optional[SubmitReceipt]:
        """按幂等键查询已记录回执。"""
        return self._receipts.get(idempotency_key)

    def finalize(self, summary: RunSummary) -> dict:
        """汇总为 finalize 报告（协议/竞赛/结果/备注/时间戳）。"""
        return {
            "protocol": "v1-mock",
            "competition_id": self._competition_id,
            "results": summary.results,
            "notes": summary.notes,
            "finalized_at": time.time(),
        }
