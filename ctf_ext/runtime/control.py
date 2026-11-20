"""步数止损、同模型换策略与持久化预算；默认阈值仅为未校准候选。"""
from __future__ import annotations

import hashlib
import json
import time


class BudgetExceeded(Exception):
    """会话/全局预算耗尽；调用方应停止模型调用并按可重试失败处理。"""
    pass


class LoopDetected(Exception):
    """循环检测命中：同一签名重复超过 repeat_limit 或长期无新证据。"""
    pass


class StepPolicy:
    """按步数窗口分 5 级路线策略：模型/策略名/槽位/优先级系数/提示语。"""
    def __init__(self, thresholds=(30, 50, 70, 100)):
        """thresholds 为 4 个递增正整数，划分 stage 0..4。"""
        if len(thresholds) != 4 or any(type(x) is not int or x <= 0 for x in thresholds) or sorted(set(thresholds)) != list(thresholds):
            raise ValueError('four increasing positive thresholds required')
        self.thresholds = tuple(thresholds)

    def route(self, steps):
        """步数 → 路线决策：阶段越高越保守，stage4（heartbeat）禁新增调用。"""
        stage = sum(steps >= t for t in self.thresholds)
        return {'stage': stage, 'model': 'deepseek-flash',
                'strategy': ('initial', 'hinted', 'alternate', 'deep_water', 'heartbeat')[stage],
                'active_slots': (1, 1, 1, 1, 0)[stage],
                'priority_factor': (1, .7, .5, .1, 0)[stage],
                'hint': ('', '复用证据和已试路径，避免重复', '更换分析路径，不更换公司模型',
                         '仅保留低资源会话', '停止新增调用')[stage]}


