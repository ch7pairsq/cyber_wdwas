"""Harness 数据契约与退出码（§12、附录 B.1）。

内部统一形状，不要求赛事平台提供同名方法；协议版本不识别即失败退出
（§12.1：不支持的版本直接失败，不静默降级）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SUPPORTED_PROTOCOLS = {"v1-mock", "v1-arena"}

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_NO_TASK = 3
EXIT_TIMEOUT = 4
EXIT_SUBMIT_ERROR = 5


class UnsupportedProtocol(Exception):
    """协议版本不在 SUPPORTED_PROTOCOLS 内：直接失败，不静默降级。"""
    pass


class SchemaError(Exception):
    """题目数据不符合必需字段/类型契约。"""
    pass


@dataclass
class HarnessContext:
    """运行上下文：协议版本、竞赛标识与截止时间。"""
    protocol: str
    competition_id: str
    deadline_epoch: float = 0.0


@dataclass
class ChallengeInput:
    """标准化题目输入：id/名称/分类/描述/附件/难度（由 parse_challenge 构造）。"""
    challenge_id: str
    name: str
    category: str = "unknown"
    description: str = ""
    attachments: tuple = ()
    points_hint: float = 0.0
    difficulty_level: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Submission:
    """提交载荷：幂等键 + 答案 + 支撑证据 id 列表。"""
    challenge_id: str
    answer: str
    idempotency_key: str
    evidence_ids: List[str] = field(default_factory=list)


@dataclass
class SubmitReceipt:
    """提交回执三态：ACCEPTED/REJECTED/UNKNOWN；corrected_answer 仅限平台显式修正。"""
    status: str  # ACCEPTED | REJECTED | UNKNOWN
    receipt_id: str = ""
    detail: str = ""
    corrected_answer: Optional[str] = None  # 仅平台显式给出的格式修正，不从错误文本推测


@dataclass
class RunSummary:
    """一轮运行汇总：逐题结果列表与备注（finalize 入参）。"""
    results: List[Dict[str, Any]] = field(default_factory=list)
    notes: str = ""


def parse_challenge(raw: Dict[str, Any], protocol: str = "v1-mock") -> ChallengeInput:
    """原始 dict → ChallengeInput：校验协议与必需字段，缺失即抛 SchemaError。"""
    if protocol not in SUPPORTED_PROTOCOLS:
        raise UnsupportedProtocol("protocol {!r} not supported; supported={}".format(
            protocol, sorted(SUPPORTED_PROTOCOLS)))
    for key in ("challenge_id", "name"):
        if not raw.get(key):
            raise SchemaError("missing required field: " + key)
    return ChallengeInput(
        challenge_id=str(raw["challenge_id"]),
        name=str(raw["name"]),
        category=str(raw.get("category", "unknown")),
        description=str(raw.get("description", "")),
        attachments=tuple(raw.get("attachments", ())),
        points_hint=float(raw.get("points_hint", 0)),
        difficulty_level=int(raw.get("difficulty_level", 0)),
        metadata=dict(raw.get("metadata", {})),
    )
