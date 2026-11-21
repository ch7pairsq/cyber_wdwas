"""推进心跳与孤儿收割（v20）：touch 开关、supervisor 判定输入、subreaper fail-open。

背景（2026-11-21 第 19 题正式赛）：worker 卡在 fork 阻塞，心跳（进程存活）
一直活着而 35 分钟无模型调用——claim_timeout 30 分钟才打断一次、重开后又卡。
v20 引入推进心跳（progress）与孤儿收割（subreaper）分别治"发现"与"根因"。
"""
from __future__ import annotations

import os
import time

from ctf_ext.runtime.progress import ENV_NAME, progress_path, touch_progress
from scripts.supervise import heartbeat_age


def test_progress_path_unset_by_default(monkeypatch):
    monkeypatch.delenv(ENV_NAME, raising=False)
    assert progress_path() is None
    assert touch_progress() is False  # 未配置时 no-op，不抛不落盘


def test_touch_progress_writes_and_refreshes_mtime(tmp_path, monkeypatch):
    path = str(tmp_path / "agent.progress")
    monkeypatch.setenv(ENV_NAME, path)
    assert progress_path() == path
    assert touch_progress(clock=lambda: 1234.5) is True
    assert os.path.exists(path)
    # 原子写不残留 .tmp；内容是时间戳
    assert not os.path.exists(path + ".tmp")
    assert open(path).read().strip() == "1234.5"
    # 二次刷新更新 mtime（supervisor 输入：age 变小）
    os.utime(path, (1234.6 - 0.1, 1234.6 - 0.1))
    first_age = heartbeat_age(path, now=1234.6)
    touch_progress(clock=lambda: 1234.6)
    assert heartbeat_age(path, now=1234.6) == 0.0
    assert 0 < first_age <= 0.11


def test_touch_progress_invalid_dir_fails_silently(tmp_path, monkeypatch):
    # 目录不存在 → OSError → False（推进判定是运维信号，不能打断解题）
    monkeypatch.setenv(ENV_NAME, str(tmp_path / "no-such-dir" / "agent.progress"))
    assert touch_progress() is False


def test_heartbeat_age_is_supervisor_progress_input(tmp_path):
    """supervisor 用同一 heartbeat_age 判 progress：新文件 age≈0，旧文件超阈。"""
    path = tmp_path / "agent.progress"
    path.write_text("0")
    past = time.time() - 700
    os.utime(path, (past, past))
    assert heartbeat_age(str(path), now=time.time()) > 600  # 超 600s 阈值 → 判卡死


def test_subreaper_fail_open(monkeypatch):
    """install_subreaper 不抛异常：Linux 返回 installed，macOS 返回 unavailable。

    真线程不在此测试里启动（2026-11-21 教训：Linux 镜像内真装 daemon 线程
    会常驻 pytest 进程，干扰后续对 SIGALRM 时序敏感的集成测试）——
    monkeypatch 替换 threading.Thread，只验证装配参数与 fail-open 契约。"""
    import ctf_ext.runtime.reaper as reaper_mod
    constructed = []

    class _FakeThread:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def start(self):
            raise AssertionError("测试不得启动真实收割线程")

    monkeypatch.setattr(reaper_mod.threading, "Thread", _FakeThread)
    status = reaper_mod.install_subreaper()
    assert isinstance(status, str) and status
    # 不强求平台：CI/开发机（macOS）无 prctl；Linux 容器内应 installed。
    if status == "installed":
        assert constructed  # 线程对象已构造但未真启动
    else:
        assert status.startswith("unavailable") and not constructed
