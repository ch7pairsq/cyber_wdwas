"""运行时指标（§18.1）：只做进程内计数，供看板/日志消费，不参与判定。"""
from __future__ import annotations

import threading
import time

CATEGORIES = ("intents", "model", "tools", "knowledge", "submissions", "resources")


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = {}
        self._started = time.time()

    def incr(self, name: str, value: int = 1) -> int:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value
            return self._counters[name]

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self._counters.setdefault(name + ".sum", 0)
            self._counters.setdefault(name + ".count", 0)
            self._counters[name + ".sum"] += value
            self._counters[name + ".count"] += 1

    def snapshot(self) -> dict:
        with self._lock:
            data = dict(self._counters)
        data["uptime_seconds"] = round(time.time() - self._started, 3)
        return data


def framework_metrics(db, task_count):
    """从持久事实复盘；单条记录损坏时跳过，不能让整张看板指标失败。"""
    import json

    def _load_object(raw):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    decision_rows = [row[0] for row in db.execute('SELECT payload FROM gate_decisions')]
    decisions = [obj for obj in (_load_object(raw) for raw in decision_rows) if obj is not None]
    attempts = {'grounding': 0, 'negation': 0, 'interrogation': 0}
    vetoes = dict.fromkeys(attempts, 0)
    unreadable = len(decision_rows) - len(decisions)
    for decision in decisions:
        gates = decision.get('gates')
        if not isinstance(gates, dict):
            unreadable += 1
            continue
        attempts['grounding'] += 1
        vetoes['grounding'] += int(not gates.get('grounding'))
        if 'negation_votes' in gates or gates.get('negation'):
            attempts['negation'] += 1
            votes = gates.get('negation_votes', [])
            if isinstance(votes, list):
                vetoes['negation'] += int(sum(bool(vote) for vote in votes) >= 2)
            else:
                vetoes['negation'] += 1
        if 'interrogation_answers' in gates or gates.get('interrogation'):
            attempts['interrogation'] += 1
            answers = gates.get('interrogation_answers', {'code': True})
            vetoes['interrogation'] += int(not all(answers.values())) if isinstance(answers, dict) else 1
    rows = list(db.execute('SELECT status,sent_at,updated_at FROM submissions'))
    sent = [row for row in rows if row['sent_at'] is not None]
    accepted = [row for row in sent if row['status'] == 'ACCEPTED']
    start = db.execute("SELECT MIN(created_at) FROM events WHERE event_type='run_started'").fetchone()[0]
    session_rows = [row[0] for row in db.execute('SELECT payload FROM sessions')]
    sessions = []
    for raw in session_rows:
        session = _load_object(raw)
        if session is None or not isinstance(session.get('steps'), int):
            unreadable += 1
            continue
        sessions.append(session)
    return {
        'first_kill_rate': None,
        'first_kill_note': '官方回执未提供名次，不能将 Mock 接受率当作首杀率',
        'accepted_first_30m_conversion': (sum(row['updated_at'] <= start + 1800 for row in accepted) / task_count) if start and task_count else None,
        'average_steps': sum(s['steps'] for s in sessions) / len(sessions) if sessions else 0,
        'gate_veto_rates': {key: vetoes[key] / count if count else None for key, count in attempts.items()},
        'submission_success_rate': len(accepted) / len(sent) if sent else None,
        'submission_average_latency_seconds': sum(row['updated_at'] - row['sent_at'] for row in accepted) / len(accepted) if accepted else None,
        'resume_events': db.execute("SELECT COUNT(*) FROM events WHERE event_type='session_resumed'").fetchone()[0],
        'loop_interventions': db.execute("SELECT COUNT(*) FROM events WHERE event_type='loop_intervention'").fetchone()[0],
        'long_sessions': [{'challenge_id': s.get('challenge_id', ''), 'steps': s['steps'],
                           'status': s.get('status', '')} for s in sessions if s['steps'] >= 30],
        'unreadable_records': unreadable,
    }
