"""命令行入口：python3 -m ctf_ext run [--seconds 60] [--db data/practice.db]

无模型凭证时用 Mock Harness + RuleBasedWorker 跑通完整链路（附录 F-4）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from .domain.migrate import migrate
from .gateway.policy import PolicyConfig, PolicyEngine
from .harness.mock import MockHarnessAdapter, default_practice_challenges
from .harness.submit import Submitter
from .runtime.orchestrator import Orchestrator
from .runtime.scheduler import Scheduler
from .runtime.worker import RuleBasedWorker
from .store.blackboard import Blackboard
from .store.evidence import EvidenceStore


def build_runtime(db_path: str, evidence_root: str, data_root: str = "."):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    evidence = EvidenceStore(evidence_root, db=conn)
    policy = PolicyEngine(PolicyConfig(
        authorized_scope=(), command_allowlist=(), max_single_bytes=0, max_total_bytes=0,
        competition_locked=False, block_private_networks=True,
    ))
    board = Blackboard(conn, evidence_check=evidence.verify)
    harness = MockHarnessAdapter(default_practice_challenges())
    submitter = Submitter(harness, policy_engine=policy, db=conn)
    scheduler = Scheduler(board, max_project_workers=1, max_running_projects=1)
    worker = RuleBasedWorker(harness, evidence_store=evidence)
    return Orchestrator(harness, board, submitter, scheduler, worker, evidence=evidence)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ctf_ext")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="跑一轮 mock 端到端")
    run.add_argument("--seconds", type=float, default=60.0)
    run.add_argument("--db", default="data/practice.db")
    run.add_argument("--evidence", default="data/evidence")
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return 2
    orch = build_runtime(args.db, args.evidence)
    out = orch.run(max_seconds=args.seconds)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["solved"] else 3


if __name__ == "__main__":
    sys.exit(main())
