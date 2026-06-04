#!/usr/bin/env python3
"""
端到端时延矩阵：每个参数组合只发送 1 条隐蔽数据（非压测），统计 C /stats 的 e2e_ms。

三维度（与论文/原 bench_matrix 一致）：
  - hidden_bytes: 1024 / 4096 / 65536
  - segments_limit (HLS 分片数): 6 / 9 / 12 / 15
  - k (密文分片数): 1 / 2 / 3 / 5

前置：A、B-gate（或 b-pull+b-worker）、C、Redis 已启动。

用法（仓库根目录）:
  python scripts/bench_matrix_embed_hls_e2e_single.py

  # 小分片 demo（65536+k=1 会单片容量不足）:
  python scripts/bench_matrix_embed_hls_e2e_single.py --segments "scripts/_test_ts/hls_seg*.ts"
"""
from __future__ import annotations

import argparse
import csv
import glob
import shlex
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEGMENTS_GLOB = str(_REPO_ROOT / "*.ts")


def _parse_bench_output(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for tok in line.replace(",", " ").split():
            if "=" in tok and tok.count("=") == 1:
                k, v = tok.split("=", 1)
                if k and v:
                    out[k.strip()] = v.strip()
    return out


def _run_one(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")


def _split_int_list(s: str) -> list[int]:
    return [int(x) for x in str(s).split(",") if str(x).strip()]


def _segment_count(*, segments_glob: str, limit: int) -> int:
    paths = sorted(glob.glob(segments_glob))
    if not paths:
        return 0
    if limit > 0:
        paths = paths[:limit]
    return len(paths)


def main() -> int:
    p = argparse.ArgumentParser(description="单条发送端到端时延矩阵（非压测）")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--out", default="bench_matrix_embed_hls_e2e_single.csv")
    p.add_argument("--a-url", default="http://127.0.0.1:8000")
    p.add_argument("--c-url", default="http://127.0.0.1:8002")
    p.add_argument(
        "--segments",
        default=DEFAULT_SEGMENTS_GLOB,
        help=r'glob，默认仓库根目录大分片 "*.ts"（与旧压测一致）',
    )
    p.add_argument("--k-list", default="1,2,3,5")
    p.add_argument("--hidden-bytes-list", default="1024,4096,65536")
    p.add_argument("--segments-limits", default="6,9,12,15")
    p.add_argument("--pad-bytes-list", default="0")
    p.add_argument("--g-bytes-list", default="0")
    p.add_argument("--timeout-s", type=float, default=300.0, help="单次 embed POST 超时")
    p.add_argument("--drain-timeout-s", type=float, default=600.0, help="发 1 条后等待 C 收齐的最长时间")
    p.add_argument("--idle-s", type=float, default=15.0)
    p.add_argument("--poll-s", type=float, default=1.0)
    p.add_argument("--extract", default="")
    args = p.parse_args()

    scripts_dir = Path(__file__).resolve().parent
    bench_kb = scripts_dir / "bench_overlay_embed_hls_scheme_b.py"
    if not bench_kb.is_file():
        raise SystemExit(f"missing: {bench_kb}")

    ks = _split_int_list(args.k_list)
    hiddens = _split_int_list(args.hidden_bytes_list)
    seg_limits = _split_int_list(args.segments_limits)
    pads = _split_int_list(args.pad_bytes_list)
    gbytes = _split_int_list(args.g_bytes_list)

    seg_counts = {sl: _segment_count(segments_glob=args.segments, limit=sl) for sl in seg_limits}
    if all(seg_counts[sl] <= 0 for sl in seg_limits):
        raise SystemExit(f"no segments matched glob: {args.segments!r}")
    max_sl = max(seg_limits)
    avail = _segment_count(segments_glob=args.segments, limit=0)
    if avail < max_sl:
        raise SystemExit(
            f"glob {args.segments!r} 只有 {avail} 个分片，矩阵需要 segments_limit 最大 {max_sl}。"
            f"请使用仓库根目录 hls_seg*.ts（≥15 个）或: python scripts/prepare_test_ts_segments.py --count {max_sl}"
        )
    for sl in seg_limits:
        if seg_counts[sl] != sl:
            print(
                f"warning: segments_limit={sl} 实际只加载 {seg_counts[sl]} 片 "
                f"(glob 文件数不足或未按 hls_segNNN 排序)",
                flush=True,
            )

    out_p = Path(str(args.out))
    if out_p.is_absolute():
        out_path = out_p
    elif out_p.parent != Path("."):
        # 相对路径且含子目录：相对仓库根目录（运行 python scripts/... 时的 cwd）
        out_path = Path.cwd() / out_p
    else:
        out_path = scripts_dir / out_p.name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    t_batch0 = time.time()

    total_cells = len(hiddens) * len(seg_limits) * len(pads) * len(gbytes) * len(ks)
    cur = 0

    for hidden_bytes in hiddens:
        for seg_limit in seg_limits:
            for pad_bytes in pads:
                for gb in gbytes:
                    for k in ks:
                        cur += 1
                        n_seg = seg_counts[seg_limit]
                        base = {
                            "matrix": "embed_hls_e2e_single",
                            "hidden_bytes": str(hidden_bytes),
                            "segments_limit": str(seg_limit),
                            "n_segments": str(n_seg),
                            "pad_bytes": str(pad_bytes),
                            "g_bytes": str(gb),
                            "k": str(k),
                            "send_count": "1",
                        }
                        if k > n_seg:
                            rows.append(
                                {
                                    **base,
                                    "rc": "-1",
                                    "note": f"skipped_k_gt_n (k={k} n={n_seg})",
                                }
                            )
                            print(f"[{cur}/{total_cells}] SKIP k>{n_seg} hidden={hidden_bytes} seg={seg_limit}")
                            continue

                        print(
                            f"[{cur}/{total_cells}] RUN hidden={hidden_bytes} seg_limit={seg_limit} "
                            f"k={k} (single message)",
                            flush=True,
                        )
                        cmd = [
                            str(args.python),
                            str(bench_kb),
                            "--segments",
                            str(args.segments),
                            "--segments-limit",
                            str(seg_limit),
                            "--a-url",
                            str(args.a_url),
                            "--c-url",
                            str(args.c_url),
                            "--hidden-bytes",
                            str(hidden_bytes),
                            "--k",
                            str(k),
                            "--pad-bytes",
                            str(pad_bytes),
                            "--send-count",
                            "1",
                            "--concurrency",
                            "1",
                            "--timeout-s",
                            str(float(args.timeout_s)),
                            "--drain-timeout-s",
                            str(float(args.drain_timeout_s)),
                            "--idle-s",
                            str(float(args.idle_s)),
                            "--poll-s",
                            str(float(args.poll_s)),
                        ]
                        if int(gb) > 0:
                            cmd += ["--g-bytes", str(int(gb))]
                        if str(args.extract or "").strip():
                            cmd += ["--extract", str(args.extract).strip()]

                        t0 = time.time()
                        rc, out = _run_one(cmd)
                        dt = time.time() - t0
                        parsed = _parse_bench_output(out)
                        row = {**base, "bench": "bench_overlay_embed_hls_scheme_b.py", "rc": str(rc), "runner_elapsed_s": f"{dt:.3f}"}
                        for key in (
                            "segments",
                            "segments_bytes",
                            "hidden_bytes",
                            "k",
                            "g_bytes",
                            "pad_bytes",
                            "send_count",
                            "elapsed_s",
                            "sent_ok",
                            "sent_err",
                            "recv_count",
                            "unique_msg_ids",
                            "loss",
                            "loss_rate",
                            "e2e_ms_p50",
                            "p95",
                            "p99",
                        ):
                            if key in parsed:
                                row[key] = parsed[key]
                        if parsed.get("e2e_ms_p50"):
                            row["e2e_ms"] = parsed["e2e_ms_p50"]
                        if parsed.get("p95"):
                            row["e2e_ms_p95"] = parsed["p95"]
                        if parsed.get("p99"):
                            row["e2e_ms_p99"] = parsed["p99"]
                        if rc != 0:
                            row["note"] = "bench_failed"
                            print("FAILED:", shlex.join(cmd))
                            print(out[-2500:])
                        else:
                            print(
                                f"  ok e2e_ms={row.get('e2e_ms', '?')} "
                                f"loss={row.get('loss', '?')} runner_s={dt:.1f}",
                                flush=True,
                            )
                        rows.append(row)

    cols: list[str] = []
    for row in rows:
        for k in row:
            if k not in cols:
                cols.append(k)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"wrote {len(rows)} rows -> {out_path}")
    print(f"batch_elapsed_s={time.time() - t_batch0:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
