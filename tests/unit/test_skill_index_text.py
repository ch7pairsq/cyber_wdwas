"""技能目录：纯函数、字节上限、跳过 deprecated、不读正文。"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from ctf_ext.skills.loader import SkillLoader


def _loader(tmp, entries=None):
    root = Path(tmp)
    (root / "sections").mkdir(parents=True)
    (root / "index.json").write_text(json.dumps(entries or [
        {"name": "crypto.rsa.small_e", "domain": "crypto", "prerequisites": ["python3"],
         "tokens_hint": 10, "risk": "compute", "keywords": ["rsa", "small"],
         "file": "crypto.rsa.small_e.md"},
        {"name": "forensics.zip", "domain": "forensics", "prerequisites": ["7z"],
         "tokens_hint": 8, "risk": "read", "keywords": ["zip", "archive"],
         "file": "forensics.zip.md"},
        {"name": "old.skill", "domain": "misc", "deprecated": True,
         "file": "old.skill.md"},
    ]), encoding="utf-8")
    (root / "sections" / "crypto.rsa.small_e.md").write_text("SECRET BODY", encoding="utf-8")
    (root / "sections" / "forensics.zip.md").write_text("SECRET BODY", encoding="utf-8")
    (root / "sections" / "old.skill.md").write_text("SECRET BODY", encoding="utf-8")
    return SkillLoader(root)


def test_index_text_is_metadata_only_and_skips_deprecated():
    with tempfile.TemporaryDirectory() as tmp:
        loader = _loader(tmp)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("section must not be read")):
            text = loader.index_text(max_bytes=600)
        assert "crypto: rsa.small_e(rsa/small)" in text
        assert "forensics: zip(zip/archive)" in text
        assert "old" not in text
        assert "SECRET BODY" not in text
        assert len(text.encode("utf-8")) <= 600


def test_index_text_does_not_expose_command_keywords():
    entries = [{"name": "forensics.arc", "domain": "forensics", "file": "arc.md",
                "keywords": ["7z", "binwalk", "zip"]}]
    with tempfile.TemporaryDirectory() as tmp:
        text = _loader(tmp, entries).index_text()
    assert "7z" not in text and "binwalk" not in text
    assert "forensics: arc(zip)" in text


def test_index_text_respects_byte_cap_and_missing_optional_fields():
    entries = [{"name": "skill.%02d" % i, "domain": "misc", "file": "s%d.md" % i,
                "keywords": ["k%d" % i]} for i in range(40)]
    with tempfile.TemporaryDirectory() as tmp:
        loader = _loader(tmp, entries)
        text = loader.index_text(max_bytes=60)
        assert 0 < len(text.encode("utf-8")) <= 60
        assert text.endswith("...")
    with tempfile.TemporaryDirectory() as tmp:
        loader = _loader(tmp, [{"name": "bare", "domain": "misc", "file": "bare.md"}])
        assert "misc: bare" in loader.index_text()
        assert loader.index_text(max_bytes=0) == ""
