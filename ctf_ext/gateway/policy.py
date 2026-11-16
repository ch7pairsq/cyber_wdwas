"""策略引擎：硬规则在代码层强制（§15、§16、附录 B.4）。

判定顺序：SCHEME → SCOPE → PRIVATE_NETWORK → QUOTA → COMMAND → SANDBOX → SUBMIT_LOCK。
未配置的限额一律 Fail Closed（§15：X 未提供时禁止下载）。
调用方必须用 enforce() 消费判定结果，禁止“读到 deny 还继续”。
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple
from urllib.parse import urlparse


class PolicyDenied(Exception):
    def __init__(self, reason_code: str, message: str = ""):
        super().__init__(message or reason_code)
        self.reason_code = reason_code


ALLOW = "allow"
DENY = "deny"
REQUIRE_APPROVAL = "require_approval"


@dataclass
class PolicyConfig:
    authorized_scope: Tuple[str, ...] = ()
    command_allowlist: Tuple[str, ...] = ()
    # 下载限额：0 表示未配置 → 全部拒绝（Fail Closed）
    max_single_bytes: int = 0
    max_total_bytes: int = 0
    competition_locked: bool = False
    sandbox_required: bool = True
    # 目标范围校验：拒绝解析到私网/回环/链路本地/保留地址的主机（§15.1 SSRF 防护）
    block_private_networks: bool = True
    # execute 类工具是否需要人工审批（附录 B.4 require_approval）
    require_approval_for_execute: bool = False


@dataclass
class Decision:
    action: str
    reason_code: str = ""


class PolicyEngine:
    def __init__(self, config: PolicyConfig, total_used_ref=None, resolver=None):
        self._cfg = config
        # total_used_ref: 返回已用下载字节的 callable（配额共享，§15.1）
        self._total_used_ref = total_used_ref or (lambda: 0)
        # resolver: 域名 → IP 列表；测试注入，避免依赖真实 DNS
        self._resolver = resolver or self._default_resolver
        self._dns_cache = {}

    def update_limits(self, max_single_bytes=None, max_total_bytes=None) -> None:
        """运行期热更新下载限额（§13）：只允许收紧或按运维指令放宽，均由调用方负责审计。"""
        if max_single_bytes is not None:
            self._cfg.max_single_bytes = int(max_single_bytes)
        if max_total_bytes is not None:
            self._cfg.max_total_bytes = int(max_total_bytes)

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
        parsed = urlparse(target)
        host = parsed.hostname or target
        if host not in self._cfg.authorized_scope:
            return Decision(DENY, "SCOPE")
        if self._cfg.block_private_networks and self._is_private(host):
            return Decision(DENY, "PRIVATE_NETWORK")
        return Decision(ALLOW)

    @staticmethod
    def _default_resolver(host: str):
        return socket.gethostbyname_ex(host)[2]

    def _is_private(self, host: str) -> bool:
        if host in self._dns_cache:
            ips = self._dns_cache[host]
        else:
            try:
                ips = list(self._resolver(host))
            except Exception:
                ips = []
            self._dns_cache[host] = ips
        if not ips:  # 解析失败 → 无法证明是公网目标 → 拒绝
            return True
        for raw in ips:
            try:
                addr = ipaddress.ip_address(raw)
            except ValueError:
                return True
            if (addr.is_private or addr.is_loopback or addr.is_link_local
                    or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
                return True
        return False

    # ---------------------------------------------------------------- download
    def decide_download(self, url: str, content_length: Optional[int] = None) -> Decision:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return Decision(DENY, "QUOTA_SCHEME")
        host = parsed.hostname or ""
        if host not in self._cfg.authorized_scope:
            return Decision(DENY, "SCOPE")
        if self._cfg.block_private_networks and self._is_private(host):
            return Decision(DENY, "PRIVATE_NETWORK")
        if self._cfg.max_single_bytes <= 0 or self._cfg.max_total_bytes <= 0:
            return Decision(DENY, "QUOTA_UNSET")  # Fail Closed：X 未配置
        if content_length is not None and content_length > self._cfg.max_single_bytes:
            return Decision(DENY, "QUOTA_SIZE")
        if self._total_used_ref() + (content_length or 0) > self._cfg.max_total_bytes:
            return Decision(DENY, "QUOTA_TOTAL")
        return Decision(ALLOW)

    def decide_total(self, url: str, total_used: int) -> Decision:
        """下载完成后的总配额复核：只看累计字节，不再套用单文件上限。"""
        host = urlparse(url).hostname or ""
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

    # --------------------------------------------------------------- tool risk
    def decide_tool_risk(self, risk_level: str) -> Decision:
        """B.4：execute 类工具在无沙箱时拒绝，需要审批时返回 require_approval。"""
        if risk_level == "execute":
            if not self._cfg.sandbox_required:
                return Decision(ALLOW)
            if self._cfg.require_approval_for_execute:
                return Decision(REQUIRE_APPROVAL, "TOOL_APPROVAL")
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
