"""步数止损、同模型换策略与持久化预算；默认阈值仅为未校准候选。"""
from __future__ import annotations

import hashlib
import json
import time


class BudgetExceeded(Exception):
    pass


class LoopDetected(Exception):
    pass


class StepPolicy:
    def __init__(self, thresholds=(30, 50, 70, 100)):
        if len(thresholds) != 4 or any(type(x) is not int or x <= 0 for x in thresholds) or sorted(set(thresholds)) != list(thresholds):
            raise ValueError('four increasing positive thresholds required')
        self.thresholds = tuple(thresholds)

    def route(self, steps):
        stage = sum(steps >= t for t in self.thresholds)
        return {'stage': stage, 'model': 'deepseek-flash',
                'strategy': ('initial', 'hinted', 'alternate', 'deep_water', 'heartbeat')[stage],
                'active_slots': (1, 1, 1, 1, 0)[stage],
                'priority_factor': (1, .7, .5, .1, 0)[stage],
                'hint': ('', '复用证据和已试路径，避免重复', '更换分析路径，不更换公司模型',
                         '仅保留低资源会话', '停止新增调用')[stage]}


class SessionControl:
    def __init__(self, store, challenge_id, *, max_steps=100, max_tokens=50_000,
                 max_tools=100, seconds=900, absolute_deadline=None, repeat_limit=3,
                 stall_steps=20, policy=None, clock=time.time):
        self.store, self.challenge_id = store, challenge_id
        self.max_steps, self.max_tokens, self.max_tools = max_steps, max_tokens, max_tools
        self.seconds, self.absolute_deadline = seconds, absolute_deadline
        self.repeat_limit, self.stall_steps = repeat_limit, stall_steps
        self.policy, self.clock = policy or StepPolicy(), clock
        if any(x <= 0 for x in (max_steps, max_tokens, max_tools, seconds, repeat_limit, stall_steps)):
            raise ValueError('positive controller limits required')
        revision, state = self.store.load(challenge_id)
        if revision == 0:
            self.store.save(challenge_id, revision, state)

    def remaining(self):
        _, state = self.store.load(self.challenge_id)
        deadline = state['budget']['started_at'] + self.seconds
        if self.absolute_deadline is not None:
            deadline = min(deadline, self.absolute_deadline)
        return {'seconds': max(0, deadline - self.clock()),
                'tokens': max(0, self.max_tokens - state['budget']['model_tokens']),
                'tools': max(0, self.max_tools - state['budget']['tool_calls'])}

    def reserve(self, tool, version, argv, input_hash, *, is_tool=True):
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
        if type(tokens) is not int or tokens < 0:
            raise ValueError('invalid measured token usage')
        self.store.patch(self.challenge_id, lambda s: s['budget'].update(model_tokens=s['budget']['model_tokens'] + tokens))
        _, state = self.store.load(self.challenge_id)
        if state['budget']['model_tokens'] >= self.max_tokens * .8:
            self.store.compact(self.challenge_id)
        if state['budget']['model_tokens'] > self.max_tokens:
            self.store.patch(self.challenge_id, lambda s: s.update(status='stopped'))
            raise BudgetExceeded('provider usage exceeded reserved output; no more calls allowed')

    def progress(self, evidence_id, path):
        def update(s):
            if evidence_id not in s['evidence_ids']:
                s['evidence_ids'].append(evidence_id)
                s['last_progress_step'] = s['steps']
            s['tried_paths'].append(path)
        self.store.patch(self.challenge_id, update)
