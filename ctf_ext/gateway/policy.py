"""策略引擎：硬规则在代码层强制（§16、附录 B.4）。

判定顺序：SCOPE → QUOTA → COMMAND → SANDBOX → SUBMIT_LOCK。
未配置的限额一律 Fail Closed（§15：X 未提供时禁止下载）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple
from urllib.parse import urlparse


class PolicyDenied(Exception):
    def __init__(self, reason_code: str, message: str = ""):
        super().__init__(message or reason_code)
        self.reason_code = reason_code


ALLOW = "allow"
DENY = "deny"


@dataclass
class PolicyConfig:
    authorized_scope: Tuple[str, ...] = ()
    command_allowlist: Tuple[str, ...] = ()
    # 下载限额：0 表示未配置 → 全部拒绝（Fail Closed）
    max_single_bytes: int = 0
    max_total_bytes: int = 0
    competition_locked: bool = False
    sandbox_required: bool = True


@dataclass
class Decision:
    action: str
    reason_code: str = ""


class PolicyEngine:
    def __init__(self, config: PolicyConfig, total_used_ref=None):
        self._cfg = config
        # total_used_ref: 返回已用下载字节的 callable（配额共享，§15.1）
        self._total_used_ref = total_used_ref or (lambda: 0)

    def bind_total_used(self, ref) -> None:
        """绑定运行期已用字节来源（如 DownloadController.total_used）。"""
        self._total_used_ref = ref

    @staticmethod
    def enforce(decision: Decision) -> None:
        """deny 即抛 PolicyDenied：硬规则不允许调用方“读到 deny 还继续”。"""
        if decision.action == DENY:
            raise PolicyDenied(decision.reason_code)

    # ------------------------------------------------------------------ scope
    def decide_scope(self, target: str) -> Decision:
        host = urlparse(target).hostname or target
        if host not in self._cfg.authorized_scope:
            return Decision(DENY, "SCOPE")
        return Decision(ALLOW)

    # ---------------------------------------------------------------- download
    def decide_download(self, url: str, content_length: Optional[int] = None) -> Decision:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return Decision(DENY, "QUOTA_SCHEME")
        host = parsed.hostname or ""
        if host not in self._cfg.authorized_scope:
            return Decision(DENY, "SCOPE")
        if self._cfg.max_single_bytes <= 0 or self._cfg.max_total_bytes <= 0:
            return Decision(DENY, "QUOTA_UNSET")  # Fail Closed：X 未配置
        if content_length is not None and content_length > self._cfg.max_single_bytes:
            return Decision(DENY, "QUOTA_SIZE")
        if self._total_used_ref() + (content_length or 0) > self._cfg.max_total_bytes:
            return Decision(DENY, "QUOTA_TOTAL")
        return Decision(ALLOW)

    def decide_total(self, url: str, total_used: int) -> Decision:
        """下载完成后的总配额复核：只看累计字节，不再套用单文件上限。"""
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host not in self._cfg.authorized_scope:
            return Decision(DENY, "SCOPE")
        if self._cfg.max_total_bytes <= 0:
            return Decision(DENY, "QUOTA_UNSET")
        if total_used > self._cfg.max_total_bytes:
            return Decision(DENY, "QUOTA_TOTAL")
        return Decision(ALLOW)

    def remaining_budget(self) -> int:
        return max(0, self._cfg.max_total_bytes - self._total_used_ref())

    # ----------------------------------------------------------------- command
    def decide_command(self, argv: Sequence[str]) -> Decision:
        if not argv:
            return Decision(DENY, "COMMAND_EMPTY")
        program = argv[0].rsplit("/", 1)[-1]
        if program not in self._cfg.command_allowlist:
            return Decision(DENY, "COMMAND_NOT_ALLOWED")
        return Decision(ALLOW)

    # --------------------------------------------------------------- submission
    def decide_submission(self) -> Decision:
        if self._cfg.competition_locked:
            return Decision(DENY, "SUBMIT_LOCK")
        return Decision(ALLOW)

    # ------------------------------------------------------------------ sandbox
    def decide_sandbox(self, available: bool) -> Decision:
        if self._cfg.sandbox_required and not available:
            return Decision(DENY, "SANDBOX")
        return Decision(ALLOW)
