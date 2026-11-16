"""证据库：内容寻址落盘 + 哈希登记（§3、§7.3、附录 F-3）。

两种登记：
- 普通证据：内容落盘，路径按 sha256 前两位分片；
- hash_only 证据：只登记哈希（不存原文），verify 仅校验登记存在。

put_file 复制而非移动源文件：题目附件必须保持可读（§15.2「不重复下载」）。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional


class EvidenceTooLarge(Exception):
    pass


class EvidenceStore:
    """put() 返回 sha256，作为 evidence_id。"""

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
            os.replace(str(tmp), str(path))
        self._register(sha, kind, str(path), len(data))
        return sha

    def put_file(self, path, kind: str = "raw") -> str:
        """复制文件到证据库，源文件保持不变。"""
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
            tmp = dest.with_suffix(".tmp")
            shutil.copyfile(str(p), str(tmp))
            os.replace(str(tmp), str(dest))
        self._register(sha, kind, str(dest), size)
        return sha

    def put_hash(self, sha: str, kind: str = "hash_only") -> str:
        """只登记哈希：适用于输出过大或含敏感内容、不需要留存原文的场景。"""
        self._register(sha, kind, "", 0)
        return sha

    def verify(self, sha: str) -> bool:
        """校验证据可被引用：登记存在；普通证据还要文件在且哈希匹配。"""
        row = self._db.execute("SELECT path, bytes FROM evidence WHERE sha256=?", (sha,)).fetchone()
        if not row:
            return False
        if not row["path"]:
            return True  # hash_only：只证明当时计算过该哈希
        path = Path(row["path"])
        if not path.exists() or path.stat().st_size != row["bytes"]:
            return False
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest() == sha

    def exists(self, sha: str) -> bool:
        row = self._db.execute("SELECT 1 FROM evidence WHERE sha256=?", (sha,)).fetchone()
        return row is not None

    def _register(self, sha: str, kind: str, path: str, size: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO evidence(sha256, kind, path, bytes, created_at) VALUES (?,?,?,?,?)",
            (sha, kind, path, size, time.time()),
        )
        self._db.commit()
