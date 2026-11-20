from __future__ import annotations

import random

from cairn.dispatcher.config import WorkerConfig


def choose_worker(candidates: list[WorkerConfig], running_counts: dict[str, int]) -> list[WorkerConfig]:
    """worker 排序：优先级 → 当前负载 → 随机抖动，返回候选顺序。"""
    grouped = sorted(
        candidates,
        key=lambda worker: (
            worker.priority,
            running_counts.get(worker.name, 0),
            random.random(),
        ),
    )
    return grouped
