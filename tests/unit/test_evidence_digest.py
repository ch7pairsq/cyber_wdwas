"""evidence_digest 工具：注册、参数校验与脚本行为（只读、哈希校验、字节预算）。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.framework_support import fixture
from ctf_ext.gateway.catalog import default_specs
from ctf_ext.gateway.tools import ToolRegistry


def test_evidence_digest_registered():
    registry = ToolRegistry(None)
    for spec in default_specs():
        registry.register(spec)
    spec = registry._specs.get("evidence_digest")
    assert spec is not None
    assert spec.risk_level == "read" and spec.command == "python3"


def test_evidence_digest_argv_rejects_bad_ids(tmp_path):
    from ctf_ext.gateway.catalog import _evidence_digest_argv
    good = "a" * 64
    argv = _evidence_digest_argv({"evidence_ids": [good, "ZZZ", "short"]}, tmp_path / "workspaces")
    script_args = argv[2:]
    assert "a" * 64 in script_args and "ZZZ" not in script_args and "short" not in script_args
    assert argv[argv.index("--root") + 1].endswith("evidence")  # root 指向 workspaces 同级 evidence 目录
    try:
        _evidence_digest_argv({"evidence_ids": ["not-an-id"]}, tmp_path)
        raise AssertionError("expected rejection")
    except ValueError:
        pass


def test_evidence_digest_script_hashes_and_budget(tmp_path):
    import hashlib
    evidence_root = tmp_path / "evidence"
    content = b"flag{ev_1}\n" + b"A" * 5000
    sha1 = hashlib.sha256(content).hexdigest()
    (evidence_root / sha1[:2]).mkdir(parents=True)
    (evidence_root / sha1[:2] / sha1).write_bytes(content)
    small = b"flag{ev_2}"
    sha2 = hashlib.sha256(small).hexdigest()
    (evidence_root / sha2[:2]).mkdir(parents=True)
    (evidence_root / sha2[:2] / sha2).write_bytes(small)

    script = Path(__file__).resolve().parents[2] / "ctf_ext" / "gateway" / "evidence_digest.py"
    out = subprocess.run(
        [sys.executable, str(script), "--root", str(evidence_root),
         "--max-bytes", "1000", sha1, sha2],
        capture_output=True, timeout=30)
    text = out.stdout.decode()
    assert "flag{ev_2}" in text and "SNIP" in text  # 大文件头尾节选，小文件全量
    assert sha2 in text
    # 哈希不符的证据被拒（完整性）
    fake = "c" * 64
    out2 = subprocess.run(
        [sys.executable, str(script), "--root", str(evidence_root), fake],
        capture_output=True, timeout=30)
    assert "MISSING" in out2.stdout.decode()


def test_evidence_digest_end_to_end_via_registry(tmp_path):
    """registry 执行路径：workspace root 推导 evidence root，工具输出含内容。"""
    import hashlib
    evidence_root = tmp_path / "evidence"
    content = b"cross-file clue: flag{ev_3}"
    sha = hashlib.sha256(content).hexdigest()
    (evidence_root / sha[:2]).mkdir(parents=True)
    (evidence_root / sha[:2] / sha).write_bytes(content)
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    from ctf_ext.gateway.catalog import _evidence_digest_argv
    argv = _evidence_digest_argv({"evidence_ids": [sha]}, workspaces)
    assert argv[argv.index("--root") + 1] == str(evidence_root)
    out = subprocess.run(argv, capture_output=True, timeout=30)
    assert "flag{ev_3}" in out.stdout.decode()
