"""测试公共工具：内存化黑板构建（无 pytest 依赖，run_checks.py 与 pytest 均可运行）。"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ctf_ext.domain.migrate import migrate  # noqa: E402
from ctf_ext.domain.models import Challenge  # noqa: E402
from ctf_ext.store.blackboard import Blackboard  # noqa: E402


def new_board(tmpdir=None):
    tmp = tempfile.mkdtemp(prefix="ctf-test-") if tmpdir is None else tmpdir
    conn = sqlite3.connect(os.path.join(tmp, "test.db"), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return Blackboard(conn), conn


def add_challenge(board, cid="c1", points=950.0, level=3):
    board.add_challenge(Challenge(id=cid, name=cid, category="misc",
                                  points_hint=points, difficulty_level=level))
    return cid
