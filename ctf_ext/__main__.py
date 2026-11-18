"""命令行入口：python3 -m ctf_ext run [--mode mock|llm] [--config FILE] ...

无模型凭证时用 Mock Harness + RuleBasedWorker 跑通完整链路（附录 F-4）；
--mode llm 仅使用公司代理与 DeepSeek；缺少 BAIZHI_API_KEY 时拒绝调用。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from .config import ConfigError, HotConfig
from .domain.migrate import migrate
from .gateway.policy import PolicyConfig, PolicyEngine
from .gateway.catalog import register_default_tools
from .gateway.tools import ToolRegistry
from .harness.mock import MockHarnessAdapter, default_practice_challenges
from .harness.flag_gate import FlagGate, ProviderReviewer
from .harness.gateway import SubmitGateway
from .store.session import StateStore
from .runtime.control import SessionControl, StepPolicy
from .llm.deepseek import DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_KEY_ENV, PROVIDER_NAME, DeepSeekProvider
from .runtime.dashboard import write_dashboard
from .runtime.llm_worker import LlmWorker
from .runtime.orchestrator import Orchestrator
from .runtime.scheduler import Scheduler
from .runtime.worker import RuleBasedWorker
from .skills.loader import SkillLoader
from .store.blackboard import Blackboard
from .store.evidence import EvidenceStore

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "worker.md")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATHS = (
    os.path.join(REPO_ROOT, "config", "base.yaml"),
    os.path.join(REPO_ROOT, "config", "practice.yaml"),
)


def _make_provider(cfg):
    model = cfg.get("model") or {}
    if model.get("provider", PROVIDER_NAME) != PROVIDER_NAME:
        raise SystemExit("only the company Anthropic-compatible gateway is allowed")
    key_env = model.get("api_key_env", DEFAULT_KEY_ENV)
    if not os.environ.get(key_env):
        raise SystemExit("model key missing: export {} before --mode llm".format(key_env))
    return DeepSeekProvider(
        api_key=os.environ[key_env],
        base_url=model.get("base_url", DEFAULT_BASE_URL),
        model=model.get("model", DEFAULT_MODEL),
        timeout=float(model.get("request_timeout_seconds") or 120),
    )


def build_runtime(db_path: str, evidence_root: str, mode: str = "mock",
                  config_path: str = None, data_root: str = ".",
                  heartbeat_path: str = None, claim_timeout_seconds: float = None):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    # 显式事务模式：黑板用 BEGIN IMMEDIATE 控制边界，证据登记不能留下隐式事务。
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migrate(conn)

    hot = None
    cfg = {}
    config_paths = [config_path] if config_path else [p for p in DEFAULT_CONFIG_PATHS
                                                       if os.path.exists(p)]
    if config_paths:
        try:
            hot = HotConfig(*config_paths)
        except ConfigError as exc:
            # 默认配置只在显式依赖缺失时退回代码默认值；--config 仍严格失败。
            if config_path or "PyYAML is required" not in str(exc):
                raise
        if hot is not None:
            cfg = hot.config
            hot.snapshot(conn, source="startup")

    evidence = EvidenceStore(evidence_root, db=conn)
    policy_cfg = cfg.get("policy") or {}
    download = cfg.get("download") or {}
    policy = PolicyEngine(PolicyConfig(
        authorized_scope=tuple(policy_cfg.get("authorized_scope") or ()),
        authorized_arena_targets=tuple(policy_cfg.get("authorized_arena_targets") or ()),
        command_allowlist=tuple(policy_cfg.get("command_allowlist") or ()),
        max_single_bytes=int(download.get("max_single_bytes") or 0),
        max_total_bytes=int(download.get("max_total_bytes") or 0),
        competition_locked=bool((cfg.get("harness") or {}).get("competition_locked", False)),
        sandbox_required=bool(policy_cfg.get("sandbox_required", True)),
    ))
    board = Blackboard(conn, evidence_check=evidence.verify)

    protocol = (cfg.get("harness") or {}).get("protocol", "mock")
    if protocol == "official":
        raise SystemExit("official harness is fail-closed: SDK/samples missing (design §12.5); "
                         "use harness.protocol=mock until the official adapter is provided")
    if protocol == "arena":
        from .harness.arena import ArenaHarnessAdapter, ArenaProtocolError
        arena_cfg = (cfg.get("harness") or {}).get("arena") or {}
        base_url = os.environ.get("ARENA_BASE_URL") or arena_cfg.get("base_url") or ""
        token = os.environ.get("ARENA_SOLVER_TOKEN") or ""
        if not base_url or not token:
            raise SystemExit("arena harness requires ARENA_BASE_URL and ARENA_SOLVER_TOKEN (runtime env only)")
        arena_kwargs = {k: arena_cfg[k] for k in
                        ("help_path", "list_path", "start_path", "files_path",
                         "submit_path", "finish_path", "download_template")
                        if arena_cfg.get(k)}
        proxy = os.environ.get("ARENA_PROXY") or arena_cfg.get("proxy") or ""
        try:
            harness = ArenaHarnessAdapter(base_url, token, proxy=proxy,
                                          auto_start=bool(arena_cfg.get("auto_start", True)),
                                          auto_finish=bool(arena_cfg.get("auto_finish", False)),
                                          client_timeout=float(arena_cfg.get("client_timeout_seconds") or 30),
                                          **arena_kwargs)
        except ArenaProtocolError as exc:
            raise SystemExit("arena harness init failed: {}".format(exc))
    else:
        harness = MockHarnessAdapter(default_practice_challenges())
    state = StateStore(board, evidence, os.path.join(os.path.dirname(db_path), "workspaces"))
    tool_registry = None
    if bool((cfg.get("model") or {}).get("tools_enabled", True)):
        tool_registry = ToolRegistry(policy, evidence_store=evidence, work_root=str(state.root),
                                     state_store=state, environment="offline-image")
        register_default_tools(tool_registry)
    budget = cfg.get("budget") or {}
    controller = cfg.get("controller") or {}
    step_policy = StepPolicy(tuple(controller.get("thresholds", (30, 50, 70, 100))))
    state.pin_policy({"thresholds": list(step_policy.thresholds), "model": DEFAULT_MODEL, "parallel_agents": 1})
    def control_factory(challenge_id):
        # 意图重试语义：上一意图因 stall/重复调用把会话标为 stopped 时，
        # 重试意图应复活会话（清停滞计数与重复签名），累计预算保留。
        # 预算真正耗尽（tokens/tools/时间/steps 任意为 0）时不复活，保持硬限制。
        control = SessionControl(state, challenge_id, policy=step_policy,
            max_steps=int(controller.get("max_steps", 100)),
            max_tokens=int(budget.get("max_model_tokens_per_intent") or 50000),
            max_tools=int(budget.get("max_tool_calls_per_intent")
                          or budget.get("max_tools_per_intent") or 100),
            seconds=float(budget.get("per_challenge_seconds") or 900),
            repeat_limit=int(budget.get("repeat_limit") or 3),
            stall_steps=int(budget.get("stall_steps") or 20),
            absolute_deadline=getattr(control_factory, "deadline", None))
        try:
            _, st = state.load(challenge_id)
            left = control.remaining()
            if (st.get("status") == "stopped"
                    and left["tokens"] > 0 and left["tools"] > 0
                    and left["seconds"] > 0 and st.get("steps", 0) < control.max_steps):
                def _revive(s):
                    s["status"] = "ready"
                    s["call_signatures"] = {}
                    s["last_progress_step"] = s["steps"]
                state.patch(challenge_id, _revive)
        except Exception:
            pass
        return control
    gate = FlagGate(state)
    gateway = SubmitGateway(state, harness, policy)

    conc = cfg.get("concurrency") or {}
    runtime_cfg = cfg.get("runtime") or {}
    scheduler = Scheduler(board,
                          max_project_workers=int(conc.get("max_project_workers") or 1),
                          max_running_projects=int(conc.get("max_running_projects") or 1),
                          state_store=state, step_policy=step_policy,
                          retry_backoff_seconds=float(runtime_cfg.get("retry_backoff_seconds") or 0))
    if mode == "fallback":
        scheduler.set_limits(1, 1)
    if mode == "llm":
        provider = _make_provider(cfg)
        try:
            skill_loader = SkillLoader()
        except (OSError, ValueError):
            skill_loader = None  # 技能索引损坏不能阻断解题
        worker = LlmWorker(provider, prompt_path=PROMPT_PATH,
                           state_store=state, control_factory=control_factory,
                           evidence_check=evidence.verify,
                           timeout=float((cfg.get("model") or {}).get("request_timeout_seconds") or 120),
                           max_tokens=int((cfg.get("model") or {}).get("max_tokens") or 4096),
                           skill_loader=skill_loader,
                           tool_registry=tool_registry,
                           tool_cwd=lambda cid: str(state.workspace(cid)),
                           max_tool_rounds=int((cfg.get("controller") or {}).get("max_tool_rounds") or 40))
    else:
        worker = RuleBasedWorker(harness, evidence_store=evidence,
                                 state_store=state, control_factory=control_factory)
    if claim_timeout_seconds is None:
        claim_timeout_seconds = float(budget.get("claim_timeout_seconds")
                                      or budget.get("per_intent_seconds") or 90)
    # 确定性兜底 Worker 没有新的探索信息，默认不做后续规划；只有 LLM 模式展开 Planner。
    planner_limit = int(runtime_cfg.get("max_intents_per_challenge") or (4 if mode == "llm" else 1))
    orch = Orchestrator(harness, board, scheduler, worker, evidence=evidence,
                        hot_config=hot, policy_engine=policy, db=conn,
                        state_store=state, flag_gate=gate, gateway=gateway,
                        claim_timeout_seconds=claim_timeout_seconds,
                        configured_max_workers=int(conc.get("max_workers") or 1),
                        heartbeat_path=heartbeat_path,
                        retry_backoff_seconds=float(runtime_cfg.get("retry_backoff_seconds") or 0),
                        poll_seconds=float(runtime_cfg.get("intent_poll_seconds") or 0.1),
                        max_intents_per_challenge=planner_limit,
                        reviewer_factory=(lambda cid: ProviderReviewer(provider, control_factory(cid))) if mode == "llm" else None)
    if hot is not None:
        orch.apply_config(hot.config)
    if mode == "fallback":
        scheduler.set_limits(1, 1)
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
    run.add_argument("--mode", choices=("mock", "llm", "fallback"), default="mock")
    run.add_argument("--config", default=None, help="JSON/YAML 配置；热更新按文件内容变化生效")
    run.add_argument("--report", default=None, help="把运行结果写成 JSON")
    run.add_argument("--dashboard", default=None, help="生成静态 HTML 看板")
    run.add_argument("--bench", default=None, help="压测结果 JSON，供看板展示")
    run.add_argument("--heartbeat-file", default=None, help="写入进程心跳，供外部 supervisor 判活")
    run.add_argument("--claim-timeout", type=float, default=None, help="单题硬超时秒数；0 表示关闭")
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return 2
    orch = build_runtime(args.db, args.evidence, mode=args.mode, config_path=args.config,
                         heartbeat_path=args.heartbeat_file,
                         claim_timeout_seconds=args.claim_timeout)
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
