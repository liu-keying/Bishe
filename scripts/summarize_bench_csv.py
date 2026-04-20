from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path


def _to_float(x: str | None) -> float | None:
    if x is None:
        return None
    s = str(x).strip()
    if not s or s.lower() == "none":
        return None
    try:
        return float(s)
    except Exception:
        return None


def _mean(vals: list[float]) -> float | None:
    if not vals:
        return None
    return sum(vals) / len(vals)


def _var(vals: list[float]) -> float | None:
    # sample variance (n-1)
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    return sum((v - m) ** 2 for v in vals) / (n - 1)


def _std(vals: list[float]) -> float | None:
    v = _var(vals)
    return math.sqrt(v) if v is not None else None


def main() -> int:
    p = argparse.ArgumentParser(description="汇总 bench_results.csv -> bench_results_summary.csv（均值/方差/标准差）")
    p.add_argument("--in", dest="inp", default="bench_results.csv", help="输入 CSV（默认 scripts/bench_results.csv）")
    p.add_argument("--out", dest="outp", default="bench_results_summary.csv", help="输出 CSV（默认 scripts/bench_results_summary.csv）")
    p.add_argument("--group-by", default="segments_limit,hidden_bytes", help="分组字段，逗号分隔")
    args = p.parse_args()

    scripts_dir = Path(__file__).resolve().parent
    in_path = (scripts_dir / args.inp).resolve() if not Path(args.inp).is_absolute() else Path(args.inp)
    out_path = (scripts_dir / args.outp).resolve() if not Path(args.outp).is_absolute() else Path(args.outp)

    group_keys = [s.strip() for s in str(args.group_by).split(",") if s.strip()]
    if not group_keys:
        raise SystemExit("group-by is empty")

    metrics = [
        "sent_ok",
        "post_qps",
        "runner_elapsed_s",
        "elapsed_s",
        "e2e_ms_p50",
        "p95",
        "p99",
        "throughput_avg_Bps",
        "loss_rate",
    ]

    groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    with open(in_path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if not row:
                continue
            if str(row.get("rc") or "0").strip() not in ("0", ""):
                continue
            key = tuple(str(row.get(k) or "").strip() for k in group_keys)
            groups[key].append(row)

    out_rows: list[dict[str, str]] = []
    for key, rows in sorted(groups.items(), key=lambda kv: kv[0]):
        base: dict[str, str] = {k: v for k, v in zip(group_keys, key)}
        base["n"] = str(len(rows))
        # pass through some context columns if present
        for ctx in ["mode", "segments_glob", "concurrency", "duration_s", "drain_timeout_s", "idle_s"]:
            v = (rows[0].get(ctx) or "").strip()
            if v:
                base[ctx] = v

        for m in metrics:
            vals = []
            for r0 in rows:
                v = _to_float(r0.get(m))
                if v is not None:
                    vals.append(v)
            mu = _mean(vals)
            va = _var(vals)
            sd = _std(vals)
            if mu is not None:
                base[f"{m}_mean"] = f"{mu:.6g}"
            if sd is not None:
                base[f"{m}_std"] = f"{sd:.6g}"
            if va is not None:
                base[f"{m}_var"] = f"{va:.6g}"
        out_rows.append(base)

    # columns
    cols: list[str] = []
    for row in out_rows:
        for k in row.keys():
            if k not in cols:
                cols.append(k)

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)

    print(f"wrote {len(out_rows)} groups -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

