"""离线技能选择评测。"""
import contextlib
import io
import pathlib
import tempfile

from ctf_ext.skills.loader import SkillLoader
from scripts.eval_skills import evaluate, load_cases, main


ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_fixture_cases_hit_without_forbidden_selection():
    cases = load_cases(ROOT / "tests" / "fixtures" / "skill_selection_cases.jsonl")
    report = evaluate(SkillLoader(), cases)
    assert report["ok"] is True
    assert report["cases"] == len(cases)
    assert report["top1"] == len(cases)
    assert report["top2"] == len(cases)
    assert report["forbidden"] == 0


class _FakeLoader:
    def select(self, domain, query, limit=2):
        return [{"name": "bad.skill"}, {"name": "other.skill"}][:limit]


def test_forbidden_hit_fails_and_is_counted():
    case = {"id": "bad", "domain": "crypto", "query": "x",
            "expect": ["good.skill"], "forbid": ["bad.skill"]}
    report = evaluate(_FakeLoader(), [case])
    assert report["ok"] is False
    assert report["top1"] == 0
    assert report["top2"] == 0
    assert report["forbidden"] == 1
    assert report["failed"][0]["forbidden_hits"] == ["bad.skill"]


def test_strict_cli_returns_nonzero_for_miss():
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "cases.jsonl"
        path.write_text('{"id":"miss","domain":"crypto","query":"x",'
                        '"expect":["not.present"]}\n', encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["--cases", str(path), "--strict"]) == 1
            assert main(["--cases", str(path)]) == 0
