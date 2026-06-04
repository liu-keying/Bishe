"""
从 bench_matrix_embed_hls.csv 或 bench_matrix_embed_hls_e2e_single.csv 生成论文用时延汇总表。

维度：hidden_bytes × segments_limit × k；多样本时对每格取 e2e p50/p95/p99 均值，单条 e2e 时 n_runs=1。
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with csv_path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if str(row.get("note", "")).startswith("skipped"):
                continue
            try:
                if int(row.get("rc", -1)) != 0:
                    continue
            except ValueError:
                continue
            rows.append(row)
    return rows


def _segments_n(row: dict[str, str]) -> int:
    for key in ("segments_number", "segments_limit"):
        raw = row.get(key)
        if raw is not None and str(raw).strip() != "":
            return int(raw)
    raise KeyError("segments_number/segments_limit")


def _float_or_none(row: dict[str, str], key: str) -> float | None:
    raw = row.get(key)
    if raw is None or str(raw).strip() in ("", "None", "none"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def e2e_groups(rows: list[dict[str, str]]) -> dict[tuple[int, int, int], list[tuple[float, float, float]]]:
    g: dict[tuple[int, int, int], list[tuple[float, float, float]]] = defaultdict(list)
    for row in rows:
        try:
            p50 = _float_or_none(row, "e2e_ms_p50")
            p95 = _float_or_none(row, "p95")
            p99 = _float_or_none(row, "p99")
            if p50 is None or p95 is None or p99 is None:
                continue
            key = (int(row["hidden_bytes"]), _segments_n(row), int(row["k"]))
            g[key].append((p50, p95, p99))
        except (KeyError, ValueError):
            continue
    return g


def _pool_e2e(rows: list[dict[str, str]]) -> list[tuple[float, float, float]]:
    out: list[tuple[float, float, float]] = []
    for row in rows:
        p50 = _float_or_none(row, "e2e_ms_p50")
        p95 = _float_or_none(row, "p95")
        p99 = _float_or_none(row, "p99")
        if p50 is not None and p95 is not None and p99 is not None:
            out.append((p50, p95, p99))
    return out


def write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--csv",
        type=Path,
        default=Path(__file__).resolve().parent / "bench_matrix_embed_hls.csv",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="默认：thesis_latency_tables 或输入为 e2e_single 时用 thesis_latency_tables_e2e_single",
    )
    args = ap.parse_args()

    csv_path = args.csv.resolve()
    if args.out_dir is None:
        if "e2e_single" in csv_path.name:
            args.out_dir = Path(__file__).resolve().parent / "thesis_latency_tables_e2e_single"
        else:
            args.out_dir = Path(__file__).resolve().parent / "thesis_latency_tables"

    rows = load_rows(args.csv)
    all_rows_raw: list[dict[str, str]] = []
    with args.csv.open(encoding="utf-8", newline="") as f:
        all_rows_raw = list(csv.DictReader(f))
    g = e2e_groups(rows)
    if not g:
        raise SystemExit(f"no valid e2e rows in {args.csv}")

    hidden_list = sorted({k[0] for k in g})
    seg_list = sorted({k[1] for k in g})
    k_list = sorted({k[2] for k in g})

    cell: dict[tuple[int, int, int], tuple[float, float, float, int]] = {}
    for key, trips in sorted(g.items()):
        n = len(trips)
        cell[key] = (
            mean(t[0] for t in trips),
            mean(t[1] for t in trips),
            mean(t[2] for t in trips),
            n,
        )

    out = args.out_dir

    # 48 格长表
    full_long: list[list[object]] = []
    for h in hidden_list:
        for sl in seg_list:
            for k in k_list:
                if (h, sl, k) not in cell:
                    continue
                p50, p95, p99, n = cell[(h, sl, k)]
                full_long.append(
                    [h, sl, k, round(p50, 1), round(p95, 1), round(p99, 1), n]
                )
    write_csv(
        out / "latency_full_long.csv",
        ["hidden_bytes_B", "segments_limit", "k", "mean_e2e_ms_p50", "mean_p95_ms", "mean_p99_ms", "n_runs"],
        full_long,
    )

    # 正文主表：固定 hidden，分片数 6/9/12/15，对其余 k 平均 p50/p95
    seg_effect: list[list[object]] = []
    for h in hidden_list:
        base_p50 = base_p95 = None
        row_cells: list[tuple[int, float, float]] = []
        for sl in seg_list:
            pool50: list[float] = []
            pool95: list[float] = []
            for k in k_list:
                if (h, sl, k) not in cell:
                    continue
                pool50.append(cell[(h, sl, k)][0])
                pool95.append(cell[(h, sl, k)][1])
            if not pool50:
                continue
            m50 = mean(pool50)
            m95 = mean(pool95)
            row_cells.append((sl, m50, m95))
        if not row_cells:
            continue
        base_p50 = row_cells[0][1]
        base_p95 = row_cells[0][2]
        for sl, m50, m95 in row_cells:
            delta50 = m50 - base_p50
            pct50 = (delta50 / base_p50 * 100.0) if base_p50 else 0.0
            seg_effect.append(
                [
                    h,
                    sl,
                    round(m50, 1),
                    round(m95, 1),
                    round(delta50, 1),
                    round(pct50, 2),
                ]
            )
    write_csv(
        out / "latency_segments_effect_by_hidden_all_k.csv",
        [
            "hidden_bytes_B",
            "segments_limit",
            "mean_e2e_ms_p50_avg_k",
            "mean_p95_ms_avg_k",
            "delta_p50_vs_seg6_ms",
            "delta_p50_vs_seg6_pct",
        ],
        seg_effect,
    )

    # 宽表：行=分片数，列=三档 hidden 的 p50（对各 k 平均）
    wide_header = ["segments_limit"] + [f"hidden={h} p50_ms" for h in hidden_list]
    wide_rows: list[list[object]] = []
    for sl in seg_list:
        row: list[object] = [sl]
        for h in hidden_list:
            pool = [cell[(h, sl, k)][0] for k in k_list if (h, sl, k) in cell]
            row.append(round(mean(pool), 1) if pool else "")
        wide_rows.append(row)
    write_csv(out / "latency_segments_x_hidden_p50_avg_k.csv", wide_header, wide_rows)

    # 1024B 专表（对应正文「仅改变分片数量」段落）
    h1024_rows: list[list[object]] = []
    pool6_50 = [cell[(1024, 6, k)][0] for k in k_list if (1024, 6, k) in cell]
    base = mean(pool6_50) if pool6_50 else 0.0
    for sl in seg_list:
        pool50 = [cell[(1024, sl, k)][0] for k in k_list if (1024, sl, k) in cell]
        pool95 = [cell[(1024, sl, k)][1] for k in k_list if (1024, sl, k) in cell]
        if not pool50:
            continue
        m50 = mean(pool50)
        h1024_rows.append(
            [
                1024,
                sl,
                round(m50, 1),
                round(mean(pool95), 1),
                round(m50 - base, 1),
                round((m50 - base) / base * 100.0, 2) if base else 0.0,
            ]
        )
    write_csv(
        out / "latency_hidden1024_segments_avg_across_k.csv",
        [
            "hidden_bytes_B",
            "segments_limit",
            "mean_e2e_ms_p50_avg_k",
            "mean_p95_ms_avg_k",
            "delta_p50_vs_seg6_ms",
            "delta_p50_vs_seg6_pct",
        ],
        h1024_rows,
    )

    # seg=6：hidden × k 宽表 p50
    for sl_fixed in (6,):
        hdr = ["hidden_bytes_B\\k"] + [f"k={k}" for k in k_list]
        wrows: list[list[object]] = []
        for h in hidden_list:
            row = [h]
            for k in k_list:
                if (h, sl_fixed, k) in cell:
                    row.append(round(cell[(h, sl_fixed, k)][0], 1))
                else:
                    row.append("")
            wrows.append(row)
        write_csv(out / f"latency_seg{sl_fixed}_hidden_x_k_p50_ms.csv", hdr, wrows)

    # 与吞吐对照：同 seg 下 p50 与吞吐（k 平均），便于 5.2 对比叙述
    tp_g: dict[tuple[int, int, int], list[float]] = defaultdict(list)
    for row in rows:
        try:
            key = (int(row["hidden_bytes"]), _segments_n(row), int(row["k"]))
            tp_g[key].append(float(row["throughput_avg_Bps"]))
        except (KeyError, ValueError):
            continue
    compare: list[list[object]] = []
    for h in hidden_list:
        for sl in seg_list:
            lat50 = [cell[(h, sl, k)][0] for k in k_list if (h, sl, k) in cell]
            tps = [
                mean(tp_g[(h, sl, k)])
                for k in k_list
                if (h, sl, k) in tp_g and tp_g[(h, sl, k)]
            ]
            if not lat50 or not tps:
                continue
            compare.append(
                [
                    h,
                    sl,
                    round(mean(lat50), 1),
                    round(mean(tps), 2),
                ]
            )
    write_csv(
        out / "latency_vs_throughput_by_seg_avg_k.csv",
        ["hidden_bytes_B", "segments_limit", "mean_e2e_ms_p50_avg_k", "mean_throughput_Bps_avg_k"],
        compare,
    )

    # --- 总体 / 分组均值（单条 e2e 矩阵也可用）---
    ok_trips = _pool_e2e(rows)
    sent_ok_total = sum(1 for r in all_rows_raw if str(r.get("sent_ok", "")).strip() == "1")
    sent_err_total = sum(1 for r in all_rows_raw if str(r.get("sent_err", "")).strip() not in ("", "0"))
    overall: list[list[object]] = [
        [
            "all_valid_e2e",
            len(ok_trips),
            len(all_rows_raw),
            sent_ok_total,
            sent_err_total,
            round(mean(t[0] for t in ok_trips), 1) if ok_trips else "",
            round(mean(t[1] for t in ok_trips), 1) if ok_trips else "",
            round(mean(t[2] for t in ok_trips), 1) if ok_trips else "",
            round(stdev(t[0] for t in ok_trips), 1) if len(ok_trips) > 1 else 0.0,
        ]
    ]
    for label, key_idx, key_name in (
        ("by_hidden", 0, "hidden_bytes"),
        ("by_segments_limit", 1, "segments_limit"),
        ("by_k", 2, "k"),
    ):
        buckets: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
        for row in rows:
            try:
                p50 = _float_or_none(row, "e2e_ms_p50")
                p95 = _float_or_none(row, "p95")
                p99 = _float_or_none(row, "p99")
                if p50 is None:
                    continue
                buckets[int(row[key_name])].append((p50, p95 or p50, p99 or p50))
            except (KeyError, ValueError):
                continue
        for bk in sorted(buckets):
            pool = buckets[bk]
            overall.append(
                [
                    label,
                    bk,
                    len(pool),
                    "",
                    "",
                    round(mean(t[0] for t in pool), 1),
                    round(mean(t[1] for t in pool), 1),
                    round(mean(t[2] for t in pool), 1),
                    round(stdev(t[0] for t in pool), 1) if len(pool) > 1 else 0.0,
                ]
            )
    write_csv(
        out / "latency_summary_overall.csv",
        [
            "group",
            "key_or_count",
            "n_e2e",
            "sent_ok_cells",
            "sent_err_cells",
            "mean_p50_ms",
            "mean_p95_ms",
            "mean_p99_ms",
            "stdev_p50_ms",
        ],
        overall,
    )

    failed: list[list[object]] = []
    for row in all_rows_raw:
        p50 = _float_or_none(row, "e2e_ms_p50")
        se = str(row.get("sent_err", "")).strip()
        if p50 is not None and se in ("", "0"):
            continue
        failed.append(
            [
                row.get("hidden_bytes", ""),
                row.get("segments_limit", ""),
                row.get("k", ""),
                row.get("sent_ok", ""),
                row.get("sent_err", ""),
                row.get("loss", ""),
                row.get("note", ""),
            ]
        )
    if failed:
        write_csv(
            out / "latency_failed_cells.csv",
            ["hidden_bytes", "segments_limit", "k", "sent_ok", "sent_err", "loss", "note"],
            failed,
        )

    readme = f"""时延汇总表（来源：{csv_path.name}）