class GlobalBudget:
    """跨题目共享的运行级 token 账本（controller_settings 持久化，重启可恢复）。

    max_total_tokens=0 表示不设全局上限；用量仍持久化。
    到达上限即 exhausted，超限时 charge 先落账再抛 BudgetExceeded。
    此处是实耗账本，不是并发调用前的原子额度预留。
    """

    KEY = "global_budget"

    def __init__(self, store, max_total_tokens=0, clock=time.time):
        """max_total_tokens=0 表示不设上限；store 为 StateStore（账本落 controller_settings）。"""
        self.store = store
        if type(max_total_tokens) is not int or max_total_tokens < 0:
            raise ValueError("invalid global token limit")
        self.max_total_tokens = max_total_tokens
        self.clock = clock
        self._spent = self._load()

    def _read_spent(self, db):
        row = db.execute(
            "SELECT payload FROM controller_settings WHERE key=?",
            (self.KEY,)).fetchone()
        if row is None:
            return 0
        try:
            spent = json.loads(row["payload"])["model_tokens"]
        except (ValueError, TypeError, KeyError):
            raise ValueError("invalid persisted global token ledger") from None
        if type(spent) is not int or spent < 0:
            raise ValueError("invalid persisted global token ledger")
        return spent

    def _load(self):
        with self.store.board.read_view() as db:
            return self._read_spent(db)

    @property
    def spent(self):
        """全局已耗 token 数（每次读取从持久化账本刷新）。"""
        self._spent = self._load()
        return self._spent

    @property
    def exhausted(self):
        """全局账本是否已耗尽（max_total_tokens<=0 表示不限）。"""
        return self.max_total_tokens > 0 and self.spent >= self.max_total_tokens

    def remaining(self):
        """全局剩余 token；未配置上限时返回 inf。"""
        if self.max_total_tokens <= 0:
            return float("inf")
        return max(0, self.max_total_tokens - self.spent)

    def charge(self, tokens):
        """实耗记账：事务内读改写累加；超限先落账再抛 BudgetExceeded。"""
        if type(tokens) is not int or tokens < 0:
            raise ValueError("invalid measured token usage")
        # 同一事务读取并累加，多个控制器/连接不能用旧内存快照覆盖账本。
        with self.store.board.transaction() as db:
            spent = self._read_spent(db) + tokens
            payload = json.dumps({"model_tokens": spent,
                                  "max": self.max_total_tokens}, sort_keys=True)
            db.execute(
                "INSERT INTO controller_settings(key,payload) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (self.KEY, payload))
        self._spent = spent
        if self.max_total_tokens > 0 and spent > self.max_total_tokens:
            raise BudgetExceeded("global run budget exhausted")


class SessionControl:
    """单题会话控制器：预算/步数/停滞/循环四重止损，全部持久化到会话状态。"""
    def __init__(self, store, challenge_id, *, max_steps=100, max_tokens=50_000,
                 max_tools=100, seconds=900, absolute_deadline=None, repeat_limit=3,
                 stall_steps=20, policy=None, clock=time.time, global_budget=None):
        """首调时初始化会话状态（revision=0 创建）；absolute_deadline 为运行级截止。"""
        self.store, self.challenge_id = store, challenge_id
        self.max_steps, self.max_tokens, self.max_tools = max_steps, max_tokens, max_tools
        self.seconds, self.absolute_deadline = seconds, absolute_deadline
        self.repeat_limit, self.stall_steps = repeat_limit, stall_steps
        self.policy, self.clock = policy or StepPolicy(), clock
        self.global_budget = global_budget
        if any(x <= 0 for x in (max_steps, max_tokens, max_tools, seconds, repeat_limit, stall_steps)):
            raise ValueError('positive controller limits required')
        revision, state = self.store.load(challenge_id)
        if revision == 0:
            self.store.save(challenge_id, revision, state)

    def remaining(self):
        """剩余预算三元组 (seconds/tokens/tools)；tokens 受全局账本下限约束。"""
        _, state = self.store.load(self.challenge_id)
        deadline = state['budget']['started_at'] + self.seconds
        if self.absolute_deadline is not None:
            deadline = min(deadline, self.absolute_deadline)
        tokens = max(0, self.max_tokens - state['budget']['model_tokens'])
        if self.global_budget is not None:
            tokens = min(tokens, self.global_budget.remaining())
        return {'seconds': max(0, deadline - self.clock()),
                'tokens': tokens,
                'tools': max(0, self.max_tools - state['budget']['tool_calls'])}

    def reserve(self, tool, version, argv, input_hash, *, is_tool=True):
        """调用前预留：步数+1、预算检查、循环检测；超限把会话置 stopped 并抛异常。

        返回调用签名（供后续审计）；重复签名/停滞触发 LoopDetected。
        """
        left = self.remaining()
        _, state = self.store.load(self.challenge_id)
        if state['status'] in ('confirmed', 'stopped'):
            raise BudgetExceeded('session closed')
        if left['seconds'] <= 0 or left['tokens'] <= 0 or (is_tool and left['tools'] <= 0) or state['steps'] >= self.max_steps:
            self.store.patch(self.challenge_id, lambda s: s.update(status='stopped'))
            raise BudgetExceeded('session budget exhausted')
        signature = hashlib.sha256(json.dumps([tool, version, list(argv), input_hash], sort_keys=True).encode()).hexdigest()
        old_stage = state['route']['stage']
        def count(s):
            """预留事务体：步数/工具数累加、签名计数、换挡路由与停滞判定。"""
            s['steps'] += 1
            s['budget']['tool_calls'] += int(is_tool)
            s['call_signatures'][signature] = s['call_signatures'].get(signature, 0) + 1
            s['route'] = {k: v for k, v in self.policy.route(s['steps']).items() if k in ('stage', 'model', 'strategy')}
            if s['call_signatures'][signature] >= self.repeat_limit or s['steps'] - s['last_progress_step'] >= self.stall_steps:
                s['status'] = 'stopped'
        state = self.store.patch(self.challenge_id, count)
        route = self.policy.route(state['steps'])
        if route['stage'] != old_stage:
            self.store.board.append_event('route_changed', self.challenge_id, {**route, 'steps': state['steps']})
        if route['active_slots'] == 0:
            self.store.patch(self.challenge_id, lambda s: s.update(status='stopped'))
            raise BudgetExceeded('heartbeat stage does not permit new calls')
        if state['status'] == 'stopped':
            self.store.board.append_event('loop_intervention', self.challenge_id, {'signature': signature, 'steps': state['steps']})
            raise LoopDetected('repeated call or no verified progress')
        return signature

    def charge_tokens(self, tokens):
        """模型实耗记账：先扣全局账本再扣单题；80% 触发上下文压缩，超限停会话。"""
        if type(tokens) is not int or tokens < 0:
            raise ValueError('invalid measured token usage')
        if self.global_budget is not None:
            # 全局账本先扣：超限直接抛，不再消耗单题额度。
            self.global_budget.charge(tokens)
        self.store.patch(self.challenge_id, lambda s: s['budget'].update(model_tokens=s['budget']['model_tokens'] + tokens))
        _, state = self.store.load(self.challenge_id)
        if state['budget']['model_tokens'] >= self.max_tokens * .8:
            self.store.compact(self.challenge_id)
        if state['budget']['model_tokens'] > self.max_tokens:
            self.store.patch(self.challenge_id, lambda s: s.update(status='stopped'))
            raise BudgetExceeded('provider usage exceeded reserved output; no more calls allowed')

    def progress(self, evidence_id, path):
        """记录新证据并推进停滞基线（last_progress_step），否则停滞检测会误伤。"""
        def update(s):
            """patch 事务体：登记证据 id 与线索路径，刷新进度步数。"""
            if evidence_id not in s['evidence_ids']:
                s['evidence_ids'].append(evidence_id)
                s['last_progress_step'] = s['steps']
            s['tried_paths'].append(path)
        self.store.patch(self.challenge_id, update)


def reset_intent_window(store, challenge_id, *, revive_stopped=True):
    """意图边界开启独立步数窗口：路线阶段/停滞/重复判定按意图重新计。

    跨意图保留的是"账户"：累计预算（model_tokens/tool_calls）、证据、
    教训、handoff。步数窗口只服务当前意图——不重置的话，先到的意图会把
    StepPolicy 推到 heartbeat 阶段（禁新调用），后续意图开局即残废。
    revive_stopped=False 时保留 stopped 状态（预算耗尽等硬停止不被绕过）。
    注意：同一题的意图并发执行需要调度器先行隔离（max_project_workers）。
    """
    def _window(s):
        s['steps'] = 0
        s['last_progress_step'] = 0
        s['call_signatures'] = {}
        if revive_stopped and s.get('status') == 'stopped':
            s['status'] = 'ready'
    return store.patch(challenge_id, _window)
