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
    pass


class SchemaError(Exception):
    pass


@dataclass
class HarnessContext:
    protocol: str
    competition_id: str
    deadline_epoch: float = 0.0


@dataclass
class ChallengeInput:
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
    challenge_id: str
    answer: str
    idempotency_key: str
    evidence_ids: List[str] = field(default_factory=list)


@dataclass
class SubmitReceipt:
    status: str  # ACCEPTED | REJECTED | UNKNOWN
    receipt_id: str = ""
    detail: str = ""
    corrected_answer: Optional[str] = None  # 仅平台显式给出的格式修正，不从错误文本推测


@dataclass
class RunSummary:
    results: List[Dict[str, Any]] = field(default_factory=list)
    notes: str = ""


def parse_challenge(raw: Dict[str, Any], protocol: str = "v1-mock") -> ChallengeInput:
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