维度
  hidden_bytes_B : 1024, 4096, 65536
  k              : 1, 2, 3, 5
  segments_limit : 6, 9, 12, 15
  每格可为多次重复或单条 send_count=1；表中为各次 e2e_ms_p50 / p95 / p99 的均值（n_runs 见 latency_full_long.csv）

总体均值
  latency_summary_overall.csv — 全表及按 hidden / segments_limit / k 分组的 p50/p95/p99 均值
  latency_failed_cells.csv   — 无有效 e2e 或 sent_err 的格子（若有）

正文推荐（分片数量对时延影响，对其余 k 平均）
  latency_segments_effect_by_hidden_all_k.csv
      三档 hidden × 分片 6/9/12/15，含相对 seg6 的 p50 增量与百分比

  latency_hidden1024_segments_avg_across_k.csv
      仅 1024B：seg6≈22360ms → seg15≈22708ms（数百 ms 级波动）

  latency_segments_x_hidden_p50_avg_k.csv
      宽表：行=分片数，列=三档 hidden 的 p50（ms）

全因子
  latency_full_long.csv — 48 格长表

固定 seg=6
  latency_seg6_hidden_x_k_p50_ms.csv — 与吞吐 seg6 表同构

与 5.2 吞吐对比
  latency_vs_throughput_by_seg_avg_k.csv

指标
  mean_e2e_ms_p50 : C /stats 的 e2e_ms.p50 均值
  mean_p95_ms     : 同上 p95

生成
  python scripts/gen_thesis_latency_tables.py --csv scripts/bench_matrix_embed_hls_e2e_single.csv
"""
    (out / "README.txt").write_text(readme, encoding="utf-8")

    if ok_trips:
        print(
            f"overall: n_e2e={len(ok_trips)} mean_p50={mean(t[0] for t in ok_trips):.1f}ms "
            f"mean_p95={mean(t[1] for t in ok_trips):.1f}ms"
        )
    if failed:
        print(f"failed_cells: {len(failed)} -> {out / 'latency_failed_cells.csv'}")
    print(f"wrote tables under {out}")
    for p in sorted(out.glob("*.csv")):
        print(f"  {p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
