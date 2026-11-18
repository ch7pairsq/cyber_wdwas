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
import uuid
from pathlib import Path
from typing import Optional


class EvidenceTooLarge(Exception):
    pass


class EvidenceStore:
    """put() 返回 sha256，作为 evidence_id。"""

    def __init__(self, root, db: sqlite3.Connection, max_bytes: int = 64 * 1024 * 1024):
        self._root = Path(root)
        self._ensure_private_dir(self._root)
        self._db = db
        self._max_bytes = max_bytes

    @staticmethod
    def _ensure_private_dir(path: Path) -> None:
        """证据可能含 Flag/凭据：目录 0700，文件 0600（§15.2）。"""
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(str(path), 0o700)
        except OSError:
            pass

    def put_bytes(self, data: bytes, kind: str = "raw") -> str:
        if len(data) > self._max_bytes:
            raise EvidenceTooLarge("evidence {} bytes > cap {}".format(len(data), self._max_bytes))
        sha = hashlib.sha256(data).hexdigest()
        path = self._root / sha[:2] / sha
        if not path.exists():
            self._ensure_private_dir(path.parent)
            self._atomic_write(path, data)
        self._register(sha, kind, str(path), len(data))
        return sha

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(str(path), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    @classmethod
    def _atomic_write(cls, path: Path, data: bytes) -> None:
        temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(str(temporary), 0o600)
            except OSError:
                pass
            os.replace(str(temporary), str(path))
            cls._fsync_dir(path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

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
            self._ensure_private_dir(dest.parent)
            tmp = dest.with_name(dest.name + ".tmp-" + uuid.uuid4().hex)
            try:
                shutil.copyfile(str(p), str(tmp))
                try:
                    os.chmod(str(tmp), 0o600)
                except OSError:
                    pass
                with tmp.open("rb") as handle:
                    os.fsync(handle.fileno())
                os.replace(str(tmp), str(dest))
                self._fsync_dir(dest.parent)
            finally:
                if tmp.exists():
                    tmp.unlink()
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
        raw = Path(row["path"])
        path = raw.resolve()
        root = self._root.resolve()
        if raw.is_symlink() or root not in path.parents or not path.exists() or path.stat().st_size != row["bytes"]:
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
        # 若调用方已开启事务（例如结果写入），由调用方统一提交，避免提前提交半笔状态。
        if not self._db.in_transaction:
            self._db.commit()
