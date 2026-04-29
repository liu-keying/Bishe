#!/usr/bin/env python3
"""
一键把「多个 .ts 分片 + hidden」POST 到 A 的 /overlay/embed-hls，并启用“方案 B”：

- hidden 先整体 AEAD 加密一次
- 密文均分为 k 份，随机散入 k 个 TS 分片尾部（tag 只有一份，B 侧组装后再发给 C）

示例:

  python scripts/publish_embed_hls_split_cipher.py --dir . --hidden hidden.bin --k 3

  python scripts/publish_embed_hls_split_cipher.py --segments "D:/out/hls_seg*.ts" --hidden hidden.bin --k 5 --pad-bytes 188
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
    p = argparse.ArgumentParser(description="方案 B：k 份密文分散 embed-hls（B 侧组装后再到 C）")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--segments", help=r'glob，如 hls_seg*.ts 或 "D:\out\*.ts"（按文件名排序）')
    src.add_argument("--dir", metavar="DIR", help="目录内所有 .ts 文件（按文件名排序，不含子目录）")

    p.add_argument("--hidden", required=True, help="隐匿文件路径")
    p.add_argument("--a-url", default="http://127.0.0.1:8000", help="A 根地址")
    p.add_argument("--c-url", default="", help="可选，查询参数 c=")
    p.add_argument("--k", type=int, default=3, help="密文分片数（k>=2 才是方案 B）")
    p.add_argument("--g-bytes", type=int, default=0, help="每份密文分片大小（bytes）。0=由 A 自动按密文均分推导")
    p.add_argument("--g-bits", type=int, default=0, help="每份密文分片大小（bits）。优先级低于 --g-bytes")
    p.add_argument("--pad-bytes", type=int, default=0, help="可选：pad_bytes=（未携带密文的 TS 尾部追加伪 TS 字节）")
    p.add_argument("--extract", default="", help="可选 append_marker")
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
            return 1

    if args.k < 2:
        print("--k 必须 >= 2 才是方案 B（否则请用 publish_embed_hls.py 或 k=1）", file=sys.stderr)
        return 1
    if args.k > len(paths):
        print(f"--k 不能大于分片数（k={args.k}, segments={len(paths)}）", file=sys.stderr)
        return 1
    if args.pad_bytes < 0:
        print("--pad-bytes 必须 >= 0", file=sys.stderr)
        return 1

    url = f"{args.a_url.rstrip('/')}/overlay/embed-hls"
    params: dict[str, str] = {"k": str(args.k)}
    if args.c_url.strip():
        params["c"] = args.c_url.strip()
    if args.pad_bytes:
        params["pad_bytes"] = str(args.pad_bytes)
    if int(args.g_bytes or 0) > 0:
        params["g_bytes"] = str(int(args.g_bytes))
    elif int(args.g_bits or 0) > 0:
        params["g_bits"] = str(int(args.g_bits))
    if args.extract.strip():
        params["extract"] = args.extract.strip()

    print(f"将上传 {len(paths)} 个分片 + hidden -> {url} (k={args.k})")
    for i, path in enumerate(paths):
        print(f"  [{i}] {path}")

    with ExitStack() as stack, httpx.Client(timeout=600.0, trust_env=False) as client:
        files: list[tuple[str, tuple[str, object, str]]] = []
        for path in paths:
            f = stack.enter_context(open(path, "rb"))
            files.append(("segment", (os.path.basename(path), f, "video/mp2t")))
        hf = stack.enter_context(open(args.hidden, "rb"))
        files.append(("hidden", (os.path.basename(args.hidden), hf, "application/octet-stream")))

        r = client.post(url, params=params or None, files=files)

    print(r.status_code, r.text[:800] if r.text else "")
    if r.status_code != 200:
        return 1
    try:
        obj = r.json()
    except Exception:
        return 0

    ck = obj.get("cipher_k")
    cg = obj.get("cipher_group")
    if int(ck or 0) == int(args.k) and cg:
        print(f"OK: 方案 B 已启用 cipher_k={ck} cipher_group={cg}")
    else:
        print(f"警告: 返回未体现方案 B（cipher_k={ck!r}, cipher_group={cg!r}），请检查 A 是否为最新代码/参数是否生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

