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
        self.missing = list(missing)
        message = "official harness unavailable; missing: {}".format(", ".join(self.missing))
        super().__init__((message + (" (" + detail + ")") if detail else message))


class OfficialHarnessAdapter:
    protocol = "official"

    def __init__(self, sdk: Optional[Any] = None):
        self._sdk = sdk

    # ------------------------------------------------------------------ gates
    def _missing(self) -> List[str]:
        if self._sdk is None:
            return list(REQUIRED_METHODS)
        return [name for name in REQUIRED_METHODS if not callable(getattr(self._sdk, name, None))]

    def capability_report(self) -> Dict[str, Any]:
        missing = self._missing()
        return {"protocol": self.protocol, "ready": not missing, "missing": missing,
                "required": list(REQUIRED_METHODS)}

    def _call(self, name: str, *args, **kwargs):
        missing = self._missing()
        if missing:
            raise OfficialProtocolUnavailable(missing, "§12.5 SDK/样例未就绪")
        return getattr(self._sdk, name)(*args, **kwargs)

    # ------------------------------------------------------------- B.1 surface
    def startup(self) -> None:
        return self._call("startup")

    def read_tasks(self):
        return self._call("read_tasks")

    def read_attachment(self, ref: str, *, max_bytes: Optional[int] = None) -> bytes:
        return self._call("read_attachment", ref, max_bytes=max_bytes)

    def submit_answer(self, submission):
        return self._call("submit_answer", submission)

    def get_receipt(self, idempotency_key: str):
        return self._call("get_receipt", idempotency_key)

    def finalize(self, summary):
        return self._call("finalize", summary)
