"""pdf_images：列出/抽取 PDF 内嵌图片（pdfimages 包装，输出落到题目工作区）。

固定 argv 由 catalog._pdf_images_argv 构造：python3 pdf_images.py --mode
list|extract --prefix <safe_name> <pdf>。list 打印内嵌图片清单；extract
输出 <prefix>-<页>-<序>.png 文件名清单，供后续 qr_decode / ocr_text 使用。
脚本只在工作区内写 prefix-*.png，不做其他写操作。
"""
from __future__ import annotations

import argparse
import glob
import subprocess
import sys


def main() -> int:
    ap = argparse.ArgumentParser(prog="pdf_images")
    ap.add_argument("--mode", choices=("list", "extract"), required=True)
    ap.add_argument("--prefix", default="pdfimg")
    ap.add_argument("pdf", help="PDF 路径（工作区内）")
    args = ap.parse_args()
    if args.mode == "list":
        proc = subprocess.run(["pdfimages", "-list", args.pdf],
                              capture_output=True, text=True, timeout=60)
        sys.stdout.write(proc.stdout)
        if proc.returncode and proc.stderr:
            sys.stdout.write(proc.stderr)
        return proc.returncode
    proc = subprocess.run(["pdfimages", "-png", "-p", args.pdf, args.prefix],
                          capture_output=True, text=True, timeout=120)
    files = sorted(glob.glob(args.prefix + "-*.png"))
    if not files:
        if proc.stderr:
            sys.stdout.write(proc.stderr)
        print("no images extracted (returncode={})".format(proc.returncode))
        return proc.returncode or 3
    for name in files:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
