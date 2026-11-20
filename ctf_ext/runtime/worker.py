"""Worker 抽象与无模型兜底实现。

正式比赛由 LLM Worker 执行 Intent；没有模型凭证时 RuleBasedWorker 负责把
「读附件 → 解码 → 找 flag → 写证据」这条链路跑通（附录 F-4 端到端要求）。
"""
from __future__ import annotations

import base64
import binascii
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from ..domain.models import (
    OUTCOME_ERROR,
    OUTCOME_NO_SIGNAL,
    OUTCOME_SUCCESS,
)
from ..harness.contract import extract_flag
from ..store.blackboard import Claim, WorkerResult

MAX_ATTACHMENT_BYTES = 1_000_000


class Worker(ABC):
    """Worker 协议：领取的 Claim + 题目上下文 → 标准化 WorkerResult。"""
    @abstractmethod
    def work(self, claim: Claim, challenge: Dict) -> WorkerResult:
        """执行一次意图：产出经契约校验的 WorkerResult（不改黑板状态）。"""
        ...


class RuleBasedWorker(Worker):
    """确定性兜底：附件文本 / base64 中寻找 flag，并把原文登记为证据。"""

    name = "rule-based"

    def __init__(self, harness, evidence_store=None, state_store=None, control_factory=None, environment=None):
        """harness 仅允许 Mock 适配器；正式题目禁止本地保底解码（独立容器）。"""
        import uuid
        self._harness = harness
        self._evidence = evidence_store
        self._state = state_store
        self._control_factory = control_factory
        self._environment = environment or uuid.uuid4().hex

    def _read(self, ref: str) -> bytes:
        """读取附件（限 1MB）；异常由调用方记录后继续。"""
        return self._harness.read_attachment(ref, max_bytes=MAX_ATTACHMENT_BYTES)

    def work(self, claim: Claim, challenge: Dict) -> WorkerResult:
        """读取附件 → 证据登记 → base64 解码 → 提取 flag → 声明并返回结果。"""
        evidence_ids: List[str] = []
        texts: List[str] = []
        failures: List[str] = []
        offsets: List[str] = []
        control = self._control_factory(claim.challenge_id) if self._control_factory else None
        executor = None
        if self._state is not None:
            from ..gateway.execution import CommandExecutor
            from ..harness.mock import MockHarnessAdapter
            if not isinstance(self._harness, MockHarnessAdapter):
                raise PermissionError("本地保底解码只允许 Mock；正式题目必须使用独立容器")
            executor = CommandExecutor(self._state, control, self._environment, allow_local_mock=True)
        for ref in challenge.get("attachments") or ():
            try:
                data = self._read(ref)
            except Exception as exc:  # 附件缺失/损坏：记录后继续（§14.1）
                failures.append("attachment {}: {}".format(ref, type(exc).__name__))
                continue
            if self._evidence is not None:
                evidence_ids.append(self._evidence.put_bytes(data, kind="attachment"))
            text = data.decode("utf-8", "replace")
            if executor is not None:
                raw, eid, _, _ = executor.execute_mock_decoder(data, decode=False)
                text = raw.decode("utf-8", "replace")
                evidence_ids.append(eid)
            texts.append(text)
            decoded = self._try_base64(text)
            if decoded is not None:
                if executor is not None:
                    raw, eid, _, _ = executor.execute_mock_decoder("".join(text.split()).encode(), decode=True)
                    decoded = raw.decode("utf-8")
                    evidence_ids.append(eid)
                elif self._evidence is not None:
                    evidence_ids.append(self._evidence.put_bytes(decoded.encode("utf-8"), kind="decoded:" + ref))
                texts.append(decoded)
                offsets.append("base64:" + ref)

        for text in texts:
            flag = extract_flag(text)
            if flag:
                if self._state is not None:
                    self._state.declare(claim.challenge_id, flag)
                return WorkerResult(
                    intent_id=claim.intent_id, owner="", attempt_no=claim.attempt_no,
                    challenge_id=claim.challenge_id, strategy_key=claim.strategy_key,
                    status="completed", outcome=OUTCOME_SUCCESS,
                    summary=["从附件 {} 提取到 flag".format(offsets[0] if offsets else "原文")],
                    preconditions=["题目附件可读"], actions=["读取附件", "base64 尝试解码", "正则匹配 flag"],
                    ruled_out=["附件中不存在其他可见 flag 形态"],
                    next_best_action="验证后提交", evidence_ids=evidence_ids,
                    candidate_answer=flag,
                    facts=[{
                        "statement": "附件经 base64 解码后包含 flag 字符串",
                        "confidence": 0.95,
                        "evidence_ids": evidence_ids[-1:],
                    }],
                    resource_usage={"wall_seconds": 0, "model_tokens": 0, "tool_calls": 0},
                )
        return WorkerResult(
            intent_id=claim.intent_id, owner="", attempt_no=claim.attempt_no,
            challenge_id=claim.challenge_id, strategy_key=claim.strategy_key,
            status="blocked" if failures else "completed",
            outcome=OUTCOME_ERROR if failures else OUTCOME_NO_SIGNAL,
            summary=["未在附件中发现 flag" if not failures else "附件读取失败"],
            preconditions=["题目附件可读"], actions=["读取附件", "base64 尝试解码"],
            failure_reason="; ".join(failures) if failures else "无匹配证据",
            ruled_out=["附件原文与 base64 解码结果均无 flag{...}"],
            next_best_action="尝试其他编码/隐写分析", evidence_ids=evidence_ids,
            resource_usage={"wall_seconds": 0, "model_tokens": 0, "tool_calls": len(failures)},
        )

    @staticmethod
    def _try_base64(text: str) -> Optional[str]:
        """尝试 base64 严格解码并转 UTF-8；非可解码文本返回 None。"""
        compact = "".join(text.split())
        if len(compact) < 8:
            return None
        try:
            raw = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError):
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
