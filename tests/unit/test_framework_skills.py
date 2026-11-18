"""技能索引：先索引、按领域选择、按需读取、预算与缓存。"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from ctf_ext.skills.loader import SkillLoader


def _loader(tmp, sections=None):
    root = Path(tmp)
    (root / "sections").mkdir(parents=True)
    index = [
        {"name": "crypto.rsa.small_e", "domain": "crypto", "prerequisites": ["python3"],
         "tokens_hint": 10, "risk": "compute", "file": "crypto.rsa.small_e.md"},
        {"name": "forensics.zip", "domain": "forensics", "prerequisites": ["unzip"],
         "tokens_hint": 8, "risk": "read", "file": "forensics.zip.md"},
    ]
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
    for name, text in (sections or {}).items():
        (root / "sections" / name).write_text(text, encoding="utf-8")
    return SkillLoader(root)


def test_skill_index_is_metadata_only_and_never_reads_sections():
    with tempfile.TemporaryDirectory() as tmp:
        loader = _loader(tmp, {"crypto.rsa.small_e.md": "x" * 100, "forensics.zip.md": "y"})
        assert [m["name"] for m in loader.list_index()] == ["crypto.rsa.small_e", "forensics.zip"]
        with patch("pathlib.Path.read_bytes", side_effect=AssertionError("section must not be read")):
            assert loader.select(domain="crypto", query="rsa", limit=2)[0]["name"] == "crypto.rsa.small_e"


def test_skill_selection_is_domain_scoped_and_unrelated_is_empty():
    with tempfile.TemporaryDirectory() as tmp:
        loader = _loader(tmp)
        assert loader.select(domain="crypto", query="small exponent")[0]["name"] == "crypto.rsa.small_e"
        assert loader.select(domain="forensics", query="zip")[0]["name"] == "forensics.zip"
        assert loader.select(domain="misc", query="misc triage") == []
        assert loader.select(domain="crypto", query="small exponent", limit=0) == []


def test_skill_loading_respects_per_section_and_total_budget():
    with tempfile.TemporaryDirectory() as tmp:
        loader = _loader(tmp, {"crypto.rsa.small_e.md": "a" * 20, "forensics.zip.md": "b" * 20})
        loaded, skipped = loader.load_for(domain="crypto", query="rsa", max_bytes=5)
        assert loaded == [] and skipped == [{"name": "crypto.rsa.small_e", "reason": "ValueError"}]
        loaded, skipped = loader.load_for(domain="crypto", query="rsa", max_bytes=100, total_bytes=5)
        assert loaded == [] and skipped[0]["reason"] == "total_budget"
        loaded, skipped = loader.load_for(domain="crypto", query="rsa", max_bytes=100, total_bytes=100)
        assert [item["name"] for item in loaded] == ["crypto.rsa.small_e"] and skipped == []


def test_skill_section_is_cached_after_first_read():
    with tempfile.TemporaryDirectory() as tmp:
        loader = _loader(tmp, {"crypto.rsa.small_e.md": "cached text"})
        calls = []
        original = Path.read_bytes
        def counted(self):
            calls.append(str(self))
            return original(self)
        with patch.object(Path, "read_bytes", counted):
            assert "cached text" in loader.load_section("crypto.rsa.small_e")
            assert "cached text" in loader.load_section("crypto.rsa.small_e")
        assert len(calls) == 1
        try:
            loader.load_section("crypto.rsa.small_e", max_bytes=2)
            raise AssertionError("cached section bypassed smaller byte cap")
        except ValueError:
            pass


def test_skill_index_traversal_and_missing_section_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sections").mkdir()
        (root / "index.json").write_text(json.dumps([
            {"name": "bad", "domain": "misc", "file": "../escape.md"}
        ]), encoding="utf-8")
        try:
            SkillLoader(root)
            raise AssertionError("traversal index accepted")
        except ValueError:
            pass
    with tempfile.TemporaryDirectory() as tmp:
        loader = _loader(tmp)
        loaded, skipped = loader.load_for(domain="crypto", query="rsa")
        assert loaded == [] and skipped[0]["reason"] in ("FileNotFoundError", "ValueError")


def test_default_index_covers_more_domains_and_alias_maps_reversing():
    loader = SkillLoader()
    domains = {meta["domain"] for meta in loader.list_index()}
    assert {"crypto", "reverse", "pwn", "forensics", "misc"} <= domains
    # 平台常见分类名 reversing 映射到 reverse，否则该题一个技能都选不出来。
    assert loader.select(domain="reversing", query="elf")[0]["name"] == "reverse.elf.static"
    assert loader.select(domain="reverse", query="shellcode cstool")[0]["name"] == "reverse.shellcode"
    assert loader.select(domain="stego", query="png steghide")[0]["name"] == "forensics.stego.image"
