"""Worker 上下文：技能注入、教训交接、压缩续跑与降级。"""
import json
import tempfile
from pathlib import Path

from tests.framework_support import FLAG, fixture
from ctf_ext.domain.models import Challenge
from ctf_ext.llm.provider import ProviderResponse, ProviderTimeout
from ctf_ext.runtime.llm_worker import LlmWorker
from ctf_ext.skills.loader import SkillLoader
from ctf_ext.store.blackboard import IntentDraft


class FakeProvider:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _payload(sha):
    return {
        "status": "completed", "summary": ["附件含 flag"],
        "facts": [{"statement": "解码得到 flag", "confidence": 0.9, "evidence_ids": [sha]}],
        "attempt": {"strategy_key": "crypto.rsa.small_e", "preconditions": ["附件可读"],
                    "actions": ["小指数"], "outcome": "success", "failure_reason": "",
                    "ruled_out": [], "next_best_action": "提交"},
        "candidate_answer": FLAG,
        "resource_usage": {"wall_seconds": 0, "model_tokens": 0, "tool_calls": 0},
    }


def _resp(payload, tokens=10):
    return ProviderResponse(text=json.dumps(payload), usage={"total_tokens": tokens})


def _skill_loader(tmp):
    root = Path(tmp)
    (root / "sections").mkdir()
    (root / "index.json").write_text(json.dumps([{
        "name": "crypto.rsa.small_e", "domain": "crypto", "prerequisites": ["python3"],
        "tokens_hint": 10, "risk": "compute", "file": "crypto.rsa.small_e.md"}]), encoding="utf-8")
    (root / "sections" / "crypto.rsa.small_e.md").write_text("用小指数攻击 RSA", encoding="utf-8")
    return SkillLoader(root)


def _setup(store):
    store.board.add_challenge(Challenge(id="c2", name="RSA 题", category="crypto",
                                      description="e=3 的 RSA"))
    sha = store.evidence.put_bytes(b"flag{demo}")
    store.board.add_intent(IntentDraft("c2", "crypto.rsa.small_e"))
    claim = store.board.claim_intent("w1")
    with store.board._write() as db:
        db.execute("INSERT INTO lessons(id,challenge_id,text,tags,source_attempt_id,superseded,created_at)"
                   " VALUES(?,?,?,?,?,0,?)",
                   ("lesson-x", "c1", "成立条件：附件可读；失败原因：e 太大；下一步：试小指数",
                    json.dumps(["domain:crypto", "outcome:error"]), "attempt-x", 1.0))
    return sha, claim


def test_worker_injects_selected_skill_and_domain_lesson_but_not_full_state():
    with fixture() as store, tempfile.TemporaryDirectory() as tmp:
        sha, claim = _setup(store)
        store.patch("c2", lambda s: s["hypotheses"].extend("h-%d" % i for i in range(200)))
        store.patch("c2", lambda s: s["evidence_ids"].extend("e-%d" % i for i in range(30)))
        provider = FakeProvider([_resp(_payload(sha))])
        worker = LlmWorker(provider, evidence_check=store.evidence.verify, state_store=store,
                           skill_loader=_skill_loader(tmp))
        result = worker.work(claim, {"name": "RSA 题", "category": "crypto",
                                    "description": "e=3 的 RSA", "handoff": store.board.handoff("c2")})
        prompt = json.dumps(provider.calls[0]["messages"], ensure_ascii=False)
        assert "内置方法参考" in prompt and "用小指数攻击 RSA" in prompt
        assert "成立条件：附件可读" in prompt
        assert "h-199" in prompt and "h-0" not in prompt
        assert "evidence_count" in prompt and "e-0" not in prompt
        assert result.resource_usage["skills_loaded"] == ["crypto.rsa.small_e"]


def test_handoff_is_bounded_and_does_not_include_other_challenge_ids():
    with fixture() as store:
        _sha, claim = _setup(store)
        handoff = {"lessons": [{"text": "L%d" % i} for i in range(20)],
                   "recent_attempts": [{"strategy_key": "x%d" % i} for i in range(20)],
                   "open_intents": [{"id": "i%d" % i} for i in range(20)]}
        provider = FakeProvider([_resp(_payload(_sha))])
        worker = LlmWorker(provider, evidence_check=store.evidence.verify, state_store=store)
        worker.work(claim, {"category": "crypto", "handoff": handoff})
        prompt = json.dumps(provider.calls[0]["messages"], ensure_ascii=False)
        assert "L0" in prompt and "L5" in prompt and "L19" not in prompt
        assert "x0" in prompt and "x4" in prompt and "x19" not in prompt
        assert "i0" in prompt and "i4" in prompt and "i19" not in prompt
        assert "challenge_id" not in prompt.split("续跑状态")[0] or "c1" not in prompt


def test_worker_redacts_flag_in_raw_handoff():
    with fixture() as store:
        _sha, claim = _setup(store)
        provider = FakeProvider([_resp(_payload(_sha))])
        worker = LlmWorker(provider, evidence_check=store.evidence.verify, state_store=store)
        worker.work(claim, {"category": "crypto", "handoff": {
            "lessons": [{"text": "raw " + FLAG}],
            "recent_attempts": [{"failure_reason": "saw " + FLAG}],
        }})
        prompt = json.dumps(provider.calls[0]["messages"], ensure_ascii=False)
        assert FLAG not in prompt and "flag{redacted}" in prompt


def test_skill_loader_failure_degrades_to_no_skills_without_blocking():
    with fixture() as store:
        _sha, claim = _setup(store)
        class Broken:
            def load_for(self, **kwargs):
                raise RuntimeError("index corrupt")
        provider = FakeProvider([_resp(_payload(_sha))])
        worker = LlmWorker(provider, evidence_check=store.evidence.verify, skill_loader=Broken())
        result = worker.work(claim, {"category": "crypto"})
        assert result.status == "completed" and result.resource_usage["skills_loaded"] == []
        assert len(provider.calls) == 1


def test_provider_timeout_still_reports_loaded_skill():
    with fixture() as store, tempfile.TemporaryDirectory() as tmp:
        _sha, claim = _setup(store)
        provider = FakeProvider([ProviderTimeout("slow")])
        worker = LlmWorker(provider, evidence_check=store.evidence.verify,
                           skill_loader=_skill_loader(tmp))
        result = worker.work(claim, {"category": "crypto"})
        assert result.status == "failed" and result.outcome == "timeout"
        assert result.resource_usage["skills_loaded"] == ["crypto.rsa.small_e"]
