"""ocr_text / pdf_images 工具：注册、argv 构造与端到端（依赖镜像内 tesseract/PIL）。"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ctf_ext.gateway.catalog import default_specs
from ctf_ext.gateway.policy import PolicyConfig, PolicyEngine
from ctf_ext.gateway.tools import ToolRegistry

TESSERACT = Path("/usr/bin/tesseract")
SYSTEM_PY = Path("/usr/bin/python3")
HAS_OCR_STACK = TESSERACT.exists() and SYSTEM_PY.exists()
HAS_PDFIMAGES = shutil.which("pdfimages") is not None


def _registry(work_root=None):
    policy = PolicyEngine(PolicyConfig(command_allowlist=("python3", "tesseract"),
                                       sandbox_required=False))
    registry = ToolRegistry(policy, work_root=work_root)
    for spec in default_specs():
        registry.register(spec)
    return registry


def test_ocr_and_pdf_tools_registered(tmp_path):
    registry = _registry(str(tmp_path))
    specs = {spec.name: spec for spec in registry.specs()}
    ocr = specs["ocr_text"]
    pdf = specs["pdf_images"]
    assert ocr.risk_level == "read" and ocr.command == "tesseract"
    assert pdf.risk_level == "execute" and not pdf.idempotent and pdf.command == "python3"


def test_ocr_text_argv_shape_and_guards(tmp_path):
    from ctf_ext.gateway.catalog import _ocr_text_argv
    argv = _ocr_text_argv({"path": "shot.png"}, tmp_path)
    assert argv[:3] == ["tesseract", str(tmp_path / "shot.png"), "stdout"]
    assert argv[argv.index("-l") + 1] == "eng+chi_sim" and argv[argv.index("--psm") + 1] == "3"
    with pytest.raises(ValueError):
        _ocr_text_argv({"path": "a.png", "lang": "fra"}, tmp_path)
    with pytest.raises(ValueError):
        _ocr_text_argv({"path": "a.png", "psm": 1}, tmp_path)
    with pytest.raises(ValueError):
        _ocr_text_argv({"path": "../escape.png"}, tmp_path)


def test_pdf_images_argv_shape_and_guards(tmp_path):
    from ctf_ext.gateway.catalog import _pdf_images_argv
    argv = _pdf_images_argv({"path": "doc.pdf", "mode": "extract", "prefix": "im"}, tmp_path)
    assert argv[0] == "python3" and argv[1].endswith("pdf_images.py")
    assert "--mode" in argv and "extract" in argv and "--prefix" in argv
    with pytest.raises(ValueError):
        _pdf_images_argv({"path": "doc.pdf", "mode": "delete"}, tmp_path)
    with pytest.raises(ValueError):
        _pdf_images_argv({"path": "doc.pdf", "prefix": "../evil"}, tmp_path)


def _minimal_pdf_with_image(path: Path) -> None:
    """程序化构造含 8x8 RGB 图像流的最小 PDF（xref 偏移按字节计算）。"""
    rgb = b"\xff\x00\x00" * 64  # 8x8 红色
    content = b"q 8 0 0 8 0 0 cm /Im0 Do Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 8 8] "
        b"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Length " + str(len(rgb)).encode() + b" >>\nstream\n" + rgb + b"\nendstream",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_pos = len(out)
    xref = b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        xref += ("%010d 00000 n \n" % off).encode()
    trailer = (b"trailer\n<< /Size " + str(len(objects) + 1).encode()
               + b" /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF\n")
    path.write_bytes(out + xref + trailer)


@pytest.mark.skipif(not HAS_PDFIMAGES, reason="镜像无 poppler/pdfimages")
def test_pdf_images_list_and_extract_end_to_end(tmp_path):
    registry = _registry(str(tmp_path))
    _minimal_pdf_with_image(tmp_path / "doc.pdf")
    listed = registry.execute_arguments("pdf_images", {"path": "doc.pdf", "mode": "list"},
                                        cwd=str(tmp_path))
    assert listed.status == "ok" and "rgb" in listed.output and "image" in listed.output
    extracted = registry.execute_arguments(
        "pdf_images", {"path": "doc.pdf", "mode": "extract", "prefix": "im"},
        cwd=str(tmp_path))
    assert extracted.status == "ok"
    names = [line for line in extracted.output.splitlines() if line.strip()]
    assert names and names[0].startswith("im-")
    assert (tmp_path / names[0]).is_file() and (tmp_path / names[0]).stat().st_size > 0


@pytest.mark.skipif(not HAS_OCR_STACK, reason="镜像无 tesseract 时跳过真实 OCR")
def test_ocr_text_real_end_to_end(tmp_path):
    """用运行时 Python（venv，带 PIL）渲染含文字 PNG，走 registry 全链路识别。"""
    script = (
        "from PIL import Image, ImageDraw, ImageFont\n"
        "img = Image.new('RGB', (320, 96), 'white')\n"
        "d = ImageDraw.Draw(img)\n"
        "try:\n"
        "    font = ImageFont.load_default(size=64)\n"
        "except TypeError:\n"
        "    font = ImageFont.load_default()\n"
        "d.text((16, 16), 'OK', fill='black', font=font)\n"
        "img.save({!r})\n".format(str(tmp_path / "shot.png"))
    )
    # PIL 在 /opt/venv，渲染子进程必须用当前解释器；SYSTEM_PY 无 PIL（v19 镜像实测）。
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode()
    result = _registry(str(tmp_path)).execute_arguments(
        "ocr_text", {"path": "shot.png", "lang": "eng"}, cwd=str(tmp_path))
    assert result.status == "ok" and result.output.strip() != ""
