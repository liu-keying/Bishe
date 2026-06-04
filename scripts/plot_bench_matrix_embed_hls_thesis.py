"""
从 bench_matrix_embed_hls.csv 生成本科论文可用的表与图。

pip 安装 matplotlib 若报错 ValueError: check_hostname requires server_hostname：
  多为系统/环境代理配置异常。可在「当前终端」临时关闭代理后再装，例如 PowerShell：
    $env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:ALL_PROXY=''; pip install matplotlib
  或改用 conda / 手机热点 / 离线 whl。本脚本不装 matplotlib 也会生成四张独立 SVG。
  栅格化已有 SVG 为 PNG：见同目录 thesis_svg_to_png.py（cairosvg / inkscape / magick 任选其一）。
  无 matplotlib 时可用 Inkscape 将 SVG 转为 PDF/EPS（--vector-formats，默认 pdf,eps）。

输出（默认目录为 CSV 所在目录下的 thesis_out/，避免与已在 Excel 中打开的同名文件冲突）：
  - thesis_embed_hls_table1_overall.csv      表1：三档 hidden_bytes 总体均值
  - thesis_embed_hls_table2_seg1024_k1.csv   表2：1024B、k=1 下 segments_number 与吞吐
  - thesis_embed_hls_table3_seg6_by_k.csv    表3：segments_number=6 时各 k、各 hidden 的均值±标准差
  - thesis_embed_hls_fig_1_overall.svg / .png       图1：三档 hidden 总体均值（柱）
  - thesis_embed_hls_fig_2_seg1024_k1.svg / .png    图2：1024B、密文分片数 k=1，HLS 分片数量 segments_number 与吞吐（折线）
  - thesis_embed_hls_fig_3_seg6_lines.svg / .png    图3：seg=6，k 与吞吐折线（三档 hidden）
  - thesis_embed_hls_fig_4_seg6_bars.svg / .png / .pdf / .eps  图4：seg=6，三档 hidden_bytes 与密文分片数 k 的吞吐（柱）
  - 安装 matplotlib 时默认同名输出 PDF+EPS（论文矢量图）；可用 --vector-formats 调整
  - thesis_embed_hls_latency_fig_1_overall.svg     图1：不同隐蔽数据规模下的端到端时延（p50/p95/p99）
  - thesis_embed_hls_latency_fig_2_seg1024_avg.svg 图2：小载荷（1024B）下 HLS 分片数量与端到端时延（对各 k 平均 p50/p95）
  - thesis_embed_hls_latency_fig_3_seg6_p50p95.svg 图3：HLS 分片数=6，k 与 p50（实线）/p95（虚线），三档 hidden（对应 k 段落）
  - thesis_embed_hls_latency_fig_4_seg6_bars.svg   图4：HLS 分片数=6 的 p50 分组柱（可选）
  - thesis_embed_hls_latency_fig_5_tail.svg        图5：三档 hidden 平均 (p95−p50) 尾部间隔（对应尾部讨论）
  - thesis_embed_hls_table_loss*.csv               丢包率：按 hidden / 分片(1024 对 k 平均) / seg=6×k 汇总
  - thesis_embed_hls_loss_fig_1..4.svg/.png      丢包率（%，与吞吐四图同构；CSV 中 loss_rate 为 0~1）
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import shutil
import subprocess
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


def _row_segments_n(row: dict[str, str]) -> int | None:
    """HLS 分片数量；兼容列名 segments_number（新）与 segments_limit（旧 CSV）。"""
    for key in ("segments_number", "segments_limit"):
        raw = row.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return None


def tp_groups(rows: list[dict[str, str]]) -> dict[tuple[int, int, int], list[float]]:
    g: dict[tuple[int, int, int], list[float]] = defaultdict(list)
    for row in rows:
        try:
            sn = _row_segments_n(row)
            if sn is None:
                continue
            key = (int(row["hidden_bytes"]), sn, int(row["k"]))
            g[key].append(float(row["throughput_avg_Bps"]))
        except (KeyError, ValueError):
            continue
    return g


def _esc_xml(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _nice_y_ticks(y_min: float, y_max: float, max_ticks: int = 6) -> list[float]:
    """在 [y_min, y_max] 上生成约 max_ticks 条的「整齐」Y 刻度。"""
    if y_max <= y_min:
        y_max = y_min + 1.0
    span = y_max - y_min
    n = max(max_ticks - 1, 1)
    raw = span / n
    exp = math.floor(math.log10(raw)) if raw > 0 else 0
    base = 10**exp
    fr = raw / base
    if fr <= 1:
        step = base
    elif fr <= 2:
        step = 2 * base
    elif fr <= 5:
        step = 5 * base
    else:
        step = 10 * base
    t0 = math.floor(y_min / step) * step
    out: list[float] = []
    t = t0
    for _ in range(24):
        if t > y_max + step * 0.001:
            break
        if t + 1e-12 >= y_min:
            out.append(float(t))
        t += step
    return out if out else [y_min, y_max]


def _fmt_axis_num(v: float) -> str:
    if abs(v - round(v)) < 0.05 * max(abs(v), 1.0):
        return str(int(round(v)))
    return f"{v:.1f}"


def _svg_axes_frame(
    *,
    plot_left: float,
    plot_right: float,
    plot_top: float,
    plot_bottom: float,
    y_ticks: list[float],
    y_min: float,
    y_max: float,
    x_labels: list[tuple[float, str]],
    x_axis_title: str,
    y_axis_title: str,
    y_tick_suffix: str = "",
) -> list[str]:
    """绘制坐标轴、Y 网格线、Y 刻度文字、X 刻度线与文字。数据曲线应在之后绘制以盖住网格。"""
    lines: list[str] = []
    if y_max <= y_min:
        y_max = y_min + 1.0

    def y_to_py(v: float) -> float:
        return plot_bottom - (v - y_min) / (y_max - y_min) * (plot_bottom - plot_top)

    for tv in y_ticks:
        yy = y_to_py(tv)
        lines.append(
            f'<line x1="{plot_left}" y1="{yy}" x2="{plot_right}" y2="{yy}" stroke="#e8e8e8" stroke-width="1"/>'
        )
    lines.append(
        f'<line x1="{plot_left}" y1="{plot_bottom}" x2="{plot_right}" y2="{plot_bottom}" '
        f'stroke="#222" stroke-width="1.5"/>'
    )
    lines.append(
        f'<line x1="{plot_left}" y1="{plot_bottom}" x2="{plot_left}" y2="{plot_top}" '
        f'stroke="#222" stroke-width="1.5"/>'
    )
    for tv in y_ticks:
        yy = y_to_py(tv)
        lines.append(f'<line x1="{plot_left}" y1="{yy}" x2="{plot_left - 5}" y2="{yy}" stroke="#222" stroke-width="1"/>')
        y_tick_text = _fmt_axis_num(tv) + y_tick_suffix
        lines.append(
            f'<text x="{plot_left - 8}" y="{yy + 4}" text-anchor="end" font-size="10" fill="#222">'
            f"{_esc_xml(y_tick_text)}</text>"
        )
    cx = (plot_left + plot_right) / 2
    lines.append(
        f'<text x="{cx}" y="{plot_bottom + 42}" text-anchor="middle" font-size="11" fill="#222">'
        f"{_esc_xml(x_axis_title)}</text>"
    )
    for xp, lab in x_labels:
        lines.append(f'<line x1="{xp}" y1="{plot_bottom}" x2="{xp}" y2="{plot_bottom + 5}" stroke="#222"/>')
        lines.append(
            f'<text x="{xp}" y="{plot_bottom + 20}" text-anchor="middle" font-size="10" fill="#222">'
            f"{_esc_xml(lab)}</text>"
        )
    cy = (plot_top + plot_bottom) / 2
    lines.append(
        f'<text x="22" y="{cy}" text-anchor="middle" font-size="11" fill="#222" '
        f'transform="rotate(-90 22 {cy})">{_esc_xml(y_axis_title)}</text>'
    )
    return lines


_COLORS = {1024: "#4C72B0", 4096: "#55A868", 65536: "#C44E52"}


def _write_svg_file(path: Path, width: int, height: int, inner: list[str]) -> None:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'font-family="Microsoft YaHei,SimHei,sans-serif" font-size="12">',
        '<rect width="100%" height="100%" fill="#fafafa"/>',
        *inner,
        "</svg>",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = path.open("w", encoding="utf-8")
    except PermissionError as e:
        raise SystemExit(f"无法写入 SVG: {path}\n请关闭占用该文件的程序或换 --out-dir。") from e
    with f:
        f.write("\n".join(parts))


def write_thesis_fig_svgs(
    out_dir: Path,
    *,
    table1: list[list[object]],
    table2: list[list[object]],
    g: dict[tuple[int, int, int], list[float]],
    by_h: dict[int, list[float]],
    ks_sorted: list[int],
) -> list[Path]:
    """生成四张独立 SVG，返回路径列表。"""
    written: list[Path] = []
    colors = _COLORS

    # 图1：总体柱（含 Y 轴、网格、X 轴与分类标签）
    def fig1() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 88}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("不同隐蔽数据规模下的平均吞吐 (4×4 合并统计)")}</text>',
        ]
        hs = [int(r[0]) for r in table1]
        ms = [float(r[1]) for r in table1]
        y_hi_raw = max(ms) * 1.12 if ms else 1.0
        y_ticks = _nice_y_ticks(0.0, y_hi_raw, 6)
        y_hi = max(y_ticks[-1], y_hi_raw, max(ms) * 1.02) if ms else 1.0
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        nbar = max(len(hs), 1)
        slot = (plot_right - plot_left) / nbar
        bw = slot * 0.55
        centers = [plot_left + (i + 0.5) * slot for i in range(nbar)]

        def y_to_py(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        x_pairs = [(centers[i], f"{hs[i]}B") for i in range(len(hs))]
        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=x_pairs,
                x_axis_title="hidden_bytes / B",
                y_axis_title="平均吞吐量 / (B/s)",
                y_tick_suffix=" B/s",
            )
        )
        for i, (h, m) in enumerate(zip(hs, ms)):
            cx = centers[i]
            bx = cx - bw / 2
            bh = y_to_py(0.0) - y_to_py(m)
            yy_top = y_to_py(m)
            col = colors.get(h, "#4C72B0")
            lines.append(
                f'<rect x="{bx}" y="{yy_top}" width="{bw}" height="{bh}" fill="{col}"/>'
                f'<text x="{cx}" y="{yy_top - 6}" text-anchor="middle" font-size="10" fill="#222">{m:.0f}</text>'
            )
        return lines

    p = out_dir / "thesis_embed_hls_fig_1_overall.svg"
    _write_svg_file(p, 580, 460, fig1())
    written.append(p)

    # 图2：折线 1024 k=1（含完整坐标轴与刻度）
    def fig2() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 88}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("小载荷(1024B)、密文分片数 k=1：HLS 分片数量 segments_number 与吞吐量")}</text>',
        ]
        xs = [int(r[2]) for r in table2]
        ys = [float(r[3]) for r in table2]
        if not xs:
            lines.append(f'<text x="{x0}" y="{y0 + 80}">(无数据)</text>')
            return lines
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        y_lo = 0.0
        y_hi_raw = max(ys) * 1.08
        y_ticks = _nice_y_ticks(y_lo, y_hi_raw, 6)
        y_hi = max(y_ticks[-1], y_hi_raw, max(ys) * 1.02)

        if len(xs) <= 1:

            def px(i: int) -> float:
                return (plot_left + plot_right) / 2

        else:

            def px(i: int) -> float:
                return plot_left + (i / (len(xs) - 1)) * (plot_right - plot_left)

        def py_v(v: float) -> float:
            return plot_bottom - (v - y_lo) / (y_hi - y_lo) * (plot_bottom - plot_top)

        x_label_pairs = [(px(i), str(xv)) for i, xv in enumerate(xs)]
        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=y_lo,
                y_max=y_hi,
                x_labels=x_label_pairs,
                x_axis_title="HLS 分片数量 segments_number / 个",
                y_axis_title="平均吞吐量 / (B/s)",
                y_tick_suffix=" B/s",
            )
        )
        pts = " ".join(f"{px(i)},{py_v(v)}" for i, v in enumerate(ys))
        lines.append(f'<polyline fill="none" stroke="#4C72B0" stroke-width="2.5" points="{pts}"/>')
        for i, yv in enumerate(ys):
            lines.append(f'<circle cx="{px(i)}" cy="{py_v(yv)}" r="5" fill="#4C72B0" stroke="white" stroke-width="1"/>')
        return lines

    p = out_dir / "thesis_embed_hls_fig_2_seg1024_k1.svg"
    _write_svg_file(p, 580, 460, fig2())
    written.append(p)

    # 图3：seg=6 折线（含坐标轴）
    def fig3() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 100}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("固定 segments_number=6：密文分片数 k 与吞吐（折线）")}</text>',
        ]
        mx_raw = 1.0
        for h in sorted(by_h.keys()):
            for k in ks_sorted:
                vals = g.get((h, 6, k), [])
                if vals:
                    mx_raw = max(mx_raw, mean(vals))
        mx_raw *= 1.08
        y_ticks = _nice_y_ticks(0.0, mx_raw, 6)
        y_hi = max(y_ticks[-1], mx_raw)
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        if len(ks_sorted) <= 1:
            xmap = {ks_sorted[0]: (plot_left + plot_right) / 2} if ks_sorted else {}
        else:
            xmap = {
                k: plot_left + i * ((plot_right - plot_left) / (len(ks_sorted) - 1))
                for i, k in enumerate(ks_sorted)
            }

        def py_v(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        x_pairs = [(xmap[k], str(k)) for k in ks_sorted]
        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=x_pairs,
                x_axis_title="密文分片数 k / 个",
                y_axis_title="平均吞吐量 / (B/s)",
                y_tick_suffix=" B/s",
            )
        )
        for h in sorted(by_h.keys()):
            pts: list[str] = []
            for k in ks_sorted:
                vals = g.get((h, 6, k), [])
                if not vals:
                    continue
                m = mean(vals)
                pxk = xmap[k]
                pyv = py_v(m)
                pts.append(f"{pxk},{pyv}")
            if len(pts) >= 2:
                col = colors.get(h, "#333")
                lines.append(f'<polyline fill="none" stroke="{col}" stroke-width="2.5" points="{" ".join(pts)}"/>')
            for k in ks_sorted:
                vals = g.get((h, 6, k), [])
                if vals:
                    m = mean(vals)
                    lines.append(
                        f'<circle cx="{xmap[k]}" cy="{py_v(m)}" r="4" fill="{colors.get(h, "#333")}" '
                        f'stroke="white" stroke-width="1"/>'
                    )
        lx = plot_left
        ly = plot_bottom + 58
        for idx, h in enumerate(sorted(by_h.keys())):
            lines.append(
                f'<rect x="{lx + idx * 88}" y="{ly}" width="10" height="10" fill="{colors.get(h, "#888")}"/>'
                f'<text x="{lx + idx * 88 + 14}" y="{ly + 9}" font-size="10" fill="#555">{h} B</text>'
            )
        return lines

    p = out_dir / "thesis_embed_hls_fig_3_seg6_lines.svg"
    _write_svg_file(p, 580, 480, fig3())
    written.append(p)

    # 图4：分组柱（含坐标轴）
    def fig4() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 100}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("固定segments_number=6：三档hidden_bytes下密文分片数与平均吞吐量的关系")}</text>',
        ]
        ys_all = []
        for h in sorted(by_h.keys()):
            for k in ks_sorted:
                vals = g.get((h, 6, k), [])
                if vals:
                    ys_all.append(mean(vals))
        mx_raw = max(ys_all) * 1.08 if ys_all else 1.0
        y_ticks = _nice_y_ticks(0.0, mx_raw, 6)
        y_hi = max(y_ticks[-1], mx_raw)
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        nk = len(ks_sorted)
        hs_sorted = sorted(by_h.keys())
        group_span = (plot_right - plot_left) / max(nk, 1)
        bar_w = group_span / 3.8

        def py_v(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        x_centers = [plot_left + (j + 0.5) * group_span for j in range(nk)]
        x_pairs = [(x_centers[j], str(ks_sorted[j])) for j in range(nk)]
        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=x_pairs,
                x_axis_title="密文分片数 k / 个",
                y_axis_title="平均吞吐量 / (B/s)",
                y_tick_suffix=" B/s",
            )
        )
        for j, k in enumerate(ks_sorted):
            gx0 = plot_left + j * group_span + group_span * 0.18
            for idx, h in enumerate(hs_sorted):
                vals = g.get((h, 6, k), [])
                m = mean(vals) if vals else 0.0
                bh = py_v(0.0) - py_v(m)
                yy_top = py_v(m)
                bx = gx0 + idx * (bar_w + 3)
                lines.append(
                    f'<rect x="{bx}" y="{yy_top}" width="{bar_w}" height="{bh}" fill="{colors.get(h, "#888")}"/>'
                )
        lx = plot_left
        ly = plot_bottom + 58
        for idx, h in enumerate(hs_sorted):
            lines.append(
                f'<rect x="{lx + idx * 88}" y="{ly}" width="10" height="10" fill="{colors.get(h, "#888")}"/>'
                f'<text x="{lx + idx * 88 + 14}" y="{ly + 9}" font-size="10" fill="#555">{h} B</text>'
            )
        return lines

    p = out_dir / "thesis_embed_hls_fig_4_seg6_bars.svg"
    _write_svg_file(p, 580, 480, fig4())
    written.append(p)

    return written


def loss_rate_groups(rows: list[dict[str, str]]) -> dict[tuple[int, int, int], list[float]]:
    """(hidden_bytes, segments_n, k) -> 各次重复的 loss_rate（0~1，C 端 drain 后 expected−unique）。"""
    g: dict[tuple[int, int, int], list[float]] = defaultdict(list)
    for row in rows:
        raw = row.get("loss_rate")
        if raw is None or str(raw).strip() == "":
            continue
        try:
            sn = _row_segments_n(row)
            if sn is None:
                continue
            key = (int(row["hidden_bytes"]), sn, int(row["k"]))
            g[key].append(float(raw))
        except (KeyError, ValueError):
            continue
    return g


def _loss_pct_axis_max(values_pct: list[float]) -> tuple[float, list[float]]:
    """纵轴为丢包率 % 时的上界与刻度；全为 0 时仍给出可读刻度。"""
    mx = max(values_pct) if values_pct else 0.0
    if mx <= 0:
        return 0.01, [0.0, 0.01]
    y_hi_raw = mx * 1.12
    y_ticks = _nice_y_ticks(0.0, y_hi_raw, 6)
    return max(y_ticks[-1], y_hi_raw), y_ticks


def write_thesis_loss_fig_svgs(
    out_dir: Path,
    *,
    g_loss: dict[tuple[int, int, int], list[float]],
    by_h_loss: dict[int, list[float]],
    ks_sorted: list[int],
) -> list[Path]:
    """丢包率四张 SVG，纵轴为百分比（%），与吞吐图版式对应。"""
    written: list[Path] = []
    colors = _COLORS
    hs_sorted = sorted(by_h_loss.keys())

    # 图1：各档 hidden 合并样本的平均丢包率
    def loss_fig1() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 100}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("不同隐蔽数据规模下的平均丢包率（%，合并统计）")}</text>',
        ]
        if not hs_sorted:
            lines.append(f'<text x="{x0}" y="{y0 + 80}">(无数据)</text>')
            return lines
        ms_pct = [mean(by_h_loss[h]) * 100.0 for h in hs_sorted]
        y_hi, y_ticks = _loss_pct_axis_max(ms_pct)
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        nbar = len(hs_sorted)
        slot = (plot_right - plot_left) / max(nbar, 1)
        bw = slot * 0.55
        centers = [plot_left + (i + 0.5) * slot for i in range(nbar)]

        def y_to_py(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        x_pairs = [(centers[i], f"{hs_sorted[i]}B") for i in range(nbar)]
        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=x_pairs,
                x_axis_title="hidden_bytes / B",
                y_axis_title="丢包率 / (%)",
                y_tick_suffix=" %",
            )
        )
        for i, h in enumerate(hs_sorted):
            m = ms_pct[i]
            cx = centers[i]
            bx = cx - bw / 2
            bh = y_to_py(0.0) - y_to_py(m)
            yy_top = y_to_py(m)
            col = colors.get(h, "#4C72B0")
            lines.append(
                f'<rect x="{bx}" y="{yy_top}" width="{bw}" height="{bh}" fill="{col}"/>'
                f'<text x="{cx}" y="{yy_top - 6}" text-anchor="middle" font-size="10" fill="#222">{m:.4f}</text>'
            )
        return lines

    p = out_dir / "thesis_embed_hls_loss_fig_1_overall.svg"
    _write_svg_file(p, 580, 460, loss_fig1())
    written.append(p)

    # 图2：1024、各分片数，对 k 平均后的平均丢包率
    def loss_fig2() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 88}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("小载荷(1024B)：HLS 分片数量 segments_number 与丢包率（对各 k 平均，%）")}</text>',
        ]
        xs: list[int] = []
        ys_pct: list[float] = []
        for sl in sorted({k[1] for k in g_loss if k[0] == 1024}):
            pool: list[float] = []
            for (h, s, _), vals in g_loss.items():
                if h == 1024 and s == sl:
                    pool.extend(vals)
            if not pool:
                continue
            xs.append(sl)
            ys_pct.append(mean(pool) * 100.0)
        if not xs:
            lines.append(f'<text x="{x0}" y="{y0 + 80}">(无数据)</text>')
            return lines
        y_hi, y_ticks = _loss_pct_axis_max(ys_pct)
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        if len(xs) <= 1:

            def px(i: int) -> float:
                return (plot_left + plot_right) / 2

        else:

            def px(i: int) -> float:
                return plot_left + (i / (len(xs) - 1)) * (plot_right - plot_left)

        def py_v(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        x_label_pairs = [(px(i), str(xv)) for i, xv in enumerate(xs)]
        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=x_label_pairs,
                x_axis_title="HLS 分片数量 segments_number / 个",
                y_axis_title="丢包率 / (%)",
                y_tick_suffix=" %",
            )
        )
        pts = " ".join(f"{px(i)},{py_v(v)}" for i, v in enumerate(ys_pct))
        lines.append(f'<polyline fill="none" stroke="#C44E52" stroke-width="2.5" points="{pts}"/>')
        for i, yv in enumerate(ys_pct):
            lines.append(f'<circle cx="{px(i)}" cy="{py_v(yv)}" r="5" fill="#C44E52" stroke="white" stroke-width="1"/>')
        return lines

    p = out_dir / "thesis_embed_hls_loss_fig_2_seg1024_avg.svg"
    _write_svg_file(p, 580, 460, loss_fig2())
    written.append(p)

    # 图3：seg=6，各 hidden 的平均丢包率随 k
    def loss_fig3() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 100}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("固定HLS分片数=6：密文分片数 k 与平均丢包率（%，分档）")}</text>',
        ]
        ys_all_pct: list[float] = []
        for h in hs_sorted:
            for k in ks_sorted:
                vals = g_loss.get((h, 6, k), [])
                if vals:
                    ys_all_pct.append(mean(vals) * 100.0)
        y_hi, y_ticks = _loss_pct_axis_max(ys_all_pct)
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        if len(ks_sorted) <= 1:
            xmap = {ks_sorted[0]: (plot_left + plot_right) / 2} if ks_sorted else {}
        else:
            xmap = {
                k: plot_left + i * ((plot_right - plot_left) / (len(ks_sorted) - 1))
                for i, k in enumerate(ks_sorted)
            }

        def py_v(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        x_pairs = [(xmap[k], str(k)) for k in ks_sorted]
        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=x_pairs,
                x_axis_title="密文分片数 k / 个",
                y_axis_title="丢包率 / (%)",
                y_tick_suffix=" %",
            )
        )
        for h in hs_sorted:
            pts: list[str] = []
            for k in ks_sorted:
                vals = g_loss.get((h, 6, k), [])
                if not vals:
                    continue
                m_pct = mean(vals) * 100.0
                pts.append(f"{xmap[k]},{py_v(m_pct)}")
            if len(pts) >= 2:
                col = colors.get(h, "#333")
                lines.append(f'<polyline fill="none" stroke="{col}" stroke-width="2.5" points="{" ".join(pts)}"/>')
            for k in ks_sorted:
                vals = g_loss.get((h, 6, k), [])
                if vals:
                    m_pct = mean(vals) * 100.0
                    lines.append(
                        f'<circle cx="{xmap[k]}" cy="{py_v(m_pct)}" r="4" fill="{colors.get(h, "#333")}" '
                        f'stroke="white" stroke-width="1"/>'
                    )
        lx = plot_left
        ly = plot_bottom + 58
        for idx, h in enumerate(hs_sorted):
            lines.append(
                f'<rect x="{lx + idx * 88}" y="{ly}" width="10" height="10" fill="{colors.get(h, "#888")}"/>'
                f'<text x="{lx + idx * 88 + 14}" y="{ly + 9}" font-size="10" fill="#555">{h} B</text>'
            )
        return lines

    p = out_dir / "thesis_embed_hls_loss_fig_3_seg6_lines.svg"
    _write_svg_file(p, 580, 480, loss_fig3())
    written.append(p)

    # 图4：seg=6 分组柱
    def loss_fig4() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 100}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("固定HLS分片数=6：平均丢包率对比（%）")}</text>',
        ]
        ys_all_pct = []
        for h in hs_sorted:
            for k in ks_sorted:
                vals = g_loss.get((h, 6, k), [])
                if vals:
                    ys_all_pct.append(mean(vals) * 100.0)
        y_hi, y_ticks = _loss_pct_axis_max(ys_all_pct)
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        nk = len(ks_sorted)
        group_span = (plot_right - plot_left) / max(nk, 1)
        bar_w = group_span / 3.8

        def py_v(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        x_centers = [plot_left + (j + 0.5) * group_span for j in range(nk)]
        x_pairs = [(x_centers[j], str(ks_sorted[j])) for j in range(nk)]
        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=x_pairs,
                x_axis_title="密文分片数 k / 个",
                y_axis_title="丢包率 / (%)",
                y_tick_suffix=" %",
            )
        )
        for j, k in enumerate(ks_sorted):
            gx0 = plot_left + j * group_span + group_span * 0.18
            for idx, h in enumerate(hs_sorted):
                vals = g_loss.get((h, 6, k), [])
                m_pct = mean(vals) * 100.0 if vals else 0.0
                bh = py_v(0.0) - py_v(m_pct)
                yy_top = py_v(m_pct)
                bx = gx0 + idx * (bar_w + 3)
                lines.append(
                    f'<rect x="{bx}" y="{yy_top}" width="{bar_w}" height="{bh}" fill="{colors.get(h, "#888")}"/>'
                )
        lx = plot_left
        ly = plot_bottom + 58
        for idx, h in enumerate(hs_sorted):
            lines.append(
                f'<rect x="{lx + idx * 88}" y="{ly}" width="10" height="10" fill="{colors.get(h, "#888")}"/>'
                f'<text x="{lx + idx * 88 + 14}" y="{ly + 9}" font-size="10" fill="#555">{h} B</text>'
            )
        return lines

    p = out_dir / "thesis_embed_hls_loss_fig_4_seg6_bars.svg"
    _write_svg_file(p, 580, 480, loss_fig4())
    written.append(p)

    return written


def e2e_groups(rows: list[dict[str, str]]) -> dict[tuple[int, int, int], list[tuple[float, float, float]]]:
    """(hidden_bytes, segments_n, k) -> 多次重复的 (p50, p95, p99) ms。"""
    g: dict[tuple[int, int, int], list[tuple[float, float, float]]] = defaultdict(list)
    for row in rows:
        try:
            sn = _row_segments_n(row)
            if sn is None:
                continue
            key = (int(row["hidden_bytes"]), sn, int(row["k"]))
            p50 = float(row["e2e_ms_p50"])
            p95 = float(row["p95"])
            p99 = float(row["p99"])
            g[key].append((p50, p95, p99))
        except (KeyError, ValueError):
            continue
    return g


def write_thesis_latency_fig_svgs(
    out_dir: Path,
    *,
    rows: list[dict[str, str]],
    g_e2e: dict[tuple[int, int, int], list[tuple[float, float, float]]],
    by_h_keys: list[int],
    ks_sorted: list[int],
) -> list[Path]:
    """端到端时延五张 SVG：与吞吐图同构，纵轴为 ms。"""
    written: list[Path] = []
    colors = _COLORS
    metric_colors = {"p50": "#4C72B0", "p95": "#DD8452", "p99": "#937860"}

    # 按 hidden 池化 (p50,p95,p99) 各取平均
    by_h_lat: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    for (h, _, _), triples in g_e2e.items():
        by_h_lat[h].extend(triples)

    # 图1：每档 hidden 三根柱 p50 / p95 / p99（全局平均）
    def lat_fig1() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 100}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("不同隐蔽数据规模下的端到端时延")}</text>',
        ]
        hs = sorted(by_h_lat.keys())
        if not hs:
            lines.append(f'<text x="{x0}" y="{y0 + 80}">(无数据)</text>')
            return lines
        m50s = [mean(t[0] for t in by_h_lat[h]) for h in hs]
        m95s = [mean(t[1] for t in by_h_lat[h]) for h in hs]
        m99s = [mean(t[2] for t in by_h_lat[h]) for h in hs]
        y_hi_raw = max(m50s + m95s + m99s) * 1.08
        y_ticks = _nice_y_ticks(0.0, y_hi_raw, 6)
        y_hi = max(y_ticks[-1], y_hi_raw)
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        n = len(hs)
        group_w = (plot_right - plot_left) / max(n, 1)
        bar_w = group_w / 4.0

        def py_v(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=[(plot_left + (i + 0.5) * group_w, f"{hs[i]}B") for i in range(n)],
                x_axis_title="hidden_bytes / B",
                y_axis_title="时延 / (ms)",
                y_tick_suffix=" ms",
            )
        )
        labels_m = [("p50", m50s), ("p95", m95s), ("p99", m99s)]
        for i, h in enumerate(hs):
            gx0 = plot_left + i * group_w + group_w * 0.12
            for j, (tag, arr) in enumerate(labels_m):
                m = arr[i]
                bh = py_v(0.0) - py_v(m)
                yy_top = py_v(m)
                bx = gx0 + j * (bar_w + 2)
                col = metric_colors[tag]
                lines.append(f'<rect x="{bx}" y="{yy_top}" width="{bar_w}" height="{bh}" fill="{col}"/>')
        lx = plot_left
        ly = plot_bottom + 58
        for j, tag in enumerate(["p50", "p95", "p99"]):
            lines.append(
                f'<rect x="{lx + j * 72}" y="{ly}" width="10" height="10" fill="{metric_colors[tag]}"/>'
                f'<text x="{lx + j * 72 + 14}" y="{ly + 9}" font-size="10" fill="#555">{tag}</text>'
            )
        return lines

    p = out_dir / "thesis_embed_hls_latency_fig_1_overall.svg"
    _write_svg_file(p, 620, 480, lat_fig1())
    written.append(p)

    # 图2：1024、各分片数；对每个分片数下所有 k 的样本取平均 p50/p95（与正文「对其余参数平均」一致）
    def lat_fig2() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 100}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("小载荷（1024B）：HLS分片数量与端到端时延")}</text>',
        ]
        xs: list[int] = []
        ys50: list[float] = []
        ys95: list[float] = []
        for sl in sorted({k[1] for k in g_e2e if k[0] == 1024}):
            trips_all: list[tuple[float, float, float]] = []
            for (h, s, _), trip in g_e2e.items():
                if h == 1024 and s == sl:
                    trips_all.extend(trip)
            if not trips_all:
                continue
            xs.append(sl)
            ys50.append(mean(t[0] for t in trips_all))
            ys95.append(mean(t[1] for t in trips_all))
        if not xs:
            lines.append(f'<text x="{x0}" y="{y0 + 80}">(无数据)</text>')
            return lines
        y_hi_raw = max(ys50 + ys95) * 1.08
        y_ticks = _nice_y_ticks(0.0, y_hi_raw, 6)
        y_hi = max(y_ticks[-1], y_hi_raw)
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        if len(xs) <= 1:

            def px(i: int) -> float:
                return (plot_left + plot_right) / 2

        else:

            def px(i: int) -> float:
                return plot_left + (i / (len(xs) - 1)) * (plot_right - plot_left)

        def py_v(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        x_pairs = [(px(i), str(xs[i])) for i in range(len(xs))]
        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=x_pairs,
                x_axis_title="HLS 分片数量 segments_number / 个",
                y_axis_title="时延 / (ms)",
                y_tick_suffix=" ms",
            )
        )
        pts50 = " ".join(f"{px(i)},{py_v(ys50[i])}" for i in range(len(xs)))
        pts95 = " ".join(f"{px(i)},{py_v(ys95[i])}" for i in range(len(xs)))
        lines.append(f'<polyline fill="none" stroke="{metric_colors["p50"]}" stroke-width="2.5" points="{pts50}"/>')
        lines.append(
            f'<polyline fill="none" stroke="{metric_colors["p95"]}" stroke-width="2" '
            f'stroke-dasharray="6,4" points="{pts95}"/>'
        )
        for i in range(len(xs)):
            lines.append(
                f'<circle cx="{px(i)}" cy="{py_v(ys50[i])}" r="4" fill="{metric_colors["p50"]}" stroke="white" stroke-width="1"/>'
            )
        lx = plot_left
        ly = plot_bottom + 58
        lines.append(
            f'<line x1="{lx}" y1="{ly}" x2="{lx+28}" y2="{ly}" stroke="{metric_colors["p50"]}" stroke-width="3"/>'
            f'<text x="{lx+32}" y="{ly+4}" font-size="10">p50</text>'
            f'<line x1="{lx+72}" y1="{ly}" x2="{lx+100}" y2="{ly}" stroke="{metric_colors["p95"]}" stroke-width="2" stroke-dasharray="4,3"/>'
            f'<text x="{lx+104}" y="{ly+4}" font-size="10">p95</text>'
        )
        return lines

    p = out_dir / "thesis_embed_hls_latency_fig_2_seg1024_avg.svg"
    _write_svg_file(p, 620, 480, lat_fig2())
    written.append(p)

    # 图3：seg=6，各 hidden 的 p50（实线）与 p95（虚线）随 k 变化
    def lat_fig3() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 116}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("固定HLS分片数=6：密文分片数 k 与 p50（实线）/ p95（虚线）")}</text>',
        ]
        mx_raw = 1.0
        for h in sorted(by_h_keys):
            for k in ks_sorted:
                trip = g_e2e.get((h, 6, k), [])
                if trip:
                    mx_raw = max(mx_raw, mean(t[0] for t in trip), mean(t[1] for t in trip))
        mx_raw *= 1.08
        y_ticks = _nice_y_ticks(0.0, mx_raw, 6)
        y_hi = max(y_ticks[-1], mx_raw)
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        if len(ks_sorted) <= 1:
            xmap = {ks_sorted[0]: (plot_left + plot_right) / 2} if ks_sorted else {}
        else:
            xmap = {
                k: plot_left + i * ((plot_right - plot_left) / (len(ks_sorted) - 1))
                for i, k in enumerate(ks_sorted)
            }

        def py_v(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        x_pairs = [(xmap[k], str(k)) for k in ks_sorted]
        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=x_pairs,
                x_axis_title="密文分片数 k / 个",
                y_axis_title="时延 / (ms)",
                y_tick_suffix=" ms",
            )
        )
        for h in sorted(by_h_keys):
            col = colors.get(h, "#333")
            pts50: list[str] = []
            pts95: list[str] = []
            for k in ks_sorted:
                trip = g_e2e.get((h, 6, k), [])
                if not trip:
                    continue
                m50 = mean(t[0] for t in trip)
                m95 = mean(t[1] for t in trip)
                pts50.append(f"{xmap[k]},{py_v(m50)}")
                pts95.append(f"{xmap[k]},{py_v(m95)}")
            if len(pts50) >= 2:
                lines.append(
                    f'<polyline fill="none" stroke="{col}" stroke-width="2.5" points="{" ".join(pts50)}"/>'
                )
                lines.append(
                    f'<polyline fill="none" stroke="{col}" stroke-width="2" stroke-dasharray="7,5" '
                    f'opacity="0.85" points="{" ".join(pts95)}"/>'
                )
            for k in ks_sorted:
                trip = g_e2e.get((h, 6, k), [])
                if trip:
                    m50 = mean(t[0] for t in trip)
                    lines.append(
                        f'<circle cx="{xmap[k]}" cy="{py_v(m50)}" r="4" fill="{col}" stroke="white" stroke-width="1"/>'
                    )
        lx = plot_left
        ly = plot_bottom + 58
        for idx, h in enumerate(sorted(by_h_keys)):
            lines.append(
                f'<rect x="{lx + idx * 88}" y="{ly}" width="10" height="10" fill="{colors.get(h, "#888")}"/>'
                f'<text x="{lx + idx * 88 + 14}" y="{ly + 9}" font-size="11" font-weight="600" fill="#222">{h} B</text>'
            )
        lines.append(
            f'<text x="{lx}" y="{ly + 22}" font-size="11" font-weight="600" fill="#111">'
            f'{_esc_xml("说明：同色为同档 hidden_bytes；粗实线 p50，虚线 p95。")}</text>'
        )
        return lines

    p = out_dir / "thesis_embed_hls_latency_fig_3_seg6_p50p95.svg"
    _write_svg_file(p, 620, 486, lat_fig3())
    written.append(p)

    # 图4：seg=6，分组柱 p50
    def lat_fig4() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 100}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("固定HLS分片数=6：p50 时延对比")}</text>',
        ]
        ys_all: list[float] = []
        hs_sorted = sorted(by_h_keys)
        for h in hs_sorted:
            for k in ks_sorted:
                trip = g_e2e.get((h, 6, k), [])
                if trip:
                    ys_all.append(mean(t[0] for t in trip))
        mx_raw = max(ys_all) * 1.08 if ys_all else 1.0
        y_ticks = _nice_y_ticks(0.0, mx_raw, 6)
        y_hi = max(y_ticks[-1], mx_raw)
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        nk = len(ks_sorted)
        group_span = (plot_right - plot_left) / max(nk, 1)
        bar_w = group_span / 3.8

        def py_v(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        x_centers = [plot_left + (j + 0.5) * group_span for j in range(nk)]
        x_pairs = [(x_centers[j], str(ks_sorted[j])) for j in range(nk)]
        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=x_pairs,
                x_axis_title="密文分片数 k / 个",
                y_axis_title="p50 时延 / (ms)",
                y_tick_suffix=" ms",
            )
        )
        for j, k in enumerate(ks_sorted):
            gx0 = plot_left + j * group_span + group_span * 0.18
            for idx, h in enumerate(hs_sorted):
                trip = g_e2e.get((h, 6, k), [])
                m = mean(t[0] for t in trip) if trip else 0.0
                bh = py_v(0.0) - py_v(m)
                yy_top = py_v(m)
                bx = gx0 + idx * (bar_w + 3)
                lines.append(
                    f'<rect x="{bx}" y="{yy_top}" width="{bar_w}" height="{bh}" fill="{colors.get(h, "#888")}"/>'
                )
        lx = plot_left
        ly = plot_bottom + 58
        for idx, h in enumerate(hs_sorted):
            lines.append(
                f'<rect x="{lx + idx * 88}" y="{ly}" width="10" height="10" fill="{colors.get(h, "#888")}"/>'
                f'<text x="{lx + idx * 88 + 14}" y="{ly + 9}" font-size="10" fill="#555">{h} B</text>'
            )
        return lines

    p = out_dir / "thesis_embed_hls_latency_fig_4_seg6_bars.svg"
    _write_svg_file(p, 620, 480, lat_fig4())
    written.append(p)

    # 图5：各档 hidden 下，所有配置样本的平均 (p95−p50)，刻画尾部相对中位的抬高
    def lat_fig5() -> list[str]:
        x0, y0, iw, ih = 50, 70, 480, 300
        lines = [
            f'<rect x="{x0 - 20}" y="{y0 - 40}" width="{iw + 40}" height="{ih + 100}" fill="white" stroke="#ccc"/>',
            f'<text x="{x0}" y="{y0 - 50}" font-size="14" font-weight="bold">'
            f'{_esc_xml("各档 hidden 下平均尾部间隔 (p95−p50)")}</text>',
        ]
        hs = sorted(by_h_lat.keys())
        if not hs:
            lines.append(f'<text x="{x0}" y="{y0 + 80}">(无数据)</text>')
            return lines
        gaps = [mean(t[1] - t[0] for t in by_h_lat[h]) for h in hs]
        y_hi_raw = max(gaps) * 1.12 if gaps else 1.0
        y_ticks = _nice_y_ticks(0.0, y_hi_raw, 6)
        y_hi = max(y_ticks[-1], y_hi_raw)
        plot_left = 95.0
        plot_right = x0 + iw - 15.0
        plot_top = y0 + 32.0
        plot_bottom = y0 + ih - 52.0
        n = len(hs)
        group_w = (plot_right - plot_left) / max(n, 1)
        bar_w = group_w * 0.42

        def py_v(v: float) -> float:
            return plot_bottom - (v - 0.0) / (y_hi - 0.0) * (plot_bottom - plot_top)

        lines.extend(
            _svg_axes_frame(
                plot_left=plot_left,
                plot_right=plot_right,
                plot_top=plot_top,
                plot_bottom=plot_bottom,
                y_ticks=y_ticks,
                y_min=0.0,
                y_max=y_hi,
                x_labels=[(plot_left + (i + 0.5) * group_w, f"{hs[i]}B") for i in range(n)],
                x_axis_title="hidden_bytes / B",
                y_axis_title="平均 (p95−p50) / (ms)",
                y_tick_suffix=" ms",
            )
        )
        for i, h in enumerate(hs):
            g = gaps[i]
            cx = plot_left + (i + 0.5) * group_w
            bx = cx - bar_w / 2
            bh = py_v(0.0) - py_v(g)
            yy_top = py_v(g)
            lines.append(
                f'<rect x="{bx}" y="{yy_top}" width="{bar_w}" height="{bh}" fill="{colors.get(h, "#4C72B0")}"/>'
            )
        lines.append(
            f'<text x="{plot_left}" y="{plot_bottom + 58}" font-size="9" fill="#666">'
            f'{_esc_xml("对每档 hidden 合并所有 (segments_number, 密文分片数 k) 重复样本后，逐条算 p95−p50 再取平均")}</text>'
        )
        return lines

    p = out_dir / "thesis_embed_hls_latency_fig_5_tail.svg"
    _write_svg_file(p, 620, 500, lat_fig5())
    written.append(p)

    return written


def write_csv(path: Path, header: list[str], data: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = path.open("w", newline="", encoding="utf-8-sig")
    except PermissionError as e:
        raise SystemExit(
            f"无法写入（文件被占用或无权写入）: {path}\n"
            "请先关闭 Excel/记事本等正在打开该文件的程序，或改用:\n"
            f'  python {Path(__file__).name} --out-dir "%TEMP%\\\\thesis_embed_hls_out"'
        ) from e
    with f:
        w = csv.writer(f)
        w.writerow(header)
        for row in data:
            w.writerow(row)


def try_export_thesis_svg_to_vector(out_dir: Path, *, formats: list[str]) -> None:
    """无 matplotlib 时，用 Inkscape 将 thesis_embed_hls*.svg 转为 PDF/EPS。"""
    want = [f for f in formats if f in ("pdf", "eps")]
    if not want:
        return
    exe = shutil.which("inkscape")
    if not exe:
        print(
            "未从 SVG 导出 PDF/EPS（无 inkscape）。"
            "请安装 Inkscape 并加入 PATH，或安装 matplotlib 后重跑本脚本。"
        )
        return
    svgs = sorted(out_dir.glob("thesis_embed_hls*.svg"))
    if not svgs:
        return
    written: list[Path] = []
    for svg in svgs:
        for fmt in want:
            out = svg.with_suffix(f".{fmt}")
            cmd = [exe, str(svg), f"--export-type={fmt}", f"--export-filename={out}"]
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as e:
                print(f"(optional) SVG→{fmt} failed for {svg.name}: {e.stderr or e}")
                continue
            written.append(out)
            print(f"wrote {out}")
    if written:
        print(f"thesis SVG→vector (inkscape): {len(written)} file(s)")


def try_export_thesis_svg_to_png(out_dir: Path, *, scale: float = 2.0) -> None:
    """若本机有 cairosvg / inkscape / magick，将 thesis_embed_hls*.svg 栅格化为同名 PNG。"""
    conv_path = Path(__file__).resolve().parent / "thesis_svg_to_png.py"
    if not conv_path.is_file():
        return
    try:
        spec = importlib.util.spec_from_file_location("_thesis_svg_to_png", conv_path)
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        written, backend = mod.export_svgs_to_png(out_dir, scale=scale)
        print(f"thesis SVG→PNG ({backend}): {len(written)} file(s)")
    except RuntimeError:
        print(
            "未从 SVG 导出 PNG（未装 cairosvg 且无 inkscape/magick）。"
            "可稍后执行: python scripts/thesis_svg_to_png.py --dir <输出目录>"
        )
    except Exception as e:
        print(f"(optional) thesis SVG→PNG skipped: {e}")


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
        help="输出目录；默认使用 <csv 所在目录>/thesis_out/，减少 PermissionError（勿与已打开文件同路径）。",
    )
    ap.add_argument(
        "--vector-formats",
        default="pdf,eps",
        help="matplotlib 矢量导出格式，逗号分隔，如 pdf,eps,svg；空字符串表示不导出矢量。",
    )
    ap.add_argument(
        "--no-png",
        action="store_true",
        help="不导出 PNG（仅 SVG + 矢量）。",
    )
    args = ap.parse_args()
    vector_formats = [x.strip().lower().lstrip(".") for x in str(args.vector_formats).split(",") if x.strip()]
    out_dir = args.out_dir if args.out_dir is not None else (args.csv.parent / "thesis_out")

    rows = load_rows(args.csv)
    g = tp_groups(rows)
    if not g:
        raise SystemExit(f"no valid rows in {args.csv}")

    # 表1：按 hidden_bytes 池化（与正文「四种分片×四种k 合在一起」一致）
    by_h: dict[int, list[float]] = defaultdict(list)
    for (h, _, _), vals in g.items():
        by_h[h].extend(vals)
    # 注意：此处标准差反映的是「不同分片数量 / k 组合之间」的差异，
    # 不是同一组参数下三次重复的波动（后者见表2、表3的 stdev）。
    table1 = [[h, round(mean(vs), 2), round(stdev(vs), 2) if len(vs) > 1 else 0.0, len(vs)] for h, vs in sorted(by_h.items())]
    write_csv(
        out_dir / "thesis_embed_hls_table1_overall.csv",
        ["hidden_bytes", "mean_throughput_Bps", "stdev_across_configs_Bps", "n_runs"],
        table1,
    )

    # 表2：1024、k=1、各分片数量
    table2 = []
    for sl in sorted({k[1] for k in g if k[0] == 1024}):
        vals = g.get((1024, sl, 1), [])
        if not vals:
            continue
        table2.append([1024, 1, sl, round(mean(vals), 2), round(stdev(vals), 2) if len(vals) > 1 else 0.0, len(vals)])
    write_csv(
        out_dir / "thesis_embed_hls_table2_seg1024_k1.csv",
        ["hidden_bytes", "k", "segments_number", "mean_throughput_Bps", "stdev_Bps", "n_runs"],
        table2,
    )

    # 表3：分片数量=6，按 k × hidden
    table3 = []
    for h in sorted(by_h.keys()):
        for k in sorted({kk[2] for kk in g if kk[0] == h and kk[1] == 6}):
            vals = g.get((h, 6, k), [])
            if not vals:
                continue
            table3.append(
                [h, 6, k, round(mean(vals), 2), round(stdev(vals), 2) if len(vals) > 1 else 0.0, len(vals)]
            )
    write_csv(
        out_dir / "thesis_embed_hls_table3_seg6_by_k.csv",
        ["hidden_bytes", "segments_number", "k", "mean_throughput_Bps", "stdev_Bps", "n_runs"],
        table3,
    )

    ks_sorted = sorted({int(r[2]) for r in table3})
    svg_paths = write_thesis_fig_svgs(
        out_dir,
        table1=table1,
        table2=table2,
        g=g,
        by_h=by_h,
        ks_sorted=ks_sorted,
    )
    for sp in svg_paths:
        print(f"wrote {sp}")

    g_e2e = e2e_groups(rows)
    if g_e2e:
        lat_paths = write_thesis_latency_fig_svgs(
            out_dir,
            rows=rows,
            g_e2e=g_e2e,
            by_h_keys=sorted(by_h.keys()),
            ks_sorted=ks_sorted,
        )
        for lp in lat_paths:
            print(f"wrote {lp}")
    else:
        print("skip latency figures: no e2e_ms_p50 / p95 / p99 in csv")

    g_loss = loss_rate_groups(rows)
    if g_loss:
        by_h_loss: dict[int, list[float]] = defaultdict(list)
        for (h, _, _), vals in g_loss.items():
            by_h_loss[h].extend(vals)
        table_loss1 = [
            [h, round(mean(vs), 6), round(stdev(vs), 6) if len(vs) > 1 else 0.0, len(vs)]
            for h, vs in sorted(by_h_loss.items())
        ]
        write_csv(
            out_dir / "thesis_embed_hls_table_loss1_overall.csv",
            ["hidden_bytes", "mean_loss_rate", "stdev_across_configs", "n_runs"],
            table_loss1,
        )
        table_loss2: list[list[object]] = []
        for sl in sorted({k[1] for k in g_loss if k[0] == 1024}):
            pool: list[float] = []
            for (h, s, _), vals in g_loss.items():
                if h == 1024 and s == sl:
                    pool.extend(vals)
            if not pool:
                continue
            table_loss2.append(
                [1024, sl, round(mean(pool), 6), round(stdev(pool), 6) if len(pool) > 1 else 0.0, len(pool)]
            )
        write_csv(
            out_dir / "thesis_embed_hls_table_loss2_seg1024_avg.csv",
            ["hidden_bytes", "segments_number", "mean_loss_rate", "stdev", "n_runs"],
            table_loss2,
        )
        table_loss3: list[list[object]] = []
        for h in sorted(by_h_loss.keys()):
            for k in sorted({kk[2] for kk in g_loss if kk[0] == h and kk[1] == 6}):
                vals = g_loss.get((h, 6, k), [])
                if not vals:
                    continue
                table_loss3.append(
                    [h, 6, k, round(mean(vals), 6), round(stdev(vals), 6) if len(vals) > 1 else 0.0, len(vals)]
                )
        write_csv(
            out_dir / "thesis_embed_hls_table_loss3_seg6_by_k.csv",
            ["hidden_bytes", "segments_number", "k", "mean_loss_rate", "stdev", "n_runs"],
            table_loss3,
        )
        for lp in write_thesis_loss_fig_svgs(
            out_dir,
            g_loss=g_loss,
            by_h_loss=by_h_loss,
            ks_sorted=ks_sorted,
        ):
            print(f"wrote {lp}")
    else:
        print("skip loss figures: no loss_rate column or no valid rows")

    try:
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter
    except ImportError:
        print("matplotlib 未安装，已跳过 matplotlib 版 PNG。若需 PNG：修复 pip/网络后 pip install matplotlib，见脚本顶部说明。")
        try_export_thesis_svg_to_vector(out_dir, formats=vector_formats)
        if not args.no_png:
            try_export_thesis_svg_to_png(out_dir)
        vec_note = f" PDF/EPS: {','.join(vector_formats)}" if vector_formats else ""
        print(f"wrote CSVs + throughput/latency/loss SVG{vec_note} under {out_dir}")
        return 0

    plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    # 矢量图嵌入 TrueType，避免 PDF/EPS 中文字变方框或变路径
    plt.rcParams["pdf.fonttype"] = 42
    plt.rcParams["ps.fonttype"] = 42
    colors = _COLORS

    def save_figure(fig, basename: str, *, also_png: bool) -> None:
        """保存论文图：PDF/EPS 矢量 + 可选 PNG。"""
        try:
            for fmt in vector_formats:
                if fmt not in ("pdf", "eps", "svg"):
                    print(f"skip unknown vector format: {fmt!r}")
                    continue
                fig_path = out_dir / f"{basename}.{fmt}"
                fig.savefig(fig_path, format=fmt, bbox_inches="tight")
                print(f"wrote {fig_path}")
            if also_png:
                png_path = out_dir / f"{basename}.png"
                fig.savefig(png_path, dpi=150, bbox_inches="tight")
                print(f"wrote {png_path}")
        except PermissionError as e:
            raise SystemExit(
                f"无法写入图片（可能被占用）: {basename}.*\n请先关闭正在预览的程序，或换 --out-dir。"
            ) from e
        plt.close(fig)

    def save_png(fig, name: str) -> None:
        save_figure(fig, f"thesis_embed_hls_fig_{name}", also_png=not args.no_png)

    def save_latency_png(fig, name: str) -> None:
        save_figure(fig, f"thesis_embed_hls_latency_fig_{name}", also_png=not args.no_png)

    def save_loss_png(fig, name: str) -> None:
        save_figure(fig, f"thesis_embed_hls_loss_fig_{name}", also_png=not args.no_png)

    def style_axes(ax) -> None:
        ax.grid(True, linestyle="--", alpha=0.35, axis="y")
        ax.set_ylim(bottom=0)
        ax.tick_params(axis="both", labelsize=9)

    def y_ticks_with_unit(ax, suffix: str) -> None:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}{suffix}"))

    # 图1
    fig1, ax = plt.subplots(figsize=(5.5, 4.2))
    hs = [r[0] for r in table1]
    ms = [r[1] for r in table1]
    labels = [f"{h}\n字节" for h in hs]
    bars = ax.bar(labels, ms, color=["#4C72B0", "#55A868", "#C44E52"])
    ax.set_xlabel("hidden_bytes / B")
    ax.set_ylabel("平均吞吐量 / (B/s)")
    ax.set_title("不同隐蔽数据规模下的平均吞吐\n(4×4 参数网格合并统计)")
    for b, m in zip(bars, ms):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height(), f"{m:.0f}", ha="center", va="bottom", fontsize=9)
    style_axes(ax)
    y_ticks_with_unit(ax, " B/s")
    fig1.tight_layout()
    save_png(fig1, "1_overall")

    # 图2
    fig2, ax = plt.subplots(figsize=(5.5, 4.2))
    xs = [r[2] for r in table2]
    ys = [r[3] for r in table2]
    ax.plot(xs, ys, "o-", color="#4C72B0", linewidth=2, markersize=8)
    ax.set_xticks(xs)
    ax.set_xlabel("HLS 分片数量 segments_number / 个")
    ax.set_ylabel("平均吞吐量 / (B/s)")
    ax.set_title("小载荷(1024B)、密文分片数 k=1 时：HLS 分片数量 segments_number 与吞吐")
    style_axes(ax)
    y_ticks_with_unit(ax, " B/s")
    fig2.tight_layout()
    save_png(fig2, "2_seg1024_k1")

    # 图3
    fig3, ax = plt.subplots(figsize=(5.5, 4.2))
    for h in sorted(by_h.keys()):
        ys_line: list[float] = []
        xs_line: list[int] = []
        for k in ks_sorted:
            vals = g.get((h, 6, k), [])
            if vals:
                xs_line.append(k)
                ys_line.append(mean(vals))
        if xs_line:
            ax.plot(xs_line, ys_line, "o-", label=f"{h} B", color=colors.get(h, "#333333"), linewidth=2, markersize=7)
    ax.set_xticks(ks_sorted)
    ax.set_xlabel("密文分片数 k / 个")
    ax.set_ylabel("平均吞吐量 / (B/s)")
    ax.set_title("固定 segments_number=6：密文分片数 k 与吞吐（分档对比）")
    ax.legend()
    style_axes(ax)
    y_ticks_with_unit(ax, " B/s")
    fig3.tight_layout()
    save_png(fig3, "3_seg6_lines")

    # 图4
    fig4, ax = plt.subplots(figsize=(5.5, 4.2))
    width = 0.25
    x = range(len(ks_sorted))
    for i, h in enumerate(sorted(by_h.keys())):
        ys_b = [mean(g[(h, 6, k)]) if (h, 6, k) in g else 0 for k in ks_sorted]
        offset = (i - 1) * width
        ax.bar([xi + offset for xi in x], ys_b, width=width, label=f"{h} B", color=colors.get(h, "#888"))
    ax.set_xticks(list(x))
    ax.set_xticklabels([str(k) for k in ks_sorted])
    ax.set_xlabel("密文分片数 k / 个")
    ax.set_ylabel("平均吞吐量 / (B/s)")
    ax.set_title("固定segments_number=6：三档hidden_bytes下密文分片数与平均吞吐量的关系")
    ax.legend()
    style_axes(ax)
    y_ticks_with_unit(ax, " B/s")
    fig4.tight_layout()
    save_png(fig4, "4_seg6_bars")

    if g_e2e:
        by_h_lat: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
        for (h, _, _), trip in g_e2e.items():
            by_h_lat[h].extend(trip)
        hs_lat = sorted(by_h_lat.keys())
        m50s = [mean(t[0] for t in by_h_lat[h]) for h in hs_lat]
        m95s = [mean(t[1] for t in by_h_lat[h]) for h in hs_lat]
        m99s = [mean(t[2] for t in by_h_lat[h]) for h in hs_lat]
        x_lab = [f"{h}\n字节" for h in hs_lat]
        x_pos = range(len(hs_lat))
        w = 0.22
        lf1, ax = plt.subplots(figsize=(6.0, 4.2))
        ax.bar([i - w for i in x_pos], m50s, width=w, label="p50", color="#4C72B0")
        ax.bar(x_pos, m95s, width=w, label="p95", color="#DD8452")
        ax.bar([i + w for i in x_pos], m99s, width=w, label="p99", color="#937860")
        ax.set_xticks(list(x_pos))
        ax.set_xticklabels(x_lab)
        ax.set_xlabel("hidden_bytes / B")
        ax.set_ylabel("时延 / (ms)")
        ax.set_title("不同隐蔽数据规模下的端到端时延")
        ax.legend()
        style_axes(ax)
        y_ticks_with_unit(ax, " ms")
        lf1.tight_layout()
        save_latency_png(lf1, "1_overall")

        xs_l2: list[int] = []
        y50_l2: list[float] = []
        y95_l2: list[float] = []
        for sl in sorted({k[1] for k in g_e2e if k[0] == 1024}):
            trips_all: list[tuple[float, float, float]] = []
            for (h, s, _), trip in g_e2e.items():
                if h == 1024 and s == sl:
                    trips_all.extend(trip)
            if not trips_all:
                continue
            xs_l2.append(sl)
            y50_l2.append(mean(t[0] for t in trips_all))
            y95_l2.append(mean(t[1] for t in trips_all))
        lf2, ax = plt.subplots(figsize=(5.5, 4.2))
        ax.plot(xs_l2, y50_l2, "o-", color="#4C72B0", linewidth=2, markersize=8, label="p50")
        ax.plot(xs_l2, y95_l2, "s--", color="#DD8452", linewidth=2, markersize=7, label="p95")
        ax.set_xticks(xs_l2)
        ax.set_xlabel("HLS 分片数量 segments_number / 个")
        ax.set_ylabel("时延 / (ms)")
        ax.set_title("小载荷（1024B）：HLS分片数量与端到端时延")
        ax.legend()
        style_axes(ax)
        y_ticks_with_unit(ax, " ms")
        lf2.tight_layout()
        save_latency_png(lf2, "2_seg1024_avg")

        lf3, ax = plt.subplots(figsize=(5.5, 4.2))
        for h in sorted(by_h.keys()):
            col = colors.get(h, "#333")
            xs_line: list[int] = []
            ys50: list[float] = []
            ys95: list[float] = []
            for k in ks_sorted:
                trip = g_e2e.get((h, 6, k), [])
                if trip:
                    xs_line.append(k)
                    ys50.append(mean(t[0] for t in trip))
                    ys95.append(mean(t[1] for t in trip))
            if xs_line:
                ax.plot(xs_line, ys50, "o-", color=col, linewidth=2.2, markersize=7, label=f"{h} B p50")
                ax.plot(xs_line, ys95, "s--", color=col, linewidth=1.8, markersize=6, alpha=0.85, label=f"{h} B p95")
        ax.set_xticks(ks_sorted)
        ax.set_xlabel("密文分片数 k / 个")
        ax.set_ylabel("时延 / (ms)")
        ax.set_title("固定HLS分片数=6：密文分片数 k 与 p50 / p95 时延")
        ax.legend(ncol=2, fontsize=8)
        style_axes(ax)
        y_ticks_with_unit(ax, " ms")
        lf3.tight_layout()
        save_latency_png(lf3, "3_seg6_p50p95")

        lf4, ax = plt.subplots(figsize=(5.5, 4.2))
        width = 0.25
        x = range(len(ks_sorted))
        for i, h in enumerate(sorted(by_h.keys())):
            ys_b = []
            for k in ks_sorted:
                trip = g_e2e.get((h, 6, k), [])
                ys_b.append(mean(t[0] for t in trip) if trip else 0.0)
            offset = (i - 1) * width
            ax.bar([xi + offset for xi in x], ys_b, width=width, label=f"{h} B", color=colors.get(h, "#888"))
        ax.set_xticks(list(x))
        ax.set_xticklabels([str(k) for k in ks_sorted])
        ax.set_xlabel("密文分片数 k / 个")
        ax.set_ylabel("p50 时延 / (ms)")
        ax.set_title("固定HLS分片数=6：p50 时延对比")
        ax.legend()
        style_axes(ax)
        y_ticks_with_unit(ax, " ms")
        lf4.tight_layout()
        save_latency_png(lf4, "4_seg6_bars")

        tail_gaps = [mean(t[1] - t[0] for t in by_h_lat[h]) for h in hs_lat]
        lf5, ax = plt.subplots(figsize=(5.5, 4.2))
        ax.bar(range(len(hs_lat)), tail_gaps, color=[colors.get(h, "#4C72B0") for h in hs_lat])
        ax.set_xticks(range(len(hs_lat)))
        ax.set_xticklabels([f"{h} B" for h in hs_lat])
        ax.set_xlabel("hidden_bytes / B")
        ax.set_ylabel("平均 (p95−p50) / (ms)")
        ax.set_title("各档 hidden 下平均尾部间隔 (p95−p50)")
        style_axes(ax)
        y_ticks_with_unit(ax, " ms")
        lf5.tight_layout()
        save_latency_png(lf5, "5_tail")

    if g_loss:
        by_h_loss_mpl: dict[int, list[float]] = defaultdict(list)
        for (h, _, _), vals in g_loss.items():
            by_h_loss_mpl[h].extend(vals)
        hs_loss = sorted(by_h_loss_mpl.keys())
        m_loss_pct = [mean(by_h_loss_mpl[h]) * 100.0 for h in hs_loss]
        loss_f1, ax = plt.subplots(figsize=(5.5, 4.2))
        ax.bar([f"{h}\n字节" for h in hs_loss], m_loss_pct, color=[colors.get(h, "#4C72B0") for h in hs_loss])
        ax.set_xlabel("hidden_bytes / B")
        ax.set_ylabel("丢包率 / (%)")
        ax.set_title("不同隐蔽数据规模下的平均丢包率\n(合并统计)")
        style_axes(ax)
        y_ticks_with_unit(ax, " %")
        ymax = max(m_loss_pct) * 1.15 if m_loss_pct and max(m_loss_pct) > 0 else 0.01
        ax.set_ylim(0, ymax)
        loss_f1.tight_layout()
        save_loss_png(loss_f1, "1_overall")

        xs_l_loss: list[int] = []
        ys_l_loss: list[float] = []
        for sl in sorted({k[1] for k in g_loss if k[0] == 1024}):
            pool_l: list[float] = []
            for (h, s, _), vals in g_loss.items():
                if h == 1024 and s == sl:
                    pool_l.extend(vals)
            if not pool_l:
                continue
            xs_l_loss.append(sl)
            ys_l_loss.append(mean(pool_l) * 100.0)
        loss_f2, ax = plt.subplots(figsize=(5.5, 4.2))
        ax.plot(xs_l_loss, ys_l_loss, "o-", color="#C44E52", linewidth=2, markersize=8)
        ax.set_xticks(xs_l_loss)
        ax.set_xlabel("HLS 分片数量 segments_number / 个")
        ax.set_ylabel("丢包率 / (%)")
        ax.set_title("小载荷(1024B)：HLS 分片数量 segments_number 与丢包率（对各 k 平均）")
        style_axes(ax)
        y_ticks_with_unit(ax, " %")
        ymax2 = max(ys_l_loss) * 1.15 if ys_l_loss and max(ys_l_loss) > 0 else 0.01
        ax.set_ylim(0, ymax2)
        loss_f2.tight_layout()
        save_loss_png(loss_f2, "2_seg1024_avg")

        loss_f3, ax = plt.subplots(figsize=(5.5, 4.2))
        for h in sorted(by_h.keys()):
            ys_line: list[float] = []
            xs_line: list[int] = []
            for k in ks_sorted:
                vals = g_loss.get((h, 6, k), [])
                if vals:
                    xs_line.append(k)
                    ys_line.append(mean(vals) * 100.0)
            if xs_line:
                ax.plot(xs_line, ys_line, "o-", label=f"{h} B", color=colors.get(h, "#333"), linewidth=2, markersize=7)
        ax.set_xticks(ks_sorted)
        ax.set_xlabel("密文分片数 k / 个")
        ax.set_ylabel("丢包率 / (%)")
        ax.set_title("固定HLS分片数=6：密文分片数 k 与平均丢包率")
        ax.legend()
        style_axes(ax)
        y_ticks_with_unit(ax, " %")
        ys_flat = [
            mean(g_loss[(h, 6, k)]) * 100.0
            for h in sorted(by_h.keys())
            for k in ks_sorted
            if (h, 6, k) in g_loss
        ]
        ymax3 = max(ys_flat) * 1.15 if ys_flat and max(ys_flat) > 0 else 0.01
        ax.set_ylim(0, ymax3)
        loss_f3.tight_layout()
        save_loss_png(loss_f3, "3_seg6_lines")

        loss_f4, ax = plt.subplots(figsize=(5.5, 4.2))
        width_l = 0.25
        x_l = range(len(ks_sorted))
        for i, h in enumerate(sorted(by_h.keys())):
            ys_b = [mean(g_loss[(h, 6, k)]) * 100.0 if (h, 6, k) in g_loss else 0.0 for k in ks_sorted]
            offset = (i - 1) * width_l
            ax.bar([xi + offset for xi in x_l], ys_b, width=width_l, label=f"{h} B", color=colors.get(h, "#888"))
        ax.set_xticks(list(x_l))
        ax.set_xticklabels([str(k) for k in ks_sorted])
        ax.set_xlabel("密文分片数 k / 个")
        ax.set_ylabel("丢包率 / (%)")
        ax.set_title("固定HLS分片数=6：平均丢包率对比")
        ax.legend()
        style_axes(ax)
        y_ticks_with_unit(ax, " %")
        ymax4 = max(
            (
                mean(g_loss[(h, 6, k)]) * 100.0
                for h in sorted(by_h.keys())
                for k in ks_sorted
                if (h, 6, k) in g_loss
            ),
            default=0.0,
        )
        ax.set_ylim(0, ymax4 * 1.15 if ymax4 > 0 else 0.01)
        loss_f4.tight_layout()
        save_loss_png(loss_f4, "4_seg6_bars")

    if not args.no_png:
        try_export_thesis_svg_to_png(out_dir)
    vec_note = f" PDF/EPS: {','.join(vector_formats)}" if vector_formats else ""
    print(f"wrote CSVs + throughput/latency/loss SVG{vec_note} under {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
