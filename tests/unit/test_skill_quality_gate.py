"""质量闸：四段结构、工具白名单、字面 flag 与超长。"""
import pathlib
import tempfile

from scripts.check_skills import _load_tools, check_card


def _card(tool="python_exec", body="flag{...}"):
    return (
        "# demo\n\n## 触发\n- 可观察条件\n\n## 最小动作\n"
        "- tool=" + tool + "\n\n## 命中判据\n- stdout 出现 " + body + "\n\n"
        "## 踩坑\n- 不要盲试\n"
    )


def _check(tmp, text):
    root = pathlib.Path(tmp)
    (root / "ctf_ext" / "skills" / "sections").mkdir(parents=True)
    (root / "ctf_ext" / "skills" / "sections" / "demo.md").write_text(text, encoding="utf-8")
    return check_card({"name": "demo", "domain": "crypto", "file": "demo.md"},
                      text, _load_tools(), root)


def test_valid_card_passes():
    with tempfile.TemporaryDirectory() as tmp:
        result = _check(tmp, _card())
        assert result["errors"] == []


def test_unknown_tool_and_missing_heading_fail():
    with tempfile.TemporaryDirectory() as tmp:
        result = _check(tmp, "# demo\n\n## 触发\n- x\n\n## 最小动作\n- tool=not_a_tool\n")
        assert any("unknown tool=not_a_tool" in item for item in result["errors"])
        assert any("missing heading" in item for item in result["errors"])


def test_literal_flag_and_raw_command_fail():
    with tempfile.TemporaryDirectory() as tmp:
        result = _check(tmp, _card(body="flag{real_value_123}").replace("不要盲试", "unzip"))
        assert any("literal flag-like" in item for item in result["errors"])
        assert any("raw command" in item for item in result["errors"])


def test_prerequisite_must_be_a_registered_tool():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "ctf_ext" / "skills" / "sections").mkdir(parents=True)
        text = _card()
        (root / "ctf_ext" / "skills" / "sections" / "demo.md").write_text(text, encoding="utf-8")
        result = check_card({"name": "demo", "domain": "crypto", "file": "demo.md",
                             "prerequisites": ["7z"]}, text, _load_tools(), root)
        assert any("not a registered tool" in item for item in result["errors"])
