"""解压防护（§15.1）：路径、条目数、字节、深度、压缩比、符号链接。"""
import io
import os
import tempfile
import zipfile

from ctf_ext.gateway.archive import ArchiveLimits, ArchiveRejected, extract_zip, inspect_zip


def make_zip(files, method=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=method) as zf:
        for name, data in files:
            zf.writestr(name, data)
    return buf.getvalue()


def _reject(data, code, limits=None):
    dest = tempfile.mkdtemp(prefix="unz-")
    try:
        extract_zip(data, dest, limits or ArchiveLimits())
        raise AssertionError("expected " + code)
    except ArchiveRejected as exc:
        assert exc.reason_code == code, (exc.reason_code, code)
    return dest


def test_normal_archive_extracts():
    data = make_zip([("a.txt", b"hello"), ("dir/b.txt", b"world")])
    dest = tempfile.mkdtemp(prefix="unz-")
    written = extract_zip(data, dest)
    assert written == ["a.txt", "dir/b.txt"]
    with open(os.path.join(dest, "dir", "b.txt"), "rb") as fh:
        assert fh.read() == b"world"


def test_traversal_and_absolute_paths_rejected():
    dest = _reject(make_zip([("../evil.txt", b"x")]), "ZIP_TRAVERSAL")
    assert os.listdir(dest) == []
    _reject(make_zip([("/tmp/evil.txt", b"x")]), "ZIP_TRAVERSAL")


def test_entry_count_and_size_limits():
    files = [("f{}.txt".format(i), b"x") for i in range(3)]
    _reject(make_zip(files), "ZIP_ENTRIES", ArchiveLimits(max_entries=2))
    _reject(make_zip([("big.bin", b"x" * 5000)]), "ZIP_ENTRY_SIZE",
            ArchiveLimits(max_entry_bytes=1000))


def test_depth_limit():
    _reject(make_zip([("a/b/c.txt", b"x")]), "ZIP_DEPTH", ArchiveLimits(max_depth=2))


def test_compression_ratio_bomb():
    payload = b"\0" * (2 * 1024 * 1024)
    data = make_zip([("zeros.bin", payload)])
    limits = ArchiveLimits(max_ratio=10, ratio_min_bytes=1024 * 1024)
    _reject(data, "ZIP_RATIO", limits)
    stats = inspect_zip(data, ArchiveLimits(max_ratio=10 ** 9))  # 只取统计，不放宽生产限额
    assert stats["ratio"] > 10 and stats["total_uncompressed"] == len(payload)


def test_symlink_entry_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "target")
    _reject(buf.getvalue(), "ZIP_SYMLINK")


def test_invalid_archive_rejected():
    _reject(b"not a zip at all", "ZIP_INVALID")
