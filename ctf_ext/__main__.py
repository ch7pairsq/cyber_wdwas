"""命令行入口：python3 -m ctf_ext run [--mode mock|llm] [--config FILE] ...

无模型凭证时用 Mock Harness + RuleBasedWorker 跑通完整链路（附录 F-4）；
--mode llm 需要 DEEPSEEK_API_KEY 与 requests，缺少时明确报错而不是静默降级。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from .config import HotConfig
from .domain.migrate import migrate
from .gateway.policy import PolicyConfig, PolicyEngine
from .harness.mock import MockHarnessAdapter, default_practice_challenges
from .harness.submit import Submitter
from .llm.deepseek import DEFAULT_BASE_URL, DEFAULT_MODEL, DeepSeekProvider
from .runtime.dashboard import write_dashboard
from .runtime.llm_worker import LlmWorker
from .runtime.orchestrator import Orchestrator
from .runtime.scheduler import Scheduler
from .runtime.worker import RuleBasedWorker
from .store.blackboard import Blackboard
from .store.evidence import EvidenceStore

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "worker.md")


def _make_provider(cfg):
    model = cfg.get("model") or {}
    key_env = model.get("api_key_env", "DEEPSEEK_API_KEY")
    if not os.environ.get(key_env):
        raise SystemExit("model key missing: export {} before --mode llm".format(key_env))
    return DeepSeekProvider(
        base_url=model.get("base_url", DEFAULT_BASE_URL),
        model=model.get("model", DEFAULT_MODEL),
        timeout=float(model.get("request_timeout_seconds") or 120),
    )


def build_runtime(db_path: str, evidence_root: str, mode: str = "mock",
                  config_path: str = None, data_root: str = "."):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migrate(conn)

    hot = None
    cfg = {}
    if config_path:
        hot = HotConfig(config_path)
        cfg = hot.config
        hot.snapshot(conn, source="startup")

    evidence = EvidenceStore(evidence_root, db=conn)
    policy_cfg = cfg.get("policy") or {}
    download = cfg.get("download") or {}
    policy = PolicyEngine(PolicyConfig(
        authorized_scope=tuple(policy_cfg.get("authorized_scope") or ()),
        command_allowlist=tuple(policy_cfg.get("command_allowlist") or ()),
        max_single_bytes=int(download.get("max_single_bytes") or 0),
        max_total_bytes=int(download.get("max_total_bytes") or 0),
        competition_locked=bool((cfg.get("harness") or {}).get("competition_locked", False)),
    ))
    board = Blackboard(conn, evidence_check=evidence.verify)

    protocol = (cfg.get("harness") or {}).get("protocol", "mock")
    if protocol == "official":
        raise SystemExit("official harness is fail-closed: SDK/samples missing (design §12.5); "
                         "use harness.protocol=mock until the official adapter is provided")
    harness = MockHarnessAdapter(default_practice_challenges())
    submitter = Submitter(harness, policy_engine=policy, db=conn)

    conc = cfg.get("concurrency") or {}
    scheduler = Scheduler(board,
                          max_project_workers=int(conc.get("max_project_workers") or 1),
                          max_running_projects=int(conc.get("max_running_projects") or 1))
    if mode == "llm":
        worker = LlmWorker(_make_provider(cfg), prompt_path=PROMPT_PATH,
                           evidence_check=evidence.verify)
    else:
        worker = RuleBasedWorker(harness, evidence_store=evidence)
    orch = Orchestrator(harness, board, submitter, scheduler, worker, evidence=evidence,
                        hot_config=hot, policy_engine=policy, db=conn)
    if hot is not None:
        orch.apply_config(hot.config)
    return orch


def _emit(out, report):
    if report:
        os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
        with open(report, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ctf_ext")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="跑一轮端到端（默认 mock）")
    run.add_argument("--seconds", type=float, default=60.0)
    run.add_argument("--db", default="data/practice.db")
    run.add_argument("--evidence", default="data/evidence")
    run.add_argument("--mode", choices=("mock", "llm"), default="mock")
    run.add_argument("--config", default=None, help="JSON/YAML 配置；热更新按文件内容变化生效")
    run.add_argument("--report", default=None, help="把运行结果写成 JSON")
    run.add_argument("--dashboard", default=None, help="生成静态 HTML 看板")
    run.add_argument("--bench", default=None, help="压测结果 JSON，供看板展示")
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return 2
    orch = build_runtime(args.db, args.evidence, mode=args.mode, config_path=args.config)
    out = orch.run(max_seconds=args.seconds)
    _emit(out, args.report)
    if args.dashboard:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(args.db)
        conn.row_factory = _sqlite3.Row
        write_dashboard(conn, args.dashboard, bench_path=args.bench, db_path=args.db)
        conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["solved"] else 3


if __name__ == "__main__":
    sys.exit(main())
