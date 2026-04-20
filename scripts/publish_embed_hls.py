#!/usr/bin/env python3
"""
一键把「多个 .ts 分片 + hidden」POST 到 A 的 /overlay/embed-hls。

示例:

  python scripts/publish_embed_hls.py --dir . --hidden hidden.bin --c-url http://127.0.0.1:8002

  python scripts/publish_embed_hls.py --segments "D:/out/hls_seg*.ts" --hidden hidden.bin
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from contextlib import ExitStack

import httpx


def _ts_files_in_dir(directory: str) -> list[str]:
    d = os.path.abspath(directory)
    if not os.path.isdir(d):
        return []
    out: list[str] = []
    for name in os.listdir(d):
        if name.lower().endswith(".ts"):
            out.append(os.path.join(d, name))
    return sorted(out)


def main() -> int:
    p = argparse.ArgumentParser(description="收集多个 .ts 分片，一次 POST /overlay/embed-hls")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--segments",
        help=r'glob，如 hls_seg*.ts 或 "D:\out\*.ts"（按文件名排序）',
    )
    src.add_argument(
        "--dir",
        metavar="DIR",
        help="目录内所有 .ts 文件（按文件名排序，不含子目录）",
    )
    p.add_argument("--hidden", required=True, help="隐匿文件路径")
    p.add_argument("--a-url", default="http://127.0.0.1:8000", help="A 根地址")
    p.add_argument("--c-url", default="", help="可选，查询参数 c=")
    p.add_argument(
        "--chunk-bytes",
        default="",
        help="可选，查询参数 chunk_bytes=（>=1024）；调大可减少 A 对最后一段的字节分块",
    )
    p.add_argument(
        "--extract",
        default="",
        help="可选 append_marker",
    )
    args = p.parse_args()

    if args.dir is not None:
        paths = _ts_files_in_dir(args.dir)
        if not paths:
            print("目录中没有 .ts 文件:", os.path.abspath(args.dir), file=sys.stderr)
            print("当前工作目录:", os.getcwd(), file=sys.stderr)
            return 1
    else:
        paths = sorted(glob.glob(args.segments))
        if not paths:
            print("没有匹配到文件:", args.segments, file=sys.stderr)
            print("当前工作目录:", os.getcwd(), file=sys.stderr)
            print(
                "提示: 请先用 ffmpeg 生成分片，或改用 --dir 指向放 .ts 的文件夹；"
                "glob 可写绝对路径，例如 D:/out/*.ts",
                file=sys.stderr,
            )
            return 1

    url = f"{args.a_url.rstrip('/')}/overlay/embed-hls"
    params: dict[str, str] = {}
    if args.c_url.strip():
        params["c"] = args.c_url.strip()
    if str(args.chunk_bytes).strip():
        params["chunk_bytes"] = str(args.chunk_bytes).strip()
    if args.extract.strip():
        params["extract"] = args.extract.strip()

    print(f"将上传 {len(paths)} 个分片 + hidden -> {url}")
    for i, path in enumerate(paths):
        print(f"  [{i}] {path}")

    with ExitStack() as stack, httpx.Client(timeout=600.0) as client:
        files: list[tuple[str, tuple[str, object, str]]] = []
        for path in paths:
            f = stack.enter_context(open(path, "rb"))
            files.append(
                ("segment", (os.path.basename(path), f, "video/mp2t")),
            )
        hf = stack.enter_context(open(args.hidden, "rb"))
        files.append(
            ("hidden", (os.path.basename(args.hidden), hf, "application/octet-stream")),
        )
        r = client.post(url, params=params or None, files=files)

    print(r.status_code, r.text[:500] if r.text else "")
    if r.status_code != 200:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
