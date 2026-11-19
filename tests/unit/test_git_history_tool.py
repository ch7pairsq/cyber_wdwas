"""git_history 注册工具：argv 构造、逃逸拒绝与包装脚本真实行为（git 缺失则跳过）。"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from ctf_ext.gateway.catalog import default_specs

_GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(_GIT is None, reason="git not installed in this image")


def _spec():
    return {s.name: s for s in default_specs()}["git_history"]


def _run_git(root, *args):
    subprocess.run(["git", *args], cwd=str(root), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _run_wrapper(argv, **kwargs):
    """跑包装脚本：argv 首位是 "python3"，换成当前解释器保证确定性。"""
    return subprocess.run([sys.executable if a == "python3" else a for a in argv],
                          **kwargs)


def _repo_with_deleted_file(root: Path):
    root.mkdir(parents=True)
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "t@t")
    _run_git(root, "config", "user.name", "t")
    (root / "secret.txt").write_text("flag{...}")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "add secret")
    (root / "secret.txt").unlink()
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-qm", "remove secret")
    return root


def test_git_history_spec_is_read_only_with_whitelisted_modes():
    spec = _spec()
    assert spec.risk_level == "read" and spec.idempotent and spec.command == "python3"
    assert spec.input_schema["properties"]["mode"]["enum"] == ["log", "show", "fsck"]
    assert "rev" not in spec.input_schema["required"]


def test_git_history_argv_builder_resolves_path_and_validates_rev():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        argv = _spec().argv_builder({"path": "repo/.git", "mode": "show", "rev": "HEAD~1:flag.txt"}, root)
        assert argv[:2] == ["python3", argv[1]] and argv[1].endswith("git_history.py")
        assert argv[argv.index("--git-dir") + 1] == str((root / "repo/.git").resolve())
        assert argv[argv.index("--rev") + 1] == "HEAD~1:flag.txt"
        for bad in ({"path": "outside", "mode": "show", "rev": "-Ofoo"},
                    {"path": "outside", "mode": "show", "rev": "a;b"},
                    {"path": "outside", "mode": "shell"},
                    {"path": "../../etc/passwd"}):
            with pytest.raises(ValueError):
                _spec().argv_builder(bad, root)


def test_git_history_wrapper_reads_log_and_recovers_deleted_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = _repo_with_deleted_file(Path(tmp) / "repo")
        spec = _spec()
        registry_log = spec.argv_builder({"path": "repo", "mode": "log"}, Path(tmp))
        out = _run_wrapper(registry_log, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=30)
        text = out.stdout.decode()
        assert out.returncode == 0 and "remove secret" in text and "secret.txt" in text
        show = spec.argv_builder({"path": "repo", "mode": "show", "rev": "HEAD~1:secret.txt"}, Path(tmp))
        out = _run_wrapper(show, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=30)
        assert out.returncode == 0 and b"flag{...}" in out.stdout


def test_git_history_wrapper_fsck_finds_dangling_object():
    with tempfile.TemporaryDirectory() as tmp:
        root = _repo_with_deleted_file(Path(tmp) / "repo")
        # 悬空 blob：stdin 内容入库但不进提交树（内容必须与已提交 blob 不同，
        # 否则对象被首提交引用，fsck 判无悬空是正确行为）。
        subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=str(root),
                       input=b"orphan-payload-not-in-history", check=True, stdout=subprocess.PIPE)
        fsck = _spec().argv_builder({"path": "repo", "mode": "fsck"}, Path(tmp))
        out = _run_wrapper(fsck, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=30)
        assert out.returncode == 0 and b"dangling blob" in out.stdout


def test_git_history_wrapper_rejects_non_repo_and_bad_rev():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notrepo").mkdir()
        spec = _spec()
        cases = [{"path": "notrepo", "mode": "log"},
                 {"path": "repo", "mode": "show", "rev": "--upload-pack=evil"}]
        for case in cases:
            try:
                argv = spec.argv_builder(case, root)
            except ValueError:
                continue  # argv 构造层白名单拒绝同样算拒绝路径
            out = _run_wrapper(argv, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=30)
            assert out.returncode == 2, (case, out.returncode)
