from __future__ import annotations

import abc
import re
import uuid
from dataclasses import dataclass

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.health import HealthResult


@dataclass(slots=True)
class DriverResult:
    """driver 构造的执行描述：argv 命令行与可选会话 id。"""
    argv: list[str]
    session: str | None = None


class WorkerDriver(abc.ABC):
    """worker 适配器抽象：健康探活 + 执行/收尾命令构造 + 会话与响应提取。"""
    type_name: str

    def supports_conclude(self) -> bool:
        """是否支持独立收尾阶段（否则收尾并入执行）。"""
        return True

    def local_binary(self) -> str | None:
        """Executable this driver invokes in local mode, checked on PATH at startup.

        None means the driver has no host binary to verify (or is not used locally).
        """
        return None

    def prepare_session(self) -> str | None:
        """预生成会话 id（需要预置会话的 driver 覆写）。"""
        return None

    @abc.abstractmethod
    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        """Verify this worker's LLM config is usable, in-process (no container, no curl)."""
        raise NotImplementedError

    def describe_health(self, worker: WorkerConfig) -> str:
        """健康检查方式说明（日志/诊断用）。"""
        return "in-process API ping"

    @abc.abstractmethod
    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        """构造执行阶段命令（含提示词注入方式与会话续接）。"""
        raise NotImplementedError

    @abc.abstractmethod
    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        """构造收尾阶段命令（沿用既有会话）。"""
        raise NotImplementedError

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        """从输入/输出中提取会话 id（默认透传输入值）。"""
        return session

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        """从进程输出提取响应正文（默认 stdout）。"""
        return stdout


class SeedSessionDriver(WorkerDriver):
    """预生成会话 id 的 mixin：prepare_session 返回随机 UUID。"""
    def prepare_session(self) -> str | None:
        """返回新建会话 id。"""
        return str(uuid.uuid4())


class RegexSessionDriver(WorkerDriver):
    """从 stderr 正则提取会话 id 的 mixin。"""
    session_pattern = re.compile(r"session id:\s*([0-9a-fA-F-]+)")

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        """优先透传已有会话，否则从 stderr 匹配。"""
        if session:
            return session
        match = self.session_pattern.search(stderr)
        if match:
            return match.group(1)
        return None
