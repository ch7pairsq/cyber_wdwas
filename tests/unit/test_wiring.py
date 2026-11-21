"""接线测试层：断言"装配结果"与"模型可见面"，补 8 题自测的盲区。

8 题模拟自测验证解题编排语义（可解/诱饵/错旗重开），从不走真实靶机
交互路径——三届 static_file_server 同一失败模式的根源。本文件锁死四条
接线（2026-11-21 正式赛四层根因的回归）：

1. arena 协议下 build_runtime 必须把 authorized_arena_targets 同步为
   ARENA_NETWORK_CIDR（平台不注入，缺它 arena_http 全程 DENY）；
2. 平台材料（start_info/arena_help）必须进模型首条消息；
3. 减法环境（只含平台快照变量）下 arena_http 对缺 CIDR 的拒绝信息明确；
4. 测试环境变量集合 ⊆ 生产快照（fixtures/golden/platform_env_snapshot）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

from tests.framework_golden import load_golden

from ctf_ext.__main__ import build_runtime
from ctf_ext.harness.arena import ArenaHarnessAdapter
from ctf_ext.runtime.orchestrator import Orchestrator
from ctf_ext.runtime.llm_worker import LlmWorker
from ctf_ext.runtime.worker import RuleBasedWorker

ARENA_CONFIG = """\
harness:
  protocol: arena
  competition_locked: true
  arena:
    base_url: http://127.0.0.1:9
    env_policy: on_demand
    auto_start: false
policy:
  authorized_arena_targets: ["10.201.0.0/16"]
concurrency:
  max_workers: 1
  max_project_workers: 1
storage:
  db_path: /tmp/wiring-unused.db
model:
  tools_enabled: true
"""


@pytest.fixture()
def clean_arena_env():
    """减法原则：测试环境的平台变量必须 ⊆ 生产快照——先清空再按需注入。"""
    snapshot = load_golden("platform_env_snapshot")
    saved = {}
    names = list(snapshot["env_subset_of_production"]) + ["ARENA_NETWORK_CIDR"]
    for name in names:
        saved[name] = os.environ.pop(name, None)
    yield snapshot
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_build_runtime_arena_wiring_injects_cidr(clean_arena_env):
    """arena 装配接线：CIDR 从配置注入、harness 类型正确、worker 不需凭据。"""
    os.environ["ARENA_SOLVER_TOKEN"] = "st_wiring_fake_token"
    try:
        with tempfile.TemporaryDirectory() as root:
            cfg = os.path.join(root, "arena.yaml")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(ARENA_CONFIG)
            orch = build_runtime(root + "/run.db", root + "/evidence",
                                 mode="mock", config_path=cfg)
            try:
                assert isinstance(orch, Orchestrator)
                assert isinstance(orch._harness, ArenaHarnessAdapter)
                assert isinstance(orch._worker, RuleBasedWorker)
                # 正式赛教训：平台不注入 CIDR，arena_http 缺它直接 DENY——
                # 装配层必须从 authorized_arena_targets 补齐。
                assert os.environ.get("ARENA_NETWORK_CIDR") == "10.201.0.0/16"
            finally:
                orch._gateway.close()
                orch._board.close()
    finally:
        os.environ.pop("ARENA_SOLVER_TOKEN", None)
        os.environ.pop("ARENA_NETWORK_CIDR", None)


def test_arena_http_without_cidr_denies_with_clear_reason(clean_arena_env):
    """减法环境：CIDR unset 时 arena_http 必须 fail-closed 且拒绝原因明确。"""
    assert "ARENA_NETWORK_CIDR" not in os.environ  # 快照形态：平台不注入
    tool = os.path.join(os.path.dirname(__file__), "..", "..",
                        "ctf_ext", "gateway", "arena_http.py")
    proc = subprocess.run([sys.executable, os.path.abspath(tool),
                           "--host", "10.201.0.201", "--port", "3000"],
                          capture_output=True, text=True, timeout=30)
    out = proc.stdout + proc.stderr
    assert "ARENA_NETWORK_CIDR is required" in out  # 明确缺什么，不是模糊失败


def test_platform_material_reaches_model_first_message(clean_arena_env):
    """平台材料接线：start_info/arena_help 必须出现在模型首条消息里。"""
    challenge = {
        "challenge_id": "c1", "name": "static file server", "category": "web",
        "description": "find the flag",
        "metadata": {"arena": {
            "start_info": {"started": True, "container_addr": "10.201.0.201:3000"},
            "help": "Targets are reachable only through the platform tunnel.",
        }},
        "attachments": (),
    }

    class FirstCallProvider:
        def __init__(self):
            self.calls = []

        def complete(self, messages, **kwargs):
            self.calls.append(list(messages))
            raise RuntimeError("stop at first call")  # 只需检查消息构造

    from types import SimpleNamespace
    claim = SimpleNamespace(intent_id="i1", challenge_id="c1",
                            strategy_key="web.triage.auto", params={},
                            budget_seconds=60, attempt_no=1)
    provider = FirstCallProvider()
    worker = LlmWorker(provider)
    try:
        worker.work(claim, challenge)  # RuntimeError 会被异常分类捕获，不外抛
    except RuntimeError:
        pass
    first = provider.calls[0][0]["content"]
    payload = json.loads(first)
    assert payload["challenge"]["start_info"] == {
        "started": True, "container_addr": "10.201.0.201:3000"}
    assert "platform tunnel" in payload["challenge"]["arena_help"]
