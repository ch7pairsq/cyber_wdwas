"""命令行入口：python3 -m ctf_ext run [--mode mock|llm] [--config FILE] ...

无模型凭证时用 Mock Harness + RuleBasedWorker 跑通完整链路（附录 F-4）；
--mode llm 默认走 DeepSeek 官方 API（deepseek_official_openai 通道）；
缺少 BAIZHI_API_KEY（值 = DeepSeek 官方 API key）时拒绝调用。
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
from .runtime.control import GlobalBudget, SessionControl, StepPolicy, reset_intent_window
from .llm.provider import ProviderConfigError
from .llm.deepseek import DEFAULT_BASE_URL, DEFAULT_MODEL, PROVIDER_NAME, DeepSeekProvider
from .llm.openai_compat import (DEFAULT_BASE_URL as OFFICIAL_BASE_URL,
                                DEFAULT_MODEL as OFFICIAL_MODEL,
                                PROVIDER_NAME as OFFICIAL_PROVIDER_NAME,
                                DeepSeekOfficialProvider)
from .llm import keycache
from .runtime.dashboard import write_dashboard
from .runtime.llm_worker import LlmWorker
from .runtime.orchestrator import Orchestrator
from .runtime.scheduler import Scheduler
from .runtime.worker import RuleBasedWorker
from .skills.loader import SkillLoader
from .store import solutions as solution_store
from .store.blackboard import Blackboard
from .store.evidence import EvidenceStore

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "worker.md")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATHS = (
    os.path.join(REPO_ROOT, "config", "base.yaml"),
    os.path.join(REPO_ROOT, "config", "practice.yaml"),
)


def _make_provider(cfg, model_override: str = None):
    """按配置构造模型 Provider（公司网关或官方备选通道）。

    负责 Key 解析与缓存同步；model_override 仅替换模型 ID（双模型换挡的
    Pro 实例与主模型共用同一网关与凭据）。provider 不在白名单时直接退出。
    """
    model = cfg.get("model") or {}
    provider_name = model.get("provider", PROVIDER_NAME)
    if provider_name == OFFICIAL_PROVIDER_NAME:
        builder, base_default, model_default = (DeepSeekOfficialProvider,
                                                OFFICIAL_BASE_URL, OFFICIAL_MODEL)
    elif provider_name == PROVIDER_NAME:
        builder, base_default, model_default = DeepSeekProvider, DEFAULT_BASE_URL, DEFAULT_MODEL
    else:
        raise SystemExit("unknown model.provider; use baizhi_anthropic or deepseek_official_openai")
    key, source = keycache.resolve_model_key(model)
    if source == "cache":
        # 环境变量缺失时从缓存恢复属于异常路径，只提示 stderr，不写进报告或日志文件。
        print("model key restored from key cache (environment injection missing)", file=sys.stderr)
    else:
        keycache.sync_key_cache(model, key)
    # model_override：双模型换挡的 Pro 实例走同一网关同一凭据，仅模型 ID 不同。
    return builder(api_key=key, base_url=model.get("base_url", base_default),
                   model=model_override or model.get("model", model_default),
                   timeout=float(model.get("request_timeout_seconds") or 120))


def _recall_solutions(board, domain: str, query: str, challenge_id: str):
    """解题思路检索：走黑板读视图（与写事务共用 RLock），共享连接读写不交叠。

    检索失败向上抛由 worker 侧 fail-open 兜底；bump 计数在 autocommit 下
    即时生效，失败只损失计数不影响注入。
    """
    with board.read_view() as db:
        return solution_store.solutions_context(db, domain, query, challenge_id=challenge_id)


def build_runtime(db_path: str, evidence_root: str, mode: str = "mock",
                  config_path: str = None, data_root: str = ".",
                  heartbeat_path: str = None, claim_timeout_seconds: float = None):
    """装配一轮完整运行时，返回编排器 Orchestrator。

    装配顺序：SQLite(WAL) → 迁移 → 热配置快照 → 证据库 → 策略引擎 → 黑板 →
    Harness（mock/arena，official 保持 fail-closed）→ 状态仓 + 工具注册表 →
    预算/控制器 → FlagGate/提交网关 → 调度器 → Worker（mock 规则式或 llm）→
    Orchestrator（含双模型换挡与审阅者工厂）。所有组件共享同一 sqlite 连接。
    """
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    # 显式事务模式：黑板用 BEGIN IMMEDIATE 控制边界，证据登记不能留下隐式事务。
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 并发执行前提：WAL 让读不阻塞写；busy_timeout 让跨连接写（如看板进程）
    # 等待持锁者而不是立即报 busy。黑板内所有读写已用同一把 RLock 串行化。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
        from .harness.arena import (ArenaHarnessAdapter, ArenaProtocolError,
                                    resolve_arena_base_url, resolve_arena_token,
                                    setup_platform_network_env)
        arena_cfg = (cfg.get("harness") or {}).get("arena") or {}
        base_url, base_source = resolve_arena_base_url(arena_cfg.get("base_url") or "")
        token, token_source = resolve_arena_token()
        if not base_url or not token:
            raise SystemExit(
                "arena harness requires a reachable arena address and solver token "
                "(base from: {}, token from: {}; expected ARENA_BASE_URL/ARENA_SOLVER_TOKEN "
                "or platform ARENA_GATEWAY_URL / token files / k8s service discovery)".format(
                    base_source, token_source))
        arena_kwargs = {k: arena_cfg[k] for k in
                        ("help_path", "list_path", "start_path", "files_path",
                         "submit_path", "finish_path", "download_template")
                        if arena_cfg.get(k)}
        # startup 阶段对平台 5xx 的退避重试：默认 20 次 × 30s，可在配置覆盖。
        arena_kwargs["startup_retry_attempts"] = int(
            arena_cfg.get("startup_retry_attempts") or 20)
        arena_kwargs["startup_retry_seconds"] = float(
            arena_cfg.get("startup_retry_seconds") or 30)
        proxy = os.environ.get("ARENA_PROXY") or arena_cfg.get("proxy") or ""
        try:
            harness = ArenaHarnessAdapter(base_url, token, proxy=proxy,
                                          base_source=base_source,
                                          token_source=token_source,
                                          auto_start=bool(arena_cfg.get("auto_start", True)),
                                          auto_finish=bool(arena_cfg.get("auto_finish", False)),
                                          client_timeout=float(arena_cfg.get("client_timeout_seconds") or 30),
                                          env_policy=str(arena_cfg.get("env_policy") or "prewarm"),
                                          **arena_kwargs)
        except ArenaProtocolError as exc:
            raise SystemExit("arena harness init failed: {}".format(exc))
        # 适配器构造后再导出隧道变量：客户端保持"直连优先+回退"阶梯，
        # 网关工具与模型调用（urllib 读环境）则拿到平台隧道。
        tunnel_actions = setup_platform_network_env()
        if tunnel_actions:
            print("[arena] platform tunnel env exported: {}".format(
                "; ".join(tunnel_actions)), file=sys.stderr, flush=True)
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
    model_cfg = cfg.get("model") or {}
    pinned_model = (model_cfg.get("model")
                    or (OFFICIAL_MODEL if model_cfg.get("provider") == OFFICIAL_PROVIDER_NAME
                        else DEFAULT_MODEL))
    state.pin_policy({"thresholds": list(step_policy.thresholds), "model": pinned_model,
                      "parallel_agents": 1})
    # 运行级全局 token 账本（0/未配置 = 不设上限）；编排层据此停止领取新意图。
    global_budget = GlobalBudget(state, int(budget.get("max_model_tokens_total") or 0))
    def control_factory(challenge_id):
        """为单个题目构造会话控制器：意图边界重置步数窗口，终局加时提升预算。

        预算真正耗尽（tokens/tools/时间任一为 0）时不复活，保持硬限制。
        """
        # 意图边界 = 新步数窗口：route 阶段/停滞/重复判定按意图独立，
        # 跨意图保留累计预算与证据教训（详见 reset_intent_window）。
        # 预算真正耗尽（tokens/tools/时间 任意为 0）时不复活，保持硬限制。
        max_tokens = int(budget.get("max_model_tokens_per_intent") or 50000)
        max_tools = int(budget.get("max_tool_calls_per_intent")
                        or budget.get("max_tools_per_intent") or 100)
        seconds = float(budget.get("per_challenge_seconds") or 900)
        # 终局加时（endgame）：编排层在剩余时间充裕时给预算耗尽的题授权一次
        # 追加额度——提高上限而非清零计数，累计账本保持诚实，全局预算仍兜底。
        boost = float(getattr(control_factory, "endgame", {}).get(challenge_id) or 0)
        if boost > 0:
            max_tokens = int(max_tokens * (1 + boost))
            max_tools = int(max_tools * (1 + boost))
            deadline = getattr(control_factory, "deadline", None)
            if deadline:
                try:
                    _, _st = state.load(challenge_id)
                    started_at = float(_st["budget"]["started_at"])
                    # 会话有效期延到运行截止：remaining() 取 min(started_at+seconds, absolute_deadline)
                    seconds = max(seconds, deadline - started_at)
                except Exception:
                    pass
        control = SessionControl(state, challenge_id, policy=step_policy,
            max_steps=int(controller.get("max_steps", 100)),
            max_tokens=max_tokens,
            max_tools=max_tools,
            seconds=seconds,
            repeat_limit=int(budget.get("repeat_limit") or 3),
            stall_steps=int(budget.get("stall_steps") or 20),
            absolute_deadline=getattr(control_factory, "deadline", None),
            global_budget=global_budget)
        try:
            _, st = state.load(challenge_id)
            left = control.remaining()
            # 预算仍在 → 每个意图开独立步数窗口（route/停滞/重复按意图重计，
            # stopped 会话复活）；预算已耗尽 → 只重置计数、保留 stopped 硬停止。
            budget_ok = (left["tokens"] > 0 and left["tools"] > 0
                         and left["seconds"] > 0 and st.get("steps", 0) < control.max_steps)
            reset_intent_window(state, challenge_id, revive_stopped=budget_ok)
            board.append_event("intent_window_reset", challenge_id,
                               {"prior_steps": st.get("steps", 0), "revived": budget_ok})
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
        # 双模型换挡（借鉴 Tsecbench 冠军方案）：pro_model 非空时构造 Pro 实例
        # （同一端点同一凭据），stage>=pro_from_stage 的换路径回合与提交前评审
        # 用 Pro；构造失败或未配置一律回退单模型，不影响可用性。
        model_cfg = cfg.get("model") or {}
        pro_model_id = str(model_cfg.get("pro_model") or "").strip()
        pro_provider = None
        if pro_model_id and pro_model_id != model_cfg.get("model"):
            try:
                pro_provider = _make_provider(cfg, model_override=pro_model_id)
            except (SystemExit, ProviderConfigError):
                pro_provider = None  # 凭据缺失/模型白名单拒绝等：回退单模型，不阻断
        try:
            skill_loader = SkillLoader()
        except (OSError, ValueError):
            skill_loader = None  # 技能索引损坏不能阻断解题
        skills_cfg = cfg.get("skills") or {}
        if not isinstance(skills_cfg, dict):
            skills_cfg = {}  # 非法配置回退默认，不能阻断启动
        try:
            skills_index_max_bytes = int(skills_cfg.get("index_max_bytes") or 600)
        except (TypeError, ValueError):
            skills_index_max_bytes = 600
        # 解题思路记忆：检索必须走黑板读视图（与写事务共用 RLock，
        # 共享 sqlite 连接上读写不得交叠）；worker 侧再 fail-open。
        solutions_recall = (lambda domain, query, cid:
                            _recall_solutions(board, domain, query, cid))
        worker = LlmWorker(provider, prompt_path=PROMPT_PATH,
                           state_store=state, control_factory=control_factory,
                           evidence_check=evidence.verify,
                           timeout=float((cfg.get("model") or {}).get("request_timeout_seconds") or 120),
                           max_tokens=int((cfg.get("model") or {}).get("max_tokens") or 4096),
                           skill_loader=skill_loader,
                           skills_mode=str(skills_cfg.get("mode") or "preload"),
                           skills_index_max_bytes=skills_index_max_bytes,
                           solutions_recall=solutions_recall,
                           tool_registry=tool_registry,
                           tool_cwd=lambda cid: str(state.workspace(cid)),
                           max_tool_rounds=int((cfg.get("controller") or {}).get("max_tool_rounds") or 40),
                           global_budget=global_budget,
                           pro_provider=pro_provider,
                           pro_max_calls=int(budget.get("pro_max_calls_per_challenge") or 0),
                           pro_from_stage=int(model_cfg.get("pro_from_stage") or 2),
                           thinking_by_stage=tuple(model_cfg.get("thinking_by_stage") or ()))
    else:
        worker = RuleBasedWorker(harness, evidence_store=evidence,
                                 state_store=state, control_factory=control_factory)
    if claim_timeout_seconds is None:
        claim_timeout_seconds = float(budget.get("claim_timeout_seconds")
                                      or budget.get("per_intent_seconds") or 90)
    # 确定性兜底 Worker 没有新的探索信息，默认不做后续规划；只有 LLM 模式展开 Planner。
    planner_limit = int(runtime_cfg.get("max_intents_per_challenge") or (4 if mode == "llm" else 1))
    # best-effort 提交：gate 拒绝/挂起但格式合法的候选仍入队，平台真值仲裁
    # （仅 official 配置开启；错提交无计分惩罚，gate 拒绝 ≠ 平台拒绝）。
    best_effort_submit = bool((cfg.get("submission") or {}).get("best_effort_on_reject") or False)
    orch = Orchestrator(harness, board, scheduler, worker, evidence=evidence,
                        hot_config=hot, policy_engine=policy, db=conn,
                        state_store=state, flag_gate=gate, gateway=gateway,
                        claim_timeout_seconds=claim_timeout_seconds,
                        configured_max_workers=int(conc.get("max_workers") or 1),
                        heartbeat_path=heartbeat_path,
                        retry_backoff_seconds=float(runtime_cfg.get("retry_backoff_seconds") or 0),
                        poll_seconds=float(runtime_cfg.get("intent_poll_seconds") or 0.1),
                        max_intents_per_challenge=planner_limit,
                        best_effort_submit=best_effort_submit,
                        parallel_agents=int((cfg.get("controller") or {}).get("parallel_agents") or 1),
                        endgame_token_bonus=float(budget.get("endgame_token_bonus") or 0),
                        reviewer_factory=(lambda cid: ProviderReviewer(pro_provider or provider, control_factory(cid))) if mode == "llm" else None)
    if hot is not None:
        orch.apply_config(hot.config)
    if mode == "fallback":
        scheduler.set_limits(1, 1)
    # 一次打印全部生效启动参数（脱敏：无凭据），平台日志即完整排查材料。
    print("[run] config: protocol={} mode={} max_workers={} "
          "parallel={} claim_timeout={}s backoff={}s poll={}s intents/chal={} best_effort={}".format(
              protocol, mode, int(conc.get("max_workers") or 1),
              int((cfg.get("controller") or {}).get("parallel_agents") or 1),
              claim_timeout_seconds,
              float(runtime_cfg.get("retry_backoff_seconds") or 0),
              float(runtime_cfg.get("intent_poll_seconds") or 0.1),
              planner_limit, best_effort_submit), file=sys.stderr, flush=True)
    print("[run] model: provider={} model={} pro_model={} key_env={} "
          "max_tokens={} request_timeout={}s tools={} tool_rounds={} skills_mode={}".format(
              model_cfg.get("provider"), pinned_model,
              (pro_model_id or "-") if mode == "llm" else "-",
              model_cfg.get("api_key_env") or "BAIZHI_API_KEY",
              int(model_cfg.get("max_tokens") or 4096),
              float(model_cfg.get("request_timeout_seconds") or 120),
              bool(model_cfg.get("tools_enabled", True)),
              int((cfg.get("controller") or {}).get("max_tool_rounds") or 40),
              str((cfg.get("skills") or {}).get("mode") or "preload")),
          file=sys.stderr, flush=True)
    print("[run] budget: per_intent_tokens={} per_intent_tools={} "
          "per_intent_seconds={} global_tokens={} pro_calls/chal={} "
          "repeat_limit={} stall_steps={} endgame_bonus={}".format(
              int(budget.get("max_model_tokens_per_intent") or 50000),
              int(budget.get("max_tool_calls_per_intent")
                  or budget.get("max_tools_per_intent") or 100),
              float(budget.get("per_challenge_seconds") or 900),
              int(budget.get("max_model_tokens_total") or 0),
              int(budget.get("pro_max_calls_per_challenge") or 0),
              int(budget.get("repeat_limit") or 3),
              int(budget.get("stall_steps") or 20),
              float(budget.get("endgame_token_bonus") or 0)),
          file=sys.stderr, flush=True)
    return orch


def _emit(out, report):
    """把运行结果 JSON 写到 --report 指定路径；未指定时不写文件。"""
    if report:
        os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
        with open(report, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)


def main(argv=None) -> int:
    """CLI 主入口：解析参数 → build_runtime → 编排一轮 → 报告/看板/退出码。

    退出码约定：0=有提交被受理；3=未解决；2=用法错误。正常结束清理 Key 缓存。
    """
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
    # 本轮正常结束即清理 Key 缓存（用户 2026-11-18 决定"解题后清理"）；
    # 异常中断时缓存保留，作为下次启动的兜底，并在有环境变量时被覆盖。
    keycache.clear_key_cache()
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
