"""证据库：内容寻址落盘 + 哈希登记（§3「证据与结论分离」、附录 F-3）。"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional


class EvidenceTooLarge(Exception):
    pass


class EvidenceStore:
    """put() 返回 sha256，作为 evidence_id；文件按前两位分片存放。"""

    def __init__(self, root, db: sqlite3.Connection, max_bytes: int = 64 * 1024 * 1024):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._db = db
        self._max_bytes = max_bytes

    def put_bytes(self, data: bytes, kind: str = "raw") -> str:
        if len(data) > self._max_bytes:
            raise EvidenceTooLarge("evidence {} bytes > cap {}".format(len(data), self._max_bytes))
        sha = hashlib.sha256(data).hexdigest()
        path = self._root / sha[:2] / sha
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        self._register(sha, kind, str(path), len(data))
        return sha

    def put_file(self, path, kind: str = "raw") -> str:
        p = Path(path)
        size = p.stat().st_size
        if size > self._max_bytes:
            raise EvidenceTooLarge("file {} bytes > cap {}".format(size, self._max_bytes))
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        sha = h.hexdigest()
        dest = self._root / sha[:2] / sha
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(p), str(dest))
        self._register(sha, kind, str(dest), size)
        return sha

    def verify(self, sha: str) -> bool:
        row = self._db.execute("SELECT path, bytes FROM evidence WHERE sha256=?", (sha,)).fetchone()
        if not row:
            return False
        path = Path(row["path"])
        if not path.exists() or path.stat().st_size != row["bytes"]:
            return False
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest() == sha

    def _register(self, sha: str, kind: str, path: str, size: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO evidence(sha256, kind, path, bytes, created_at) VALUES (?,?,?,?,?)",
            (sha, kind, path, size, time.time()),
        )
        self._db.commit()
