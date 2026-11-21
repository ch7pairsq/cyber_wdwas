"""只读工具补齐（pcap_summary/wasm_translate/java_decompile/media_probe/disk_partitions/disk_files）：
注册、argv 构造、参数守卫与端到端（依赖镜像内 tshark/wabt/procyon/ffprobe/sleuthkit）。
"""
from __future__ import annotations

import shutil
import struct
import wave
from pathlib import Path

import pytest

from ctf_ext.gateway.catalog import default_specs
from ctf_ext.gateway.policy import PolicyConfig, PolicyEngine
from ctf_ext.gateway.tools import ToolRegistry

PACK = ("pcap_summary", "wasm_translate", "java_decompile",
        "media_probe", "disk_partitions", "disk_files")


def _registry(work_root=None):
    policy = PolicyEngine(PolicyConfig(command_allowlist=("tshark", "wasm2wat", "procyon",
                                                          "ffprobe", "mmls", "fls"),
                                       sandbox_required=False))
    registry = ToolRegistry(policy, work_root=work_root)
    for spec in default_specs():
        registry.register(spec)
    return registry


def test_readonly_pack_registered():
    specs = {spec.name: spec for spec in _registry().specs()}
    expected_command = {"pcap_summary": "tshark", "wasm_translate": "wasm2wat",
                        "java_decompile": "procyon", "media_probe": "ffprobe",
                        "disk_partitions": "mmls", "disk_files": "fls"}
    for name in PACK:
        assert name in specs, name
        assert specs[name].command == expected_command[name]
        assert specs[name].risk_level == "read" and specs[name].evidence_policy == "raw"


def test_pcap_summary_argv_shape(tmp_path):
    from ctf_ext.gateway.catalog import _pcap_summary_argv
    argv = _pcap_summary_argv({"path": "cap.pcap"}, tmp_path)
    assert argv[0] == "tshark" and argv[1] == "-r"
    assert argv[2] == str(tmp_path / "cap.pcap")
    assert argv[3:] == ["-q", "-z", "io,phs"]
    with pytest.raises(ValueError):
        _pcap_summary_argv({"path": "../escape.pcap"}, tmp_path)


def test_wasm_and_java_argv_shape(tmp_path):
    from ctf_ext.gateway.catalog import _java_decompile_argv, _wasm_translate_argv
    assert _wasm_translate_argv({"path": "chal.wasm"}, tmp_path) == \
        ["wasm2wat", str(tmp_path / "chal.wasm")]
    assert _java_decompile_argv({"path": "a.class"}, tmp_path) == \
        ["procyon", str(tmp_path / "a.class")]


def test_media_probe_argv_shape(tmp_path):
    from ctf_ext.gateway.catalog import _media_probe_argv
    argv = _media_probe_argv({"path": "clip.mp4"}, tmp_path)
    assert argv[0] == "ffprobe" and argv[1] == "-v" and argv[2] == "error"
    assert "-show_format" in argv and "-show_streams" in argv and "json" in argv
    assert argv[-1] == str(tmp_path / "clip.mp4")


def test_disk_argv_shape_and_offset_guard(tmp_path):
    from ctf_ext.gateway.catalog import _disk_files_argv, _disk_partitions_argv
    assert _disk_partitions_argv({"path": "disk.img"}, tmp_path) == \
        ["mmls", str(tmp_path / "disk.img")]
    argv = _disk_files_argv({"path": "fs.img"}, tmp_path)
    assert argv == ["fls", "-r", "-p", str(tmp_path / "fs.img")]
    argv = _disk_files_argv({"path": "fs.img", "offset": 2048}, tmp_path)
    assert argv == ["fls", "-r", "-p", "-o", "2048", str(tmp_path / "fs.img")]
    with pytest.raises(ValueError):
        _disk_files_argv({"path": "fs.img", "offset": -1}, tmp_path)
    with pytest.raises(ValueError):
        _disk_files_argv({"path": "fs.img", "offset": 2 ** 32}, tmp_path)
    with pytest.raises(ValueError):
        _disk_files_argv({"path": "fs.img", "offset": "x"}, tmp_path)


def _minimal_eth_ip_tcp_pcap(path: Path) -> None:
    """构造含一帧 Ethernet/IPv4/TCP 的最小 pcap（IP 校验和手算，TCP 校验和置 0）。"""
    ip_header = bytearray(b"\x45\x00\x00\x28\x00\x01\x00\x00\x40\x06\x00\x00"
                          b"\x0a\x00\x00\x01\x0a\x00\x00\x02")
    total = 0
    for i in range(0, 20, 2):
        total += (ip_header[i] << 8) | ip_header[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    ip_header[10:12] = (~total & 0xFFFF).to_bytes(2, "big")
    frame = (b"\x00\x11\x22\x33\x44\x55\x66\x77\x88\x99\xaa\xbb\x08\x00"
             + bytes(ip_header)
             + b"\x00\x50\x00\x50\x00\x00\x00\x01\x00\x00\x00\x02\x50\x02\x20\x00\x00\x00\x00\x00")
    rec = struct.pack("<IIII", 1700000000, 0, len(frame), len(frame)) + frame
    path.write_bytes(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1) + rec)


@pytest.mark.skipif(shutil.which("tshark") is None, reason="镜像无 tshark")
def test_pcap_summary_end_to_end(tmp_path):
    _minimal_eth_ip_tcp_pcap(tmp_path / "cap.pcap")
    out = _registry(str(tmp_path)).execute_arguments("pcap_summary", {"path": "cap.pcap"},
                                                     cwd=str(tmp_path))
    assert out.status == "ok" and "tcp" in out.output.lower()


@pytest.mark.skipif(shutil.which("wasm2wat") is None, reason="镜像无 wasm2wat")
def test_wasm_translate_end_to_end(tmp_path):
    (tmp_path / "m.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    out = _registry(str(tmp_path)).execute_arguments("wasm_translate", {"path": "m.wasm"},
                                                     cwd=str(tmp_path))
    assert out.status == "ok" and "(module)" in out.output


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="镜像无 ffprobe")
def test_media_probe_end_to_end(tmp_path):
    with wave.open(str(tmp_path / "a.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    out = _registry(str(tmp_path)).execute_arguments("media_probe", {"path": "a.wav"},
                                                     cwd=str(tmp_path))
    assert out.status == "ok" and "wav" in out.output.lower()
