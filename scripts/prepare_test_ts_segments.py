#!/usr/bin/env python3
"""从现有 hls_seg*.ts 循环复制，凑齐至少 N 个分片（默认 15），供 segments_limit=6/9/12/15 矩阵使用。"""
from __future__ import annotations

import argparse
import glob
import shutil
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--dir",
        type=Path,
        default=Path(__file__).resolve().parent / "_test_ts",
    )
    p.add_argument("--count", type=int, default=15, help="目标分片数量（生成 hls_seg000.ts ..）")
    args = p.parse_args()

    d = args.dir.resolve()
    d.mkdir(parents=True, exist_ok=True)
    src_paths = sorted(glob.glob(str(d / "hls_seg*.ts")))
    if not src_paths:
        raise SystemExit(f"no hls_seg*.ts under {d}")

    n_src = len(src_paths)
    want = max(1, int(args.count))
    for i in range(want):
        src = Path(src_paths[i % n_src])
        dst = d / f"hls_seg{i:03d}.ts"
        if dst.resolve() == src.resolve():
            continue
        shutil.copy2(src, dst)
        print(f"wrote {dst.name} <- {src.name}")

    final = sorted(glob.glob(str(d / "hls_seg*.ts")))
    print(f"done: {len(final)} files in {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
