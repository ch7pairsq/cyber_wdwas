"""官方 Harness 适配器（§12.5）：未拿到 SDK 前保持 Fail Closed，不臆造协议。

SDK 通过注入提供（未来由官方包替换）。适配器只做三件事：
1. 校验 SDK 是否暴露完整方法集，缺失即抛 OfficialProtocolUnavailable；
2. 一律转发，不做任何默认值/兜底/降级；
3. 任何调用失败向上抛，调用方按协议错误处理，绝不静默当作成功。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

REQUIRED_METHODS = (
    "startup", "read_tasks", "read_attachment", "submit_answer", "get_receipt", "finalize",
)


class OfficialProtocolUnavailable(RuntimeError):
    """SDK 缺失或方法不全：属于阻塞项，不是运行时异常。"""

    def __init__(self, missing: Sequence[str], detail: str = ""):
        """missing 为缺失方法名列表，直接拼入异常消息。"""
        self.missing = list(missing)
        message = "official harness unavailable; missing: {}".format(", ".join(self.missing))
        super().__init__((message + (" (" + detail + ")") if detail else message))


class OfficialHarnessAdapter:
    """官方协议适配器：只转发不兜底，方法集不全即拒绝运行。"""
    protocol = "official"

    def __init__(self, sdk: Optional[Any] = None):
        """sdk 为官方注入的实现对象；None 时 capability_report 报告全部缺失。"""
        self._sdk = sdk

    # ------------------------------------------------------------------ gates
    def _missing(self) -> List[str]:
        """返回 SDK 缺失的必需方法名列表（空列表 = 协议就绪）。"""
        if self._sdk is None:
            return list(REQUIRED_METHODS)
        return [name for name in REQUIRED_METHODS if not callable(getattr(self._sdk, name, None))]

    def capability_report(self) -> Dict[str, Any]:
        """协议就绪度报告：ready/missing/required（预检与启动诊断用）。"""
        missing = self._missing()
        return {"protocol": self.protocol, "ready": not missing, "missing": missing,
                "required": list(REQUIRED_METHODS)}

    def _call(self, name: str, *args, **kwargs):
        """统一转发入口：方法缺失即抛 OfficialProtocolUnavailable（fail-closed）。"""
        missing = self._missing()
        if missing:
            raise OfficialProtocolUnavailable(missing, "§12.5 SDK/样例未就绪")
        return getattr(self._sdk, name)(*args, **kwargs)

    # ------------------------------------------------------------- B.1 surface
    def startup(self) -> None:
        """转发 startup（方法集不全即抛 OfficialProtocolUnavailable）。"""
        return self._call("startup")

    def read_tasks(self):
        """转发 read_tasks。"""
        return self._call("read_tasks")

    def read_attachment(self, ref: str, *, max_bytes: Optional[int] = None) -> bytes:
        """转发 read_attachment。"""
        return self._call("read_attachment", ref, max_bytes=max_bytes)

    def submit_answer(self, submission):
        """转发 submit_answer。"""
        return self._call("submit_answer", submission)

    def get_receipt(self, idempotency_key: str):
        """转发 get_receipt。"""
        return self._call("get_receipt", idempotency_key)

    def finalize(self, summary):
        """转发 finalize。"""
        return self._call("finalize", summary)
