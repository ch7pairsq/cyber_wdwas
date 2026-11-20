from __future__ import annotations

from dataclasses import dataclass

from cairn.dispatcher.runtime.cancellation import TaskCancellation


@dataclass(slots=True)
class RunningTask:
    """在途任务记录：项目/类型/worker 与取消控制柄，附带 checkpoint 计数。"""
    project_id: str
    task_type: str
    worker_name: str
    cancellation: TaskCancellation
    intent_id: str | None = None
    fact_count: int | None = None
    hint_count: int | None = None
    open_intent_count: int | None = None


@dataclass(slots=True)
class ReasonCheckpoint:
    """reason 任务启动时的项目计数快照，用于回写时对比新增量。"""
    fact_count: int
    hint_count: int
    open_intent_count: int
