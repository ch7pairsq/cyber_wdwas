"""解压防护（§15.1）：条目数、单条字节、总字节、路径深度、压缩比、符号链接。

所有检查在写出前完成（inspect_zip），extract 阶段再次按上限流式复核；
拒绝即抛 ArchiveRejected，调用方不得静默继续。
"""
from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Tuple, Union


class ArchiveRejected(Exception):
    def __init__(self, reason_code: str, detail: str = ""):
        super().__init__(detail or reason_code)
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 256
    max_entry_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 32 * 1024 * 1024
    max_depth: int = 6
    max_ratio: int = 200
    ratio_min_bytes: int = 1 * 1024 * 1024
    max_path_length: int = 200
    allowed_methods: Tuple[int, ...] = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


DEFAULT_LIMITS = ArchiveLimits()
CHUNK = 64 * 1024


def _open(source: Union[str, bytes, io.BytesIO]):
    try:
        if isinstance(source, bytes):
            return zipfile.ZipFile(io.BytesIO(source)), True
        if isinstance(source, io.BytesIO):
            return zipfile.ZipFile(source), False
        return zipfile.ZipFile(source), True
    except zipfile.BadZipFile as exc:
        raise ArchiveRejected("ZIP_INVALID", str(exc)) from exc


def _parts(name: str) -> List[str]:
    return [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]


def _check_member(info: zipfile.ZipInfo, limits: ArchiveLimits) -> List[str]:
    name = info.filename
    if len(name) > limits.max_path_length:
        raise ArchiveRejected("ZIP_PATH", "path too long: {} chars".format(len(name)))
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise ArchiveRejected("ZIP_TRAVERSAL", "absolute path: " + name)
    parts = _parts(name)
    if not parts or any(p == ".." for p in parts):
        raise ArchiveRejected("ZIP_TRAVERSAL", "unsafe path: " + name)
    if len(parts) > limits.max_depth:
        raise ArchiveRejected("ZIP_DEPTH", "{} > max_depth {}".format(len(parts), limits.max_depth))
    if info.flag_bits & 0x1:
        raise ArchiveRejected("ZIP_ENCRYPTED", name)
    if info.compress_type not in limits.allowed_methods:
        raise ArchiveRejected("ZIP_METHOD", "compression {} not allowed".format(info.compress_type))
    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
        raise ArchiveRejected("ZIP_SYMLINK", name)
    if info.file_size > limits.max_entry_bytes:
        raise ArchiveRejected("ZIP_ENTRY_SIZE", "{} bytes > {}".format(info.file_size, limits.max_entry_bytes))
    return parts


def inspect_zip(source, limits: ArchiveLimits = DEFAULT_LIMITS) -> Dict[str, float]:
    """在未解压任何条目前给出安全结论；不合规直接抛 ArchiveRejected。"""
    zf, close = _open(source)
    try:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > limits.max_entries:
            raise ArchiveRejected("ZIP_ENTRIES", "{} > {}".format(len(infos), limits.max_entries))
        total = sum(i.file_size for i in infos)
        compressed = sum(i.compress_size for i in infos)
        if total > limits.max_total_bytes:
            raise ArchiveRejected("ZIP_TOTAL_SIZE", "{} > {}".format(total, limits.max_total_bytes))
        ratio = total / max(compressed, 1)
        if total >= limits.ratio_min_bytes and ratio > limits.max_ratio:
            raise ArchiveRejected("ZIP_RATIO", "{}x > {}x".format(round(ratio, 1), limits.max_ratio))
        depth = 0
        for info in infos:
            depth = max(depth, len(_check_member(info, limits)))
        return {
            "entries": len(infos), "total_uncompressed": total, "total_compressed": compressed,
            "ratio": round(ratio, 2), "max_depth": depth,
        }
    finally:
        if close:
            zf.close()


def extract_zip(source, dest: str, limits: ArchiveLimits = DEFAULT_LIMITS) -> List[str]:
    """校验后解压到 dest；返回写入的相对路径。任何越界/超限都在写前拒绝。"""
    inspect_zip(source, limits)  # 先整体判定，避免“解到一半才发现超限”
    os.makedirs(dest, exist_ok=True)
    real_dest = os.path.realpath(dest)
    zf, close = _open(source)
    written: List[str] = []
    total = 0
    try:
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = _check_member(info, limits)
            target = os.path.join(real_dest, *parts)
            if os.path.commonpath([real_dest, os.path.realpath(target)]) != real_dest:
                raise ArchiveRejected("ZIP_TRAVERSAL", target)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            size = 0
            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if size > limits.max_entry_bytes:
                        raise ArchiveRejected("ZIP_ENTRY_SIZE", "streamed {} exceeds limit".format(info.filename))
                    if total > limits.max_total_bytes:
                        raise ArchiveRejected("ZIP_TOTAL_SIZE", "streamed total exceeds limit")
                    out.write(chunk)
            written.append("/".join(parts))
        return written
    except zipfile.BadZipFile as exc:
        raise ArchiveRejected("ZIP_INVALID", str(exc)) from exc
    finally:
        if close:
            zf.close()
