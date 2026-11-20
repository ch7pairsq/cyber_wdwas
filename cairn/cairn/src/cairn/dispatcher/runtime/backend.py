from __future__ import annotations

from typing import Protocol, runtime_checkable

from cairn.dispatcher.runtime.process import ExecProcess


@runtime_checkable
class ExecutionBackend(Protocol):
    """The execution substrate for worker processes.

    Two implementations exist: ContainerManager (one Docker container per project) and
    LocalBackend (host subprocesses, one working directory per project). The scheduler,
    task runners and startup healthcheck only depend on this surface so the two backends
    are interchangeable behind a single ``runtime.execution`` switch.
    """

    def container_name(self, project_id: str) -> str:
        """项目对应的执行单元名（容器名或工作区名）。"""
        ...

    def ensure_running(self, project_id: str) -> str:
        """确保执行单元就绪，返回其名称。"""
        ...

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> ExecProcess:
        """构造一个 ExecProcess（容器 exec 或本地子进程）。"""
        ...

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        """向执行单元内写入文本文件（提示词/快照注入）。"""
        ...

    def needs_completed_cleanup(self, project_id: str) -> bool:
        """项目结束后是否需要清理动作。"""
        ...

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        """项目停止后是否需要清理动作。"""
        ...

    def cleanup_completed(self, project_id: str) -> bool:
        """执行“结束后”处置（删除/保留），返回是否执行。"""
        ...

    def cleanup_stopped(self, project_id: str) -> bool:
        """执行“停止后”处置，返回是否执行。"""
        ...

    def close(self) -> None:
        """释放后端资源（Docker 客户端等）。"""
        ...
