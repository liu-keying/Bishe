#!/usr/bin/env python3
"""
HLS-TS 载体层隐蔽性：生成 cover/stego 样本并做统计对比。

与 A 的 /overlay/embed-hls（psk_hmac_inplace）对齐的离线嵌入；指标含熵、
gzip 压缩比、卡方（实际出现字节 m×2 表，E<5 相邻合并后检验，df=合并桶数−1）、KS、KL/JS、差分方差、修改比例；可选 188B 滑窗。

用法（仓库根目录）:
  python scripts/stego_covertness_ts.py ^
    --segments ".\\out\\hls_seg*.ts" --segments-limit 6 ^
    --out scripts/stego_covertness_out ^
    --k-list 1,3 --hidden-bytes-list 1024,4096,65536 --pad-bytes-list 0,188

卡方 p 值由标准库近似计算，无需 scipy。
环境: BISHE_PSK_HEX 或 --psk-hex（64 位十六进制，缺省则固定实验 PSK）
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import random
import sys
import uuid
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from bishe_demo.stego import (  # noqa: E402
    STEGO_TAG_LEN,
    embed_pad_inplace,
    embed_psk_inplace,
)

# 与 bench 一致：固定实验 PSK（仅用于隐蔽性统计，勿用于生产）
_DEFAULT_PSK_HEX = "00" * 32
_DEFAULT_TOKEN = "stego-covertness-lab-token"
_DEFAULT_SESSION = "bishe-1"
_TS_PACKET = 188


def _load_segments(glob_pat: str, limit: int) -> list[tuple[str, bytes]]:
    import glob as glob_mod

    paths = sorted(glob_mod.glob(glob_pat))
    if not paths:
        raise FileNotFoundError(f"no segments matched: {glob_pat!r}")
    if limit > 0:
        paths = paths[:limit]
    out: list[tuple[str, bytes]] = []
    for p in paths:
        with open(p, "rb") as f:
            out.append((os.path.basename(p), f.read()))
    return out


def _psk_from_hex(psk_hex: str) -> bytes:
    hx = psk_hex.strip()
    if len(hx) != 64:
        raise ValueError(f"PSK must be 64 hex chars, got len={len(hx)}")
    return bytes.fromhex(hx)


def _psk_from_args(psk_hex: str) -> bytes:
    hx = (psk_hex or os.environ.get("BISHE_PSK_HEX") or _DEFAULT_PSK_HEX).strip()
    return _psk_from_hex(hx)


def _derive_psk_token(*, run_index: int, master_seed: int) -> tuple[str, str]:
    """由 master_seed 确定性派生第 run_index 把 PSK（64 hex）与 link_token。"""
    i = int(run_index)
    psk_hex = hashlib.sha256(f"bishe-covertness/v1|psk|{master_seed}|{i}".encode()).hexdigest()
    tok = hashlib.sha256(f"bishe-covertness/v1|token|{master_seed}|{i}".encode()).hexdigest()[:24]
    return psk_hex, f"token-{tok}"


def _make_hidden(n: int, rng: random.Random) -> bytes:
    n = max(16, int(n))
    msg_id = uuid.UUID(int=rng.getrandbits(128)).bytes
    if n <= 16:
        return msg_id[:n]
    return msg_id + rng.randbytes(n - 16)


def _encrypt_hidden_seeded(*, psk: bytes, hidden: bytes, aad: bytes, rng: random.Random) -> bytes:
    """与 psk_aead.encrypt_hidden 相同格式，nonce 由 rng 产生以便复现。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from bishe_demo.psk_aead import MAGIC, NONCE_LEN

    if len(psk) != 32:
        raise ValueError("psk must be 32 bytes")
    msg_id = hidden[:16].ljust(16, b"\x00")
    body = hidden[16:]
    nonce = rng.randbytes(NONCE_LEN)
    ct = AESGCM(psk).encrypt(nonce, body, aad)
    return msg_id + MAGIC + nonce + ct


def embed_hls_local(
    segment_parts: list[bytes],
    hidden_plain: bytes,
    *,
    psk: bytes,
    link_token: str,
    session: str,
    k: int,
    pad_bytes: int,
    g_bytes: int | None,
    seed: int,
) -> tuple[list[bytes], dict]:
    """离线复现 A 的 psk_hmac_inplace + 非承载分片 embed_pad_inplace。"""
    rng = random.Random(int(seed))
    n = len(segment_parts)
    k = int(k)
    if k < 1 or k > n:
        raise ValueError(f"k must be in 1..{n}, got {k}")

    aad = str(session).encode("utf-8")
    cipher = _encrypt_hidden_seeded(psk=psk, hidden=hidden_plain, aad=aad, rng=rng)
    cipher_bytes_real = len(cipher)

    if g_bytes is None or g_bytes <= 0:
        g_bytes = (len(cipher) + k - 1) // k
    g_bytes = max(1, min(int(g_bytes), 16 * 1024 * 1024))

    total_cipher_bytes = k * g_bytes
    if len(cipher) > total_cipher_bytes:
        raise ValueError(
            f"cipher too large: {len(cipher)} > k*g_bytes={total_cipher_bytes} (k={k} g={g_bytes})"
        )
    if len(cipher) < total_cipher_bytes:
        cipher = cipher + rng.randbytes(total_cipher_bytes - len(cipher))

    parts = [cipher[i * g_bytes : (i + 1) * g_bytes] for i in range(k)]
    chosen = sorted(rng.sample(range(n), k))
    assign = {idx: parts[j] for j, idx in enumerate(chosen)}
    idx_map = {idx: j for j, idx in enumerate(chosen)}
    pad_psk_len = STEGO_TAG_LEN + g_bytes
    pad_tail = max(0, int(pad_bytes))

    stego: list[bytes] = []
    for i in range(n):
        piece = assign.get(i)
        if piece is None:
            blob = embed_pad_inplace(
                segment_parts[i],
                token=link_token,
                hls_index=i,
                pad_len=pad_psk_len,
            )
            if pad_tail > 0:
                blob = blob + rng.randbytes(pad_tail)
            stego.append(blob)
            continue
        blob = embed_psk_inplace(
            segment_parts[i],
            token=link_token,
            hls_index=i,
            frag_idx=idx_map[i],
            cipher_fragment=piece,
        )
        stego.append(blob)

    meta = {
        "k": k,
        "g_bytes": g_bytes,
        "cipher_bytes_real": cipher_bytes_real,
        "chosen_indices": chosen,
        "pad_psk_len": pad_psk_len,
        "pad_bytes_tail": pad_tail,
        "plain_hidden_bytes": len(hidden_plain),
    }
    return stego, meta


def byte_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    c = Counter(data)
    n = len(data)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def gzip_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    return len(gzip.compress(data, compresslevel=6)) / len(data)


def byte_histogram(data: bytes) -> list[int]:
    h = [0] * 256
    for b in data:
        h[b] += 1
    return h


def hist_to_prob(h: list[int], *, eps: float = 1e-12) -> list[float]:
    """字节直方图 -> 概率分布；Laplace 平滑避免 KL 中 log(0)。"""
    n = sum(h)
    if n == 0:
        u = 1.0 / 256.0
        return [u] * 256
    denom = n + 256.0 * eps
    return [(c + eps) / denom for c in h]


def kl_divergence(p: list[float], q: list[float]) -> float:
    """D_KL(P||Q) = Σ p_i log2(p_i/q_i)，单位 bit（与 byte_entropy 一致）。"""
    total = 0.0
    for pi, qi in zip(p, q):
        if pi > 0.0 and qi > 0.0:
            total += pi * math.log2(pi / qi)
    return total


def js_divergence(p: list[float], q: list[float]) -> float:
    """Jensen–Shannon 散度（对称、有界），单位 bit。"""
    m = [(pi + qi) * 0.5 for pi, qi in zip(p, q)]
    return 0.5 * kl_divergence(p, m) + 0.5 * kl_divergence(q, m)


def kl_hist(h_p: list[int], h_q: list[int]) -> tuple[float, float, float]:
    """
    cover/stego 字节直方图的 KL 与 JS。
    返回 (D_KL(stego||cover), D_KL(cover||stego), JS(cover,stego))。
    隐蔽性越好则 D_KL(stego||cover) 与 JS 越小。
    """
    p_cover = hist_to_prob(h_p)
    p_stego = hist_to_prob(h_q)
    kl_sc = kl_divergence(p_stego, p_cover)
    kl_cs = kl_divergence(p_cover, p_stego)
    js = js_divergence(p_cover, p_stego)
    return kl_sc, kl_cs, js


def _norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def chi2_sf(x: float, df: int) -> float:
    """卡方分布上尾概率 P(X > x)，Wilson–Hilferty 正态近似（df>=30 时足够论文使用）。"""
    if df <= 0:
        return 1.0
    if x <= 0:
        return 1.0
    z = ((x / df) ** (1.0 / 3.0) - (1.0 - 2.0 / (9.0 * df))) / math.sqrt(2.0 / (9.0 * df))
    return _norm_sf(z)


CHI2_MIN_EXPECTED = 5.0


def chi2_uniform(data: bytes) -> tuple[float, float | None]:
    """相对均匀分布（256 桶）的 Pearson 卡方；返回 (chi2, p_value)。"""
    n = len(data)
    if n == 0:
        return 0.0, None
    obs = byte_histogram(data)
    exp = n / 256.0
    chi2 = sum((o - exp) ** 2 / exp for o in obs)
    return chi2, chi2_sf(chi2, 255)


def _chi2_group_expected_ok(
    group: list[int],
    h_a: list[int],
    h_b: list[int],
    *,
    n_a: int,
    n_b: int,
    tot: int,
    min_expected: float,
) -> bool:
    o_a = sum(h_a[i] for i in group)
    o_b = sum(h_b[i] for i in group)
    col = o_a + o_b
    if col == 0:
        return True
    e_a = n_a * col / tot
    e_b = n_b * col / tot
    if o_a > 0 and e_a < min_expected:
        return False
    if o_b > 0 and e_b < min_expected:
        return False
    return True


def _chi2_merge_groups_for_efive(
    groups: list[list[int]],
    h_a: list[int],
    h_b: list[int],
    *,
    n_a: int,
    n_b: int,
    tot: int,
    min_expected: float = CHI2_MIN_EXPECTED,
) -> list[list[int]]:
    """按字节值顺序，将相邻类别合并直至各合并组在 cover/stego 行上 E>=min_expected。"""
    if len(groups) <= 1:
        return groups
    while True:
        bad = [
            j
            for j, g in enumerate(groups)
            if not _chi2_group_expected_ok(g, h_a, h_b, n_a=n_a, n_b=n_b, tot=tot, min_expected=min_expected)
        ]
        if not bad:
            break
        if len(groups) <= 1:
            break
        j = bad[0]
        if j == 0:
            partner = 1
        elif j == len(groups) - 1:
            partner = j - 1
        else:
            left_col = sum(h_a[i] + h_b[i] for i in groups[j - 1])
            right_col = sum(h_a[i] + h_b[i] for i in groups[j + 1])
            partner = j - 1 if left_col <= right_col else j + 1
        groups[partner].extend(groups[j])
        del groups[j]
    return groups


def _chi2_homogeneity_on_groups(
    groups: list[list[int]],
    h_a: list[int],
    h_b: list[int],
    *,
    n_a: int,
    n_b: int,
    tot: int,
) -> tuple[float, float]:
    chi2 = 0.0
    min_e = float("inf")
    for g in groups:
        o_a = sum(h_a[i] for i in g)
        o_b = sum(h_b[i] for i in g)
        col = o_a + o_b
        if col == 0:
            continue
        e_a = n_a * col / tot
        e_b = n_b * col / tot
        if o_a > 0:
            min_e = min(min_e, e_a)
            chi2 += (o_a - e_a) ** 2 / e_a
        if o_b > 0:
            min_e = min(min_e, e_b)
            chi2 += (o_b - e_b) ** 2 / e_b
    return chi2, 0.0 if min_e == float("inf") else float(min_e)


def chi2_homogeneity(
    h_a: list[int],
    h_b: list[int],
    *,
    min_expected: float = CHI2_MIN_EXPECTED,
) -> tuple[float, float | None, int, int, int, float]:
    """
    cover vs stego 同质性 Pearson 卡方（规范流程）：

    1. 统计实际出现的字节取值，构造初始 m×2 列联表；
    2. 在「分布相同」假设下计算各列期望频数 E_{c,b}, E_{s,b}；
    3. 将相邻且 E<5 的类别合并（Cochran 准则）；
    4. 对合并后的 m' 列计算 χ²，df = m' - 1。

    返回 (chi2, p, m_initial_bins, m_merged_bins, df, min_expected_cell)。
    """
    n_a = sum(h_a)
    n_b = sum(h_b)
    if n_a == 0 or n_b == 0:
        return 0.0, None, 0, 0, 0, 0.0
    tot = n_a + n_b
    active = [i for i in range(256) if h_a[i] or h_b[i]]
    m_initial = len(active)
    if m_initial == 0:
        return 0.0, None, 0, 0, 0, 0.0

    groups = [[i] for i in active]
    groups = _chi2_merge_groups_for_efive(
        groups, h_a, h_b, n_a=n_a, n_b=n_b, tot=tot, min_expected=min_expected
    )
    m_merged = len(groups)
    chi2, min_e = _chi2_homogeneity_on_groups(groups, h_a, h_b, n_a=n_a, n_b=n_b, tot=tot)
    df = m_merged - 1
    if df <= 0:
        return chi2, 1.0, m_initial, m_merged, df, min_e
    return chi2, chi2_sf(chi2, df), m_initial, m_merged, df, min_e


def ks_hist(h_a: list[int], h_b: list[int]) -> tuple[float, float | None]:
    """两直方图的 KS 距离 D 及近似 p 值（有效样本量 n_a*n_b/(n_a+n_b)）。"""
    n_a = sum(h_a)
    n_b = sum(h_b)
    if n_a == 0 or n_b == 0:
        return 0.0, None
    cdf_a = 0.0
    cdf_b = 0.0
    d_max = 0.0
    for i in range(256):
        cdf_a += h_a[i] / n_a
        cdf_b += h_b[i] / n_b
        d_max = max(d_max, abs(cdf_a - cdf_b))
    ne = n_a * n_b / (n_a + n_b)
    if ne <= 0 or d_max <= 0:
        return d_max, 1.0
    p = min(1.0, max(0.0, 2.0 * math.exp(-2.0 * ne * d_max * d_max)))
    return d_max, p


def diff_variance(cover: bytes, stego: bytes) -> float:
    n = min(len(cover), len(stego))
    if n < 2:
        return 0.0
    diffs = [stego[i] - cover[i] for i in range(n)]
    mean = sum(diffs) / n
    return sum((d - mean) ** 2 for d in diffs) / n


def modified_ratio(cover: bytes, stego: bytes) -> float:
    n = min(len(cover), len(stego))
    if n == 0:
        return 0.0
    changed = sum(1 for i in range(n) if cover[i] != stego[i])
    return changed / n


def iter_ts_windows(data: bytes, win: int) -> list[bytes]:
    if win <= 0 or not data:
        return [data] if data else []
    return [data[i : i + win] for i in range(0, len(data), win)]


def _rel_delta(cover_val: float, stego_val: float) -> float:
    if cover_val == 0:
        return 0.0 if stego_val == 0 else float("inf")
    return 100.0 * (stego_val - cover_val) / cover_val


def analyze_pair(
    cover: bytes,
    stego: bytes,
    *,
    window: int,
) -> dict[str, float | int | None]:
    h_c = byte_histogram(cover)
    h_s = byte_histogram(stego)
    chi2_u_c, p_u_c = chi2_uniform(cover)
    chi2_u_s, p_u_s = chi2_uniform(stego)
    chi2_h, p_h, chi2_m0, chi2_m1, chi2_df, chi2_min_e = chi2_homogeneity(h_c, h_s)
    ks_d, ks_p = ks_hist(h_c, h_s)
    kl_sc, kl_cs, js = kl_hist(h_c, h_s)

    row: dict[str, float | int | None] = {
        "file_bytes": len(cover),
        "entropy_cover": byte_entropy(cover),
        "entropy_stego": byte_entropy(stego),
        "entropy_delta_pct": _rel_delta(byte_entropy(cover), byte_entropy(stego)),
        "gzip_ratio_cover": gzip_ratio(cover),
        "gzip_ratio_stego": gzip_ratio(stego),
        "gzip_ratio_delta_pct": _rel_delta(gzip_ratio(cover), gzip_ratio(stego)),
        "chi2_uniform_cover": chi2_u_c,
        "chi2_uniform_stego": chi2_u_s,
        "chi2_uniform_p_cover": p_u_c,
        "chi2_uniform_p_stego": p_u_s,
        "chi2_homogeneity_cover_vs_stego": chi2_h,
        "chi2_homogeneity_p": p_h,
        "chi2_homogeneity_active_bins": chi2_m0,
        "chi2_homogeneity_merged_bins": chi2_m1,
        "chi2_homogeneity_df": chi2_df,
        "chi2_homogeneity_min_expected": chi2_min_e,
        "ks_cover_vs_stego": ks_d,
        "ks_p": ks_p,
        "kl_stego_vs_cover": kl_sc,
        "kl_cover_vs_stego": kl_cs,
        "js_cover_stego": js,
        "diff_variance": diff_variance(cover, stego),
        "modified_ratio": modified_ratio(cover, stego),
    }

    if window > 0:
        wins_c = iter_ts_windows(cover, window)
        wins_s = iter_ts_windows(stego, window)
        n_w = min(len(wins_c), len(wins_s))
        if n_w > 0:
            ent_d = []
            gz_d = []
            mod = []
            kl_w = []
            js_w = []
            for i in range(n_w):
                ent_d.append(_rel_delta(byte_entropy(wins_c[i]), byte_entropy(wins_s[i])))
                gz_d.append(_rel_delta(gzip_ratio(wins_c[i]), gzip_ratio(wins_s[i])))
                mod.append(modified_ratio(wins_c[i], wins_s[i]))
                kl_sc_w, _, js_w_val = kl_hist(byte_histogram(wins_c[i]), byte_histogram(wins_s[i]))
                kl_w.append(kl_sc_w)
                js_w.append(js_w_val)
            row["window_n"] = n_w
            row["window_entropy_delta_pct_mean"] = sum(ent_d) / n_w
            row["window_gzip_delta_pct_mean"] = sum(gz_d) / n_w
            row["window_modified_ratio_mean"] = sum(mod) / n_w
            row["window_kl_stego_vs_cover_mean"] = sum(kl_w) / n_w
            row["window_js_cover_stego_mean"] = sum(js_w) / n_w
    return row


def _config_tag(k: int, hidden_bytes: int, pad_bytes: int) -> str:
    return f"k{k}_h{hidden_bytes}_pad{pad_bytes}"


def _mean_std(vals: list[float]) -> tuple[float, float]:
    if not vals:
        return 0.0, 0.0
    m = sum(vals) / len(vals)
    if len(vals) < 2:
        return m, 0.0
    var = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
    return m, math.sqrt(var)


def _aggregate_across_psk(summary_rows: list[dict]) -> list[dict]:
    """按 (k, hidden_bytes, pad_bytes) 对多把密钥的 summary 再聚合。"""
    from collections import defaultdict

    groups: dict[tuple[int, int, int], list[dict]] = defaultdict(list)
    for r in summary_rows:
        key = (int(r["k"]), int(r["hidden_bytes"]), int(r["pad_bytes"]))
        groups[key].append(r)

    out: list[dict] = []
    for (k, h, pad), rows in sorted(groups.items()):
        def avg(key: str) -> float:
            vals = [float(x[key]) for x in rows if x.get(key) is not None]
            return sum(vals) / len(vals) if vals else 0.0

        def vmin(key: str) -> float:
            vals = [float(x[key]) for x in rows if x.get(key) is not None]
            return min(vals) if vals else 0.0

        chi2_ps = [float(x["chi2_homogeneity_p_min"]) for x in rows if x.get("chi2_homogeneity_p_min") is not None]
        out.append(
            {
                "k": k,
                "hidden_bytes": h,
                "pad_bytes": pad,
                "n_psk_runs": len(rows),
                "psk_runs": ",".join(str(x.get("psk_run", "")) for x in rows),
                "modified_ratio_pct_mean": avg("modified_ratio_mean") * 100.0,
                "modified_ratio_pct_max": max(float(x["modified_ratio_max"]) for x in rows) * 100.0,
                "delta_H_pct_mean": avg("entropy_delta_pct_mean"),
                "delta_H_pct_stdev_of_runs": _mean_std([float(x["entropy_delta_pct_mean"]) for x in rows])[1],
                "kl_stego_vs_cover_mean": avg("kl_stego_vs_cover_mean"),
                "js_cover_stego_mean": avg("js_cover_stego_mean"),
                "chi2_homogeneity_p_min_across_psk": min(chi2_ps) if chi2_ps else None,
                "chi2_homogeneity_p_max_across_psk": max(chi2_ps) if chi2_ps else None,
            }
        )
    return out


def run_matrix(args: argparse.Namespace) -> int:
    segments = _load_segments(args.segments, int(args.segments_limit))
    session = (args.session or _DEFAULT_SESSION).strip()
    k_list = [int(x) for x in args.k_list.split(",") if x.strip()]
    h_list = [int(x) for x in args.hidden_bytes_list.split(",") if x.strip()]
    pad_list = [int(x) for x in args.pad_bytes_list.split(",") if x.strip()]
    out_base = Path(args.out).resolve()
    out_base.mkdir(parents=True, exist_ok=True)

    psk_runs = max(1, int(args.psk_runs))
    keys_manifest: list[dict] = []
    per_file_rows_all: list[dict] = []
    summary_rows_all: list[dict] = []

    for psk_i in range(psk_runs):
        if psk_runs == 1 and (args.psk_hex or os.environ.get("BISHE_PSK_HEX")):
            psk_hex = (args.psk_hex or os.environ.get("BISHE_PSK_HEX") or _DEFAULT_PSK_HEX).strip()
            token = (args.token or _DEFAULT_TOKEN).strip()
        else:
            psk_hex, token = _derive_psk_token(run_index=psk_i + 1, master_seed=int(args.seed))
        psk = _psk_from_hex(psk_hex)
        psk_label = f"psk{psk_i + 1:02d}"
        keys_manifest.append(
            {
                "psk_run": psk_label,
                "psk_hex": psk_hex,
                "link_token": token,
            }
        )
        out_root = out_base / psk_label if psk_runs > 1 else out_base
        out_root.mkdir(parents=True, exist_ok=True)
        print(f"\n=== {psk_label} psk_hex={psk_hex[:16]}... token={token!r} ===")

        per_file_rows: list[dict] = []
        summary_rows: list[dict] = []

        for k in k_list:
            for hidden_bytes in h_list:
                for pad_bytes in pad_list:
                    tag = _config_tag(k, hidden_bytes, pad_bytes)
                    cfg_dir = out_root / tag
                    cover_dir = cfg_dir / "cover"
                    stego_dir = cfg_dir / "stego"
                    cover_dir.mkdir(parents=True, exist_ok=True)
                    stego_dir.mkdir(parents=True, exist_ok=True)

                    seed = (int(args.seed) ^ (hash(tag) & 0xFFFFFFFF)) & 0xFFFFFFFF
                    rng = random.Random(seed)
                    hidden = _make_hidden(hidden_bytes, rng)
                    parts = [b for _, b in segments]

                    if args.analyze_only:
                        cover_dir = cfg_dir / "cover"
                        stego_dir = cfg_dir / "stego"
                        if not cover_dir.is_dir() or not stego_dir.is_dir():
                            print(f"[skip] {tag}: missing cover/ or stego/ under {cfg_dir}")
                            continue
                        stego_parts = []
                        names_order = [n for n, _ in segments]
                        for name in names_order:
                            with open(stego_dir / name, "rb") as f:
                                stego_parts.append(f.read())
                        meta_path = cfg_dir / "embed_meta.json"
                        if meta_path.is_file():
                            with open(meta_path, encoding="utf-8") as f:
                                meta = json.load(f)
                        else:
                            meta = {"g_bytes": 0, "chosen_indices": []}
                    else:
                        try:
                            stego_parts, meta = embed_hls_local(
                                parts,
                                hidden,
                                psk=psk,
                                link_token=token,
                                session=session,
                                k=k,
                                pad_bytes=pad_bytes,
                                g_bytes=int(args.g_bytes) if args.g_bytes else None,
                                seed=seed,
                            )
                        except ValueError as e:
                            print(f"[skip] {tag}: {e}")
                            continue

                        meta_path = cfg_dir / "embed_meta.json"
                        with open(meta_path, "w", encoding="utf-8") as f:
                            json.dump(meta, f, indent=2)

                    file_metrics: list[dict] = []
                    for (name, cover_b), stego_b in zip(segments, stego_parts):
                        cover_path = cover_dir / name
                        stego_path = stego_dir / name
                        if args.analyze_only:
                            with open(cover_path, "rb") as f:
                                cover_b = f.read()
                        else:
                            with open(cover_path, "wb") as f:
                                f.write(cover_b)
                            with open(stego_path, "wb") as f:
                                f.write(stego_b)

                        m = analyze_pair(cover_b, stego_b, window=int(args.window))
                        m.update(
                            {
                                "config": tag,
                                "filename": name,
                                "k": k,
                                "hidden_bytes": hidden_bytes,
                                "pad_bytes": pad_bytes,
                            }
                        )
                        file_metrics.append(m)
                        per_file_rows.append(m)

                    def col(key: str) -> list[float]:
                        return [float(r[key]) for r in file_metrics if r.get(key) is not None]

                    ent_d = col("entropy_delta_pct")
                    gz_d = col("gzip_ratio_delta_pct")
                    mod = col("modified_ratio")
                    ks_v = col("ks_cover_vs_stego")
                    kl_sc_v = col("kl_stego_vs_cover")
                    js_v = col("js_cover_stego")
                    summary_rows.append(
                        {
                            "psk_run": psk_label,
                            "psk_hex": psk_hex,
                            "link_token": token,
                            "config": tag,
                            "k": k,
                            "hidden_bytes": hidden_bytes,
                            "pad_bytes": pad_bytes,
                            "n_segments": len(file_metrics),
                            "g_bytes": meta["g_bytes"],
                            "chosen_indices": json.dumps(meta["chosen_indices"]),
                            "entropy_delta_pct_mean": _mean_std(ent_d)[0],
                            "entropy_delta_pct_stdev": _mean_std(ent_d)[1],
                            "gzip_ratio_delta_pct_mean": _mean_std(gz_d)[0],
                            "gzip_ratio_delta_pct_stdev": _mean_std(gz_d)[1],
                            "modified_ratio_mean": _mean_std(mod)[0],
                            "modified_ratio_max": max(mod) if mod else 0.0,
                            "ks_mean": _mean_std(ks_v)[0],
                            "kl_stego_vs_cover_mean": _mean_std(kl_sc_v)[0],
                            "kl_stego_vs_cover_max": max(kl_sc_v) if kl_sc_v else 0.0,
                            "js_cover_stego_mean": _mean_std(js_v)[0],
                            "chi2_homogeneity_p_min": min(
                                (
                                    r["chi2_homogeneity_p"]
                                    for r in file_metrics
                                    if r.get("chi2_homogeneity_p") is not None
                                ),
                                default=None,
                            ),
                        }
                    )
                    print(f"[ok] {psk_label} {tag} -> {cfg_dir}")

        for r in per_file_rows:
            r["psk_run"] = psk_label
            r["psk_hex"] = psk_hex
        per_file_rows_all.extend(per_file_rows)
        summary_rows_all.extend(summary_rows)

        per_file_csv = out_root / "metrics_per_file.csv"
        summary_csv = out_root / "metrics_summary_by_config.csv"
        if per_file_rows:
            with open(per_file_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(per_file_rows[0].keys()), extrasaction="ignore")
                w.writeheader()
                w.writerows(per_file_rows)
        if summary_rows:
            with open(summary_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()), extrasaction="ignore")
                w.writeheader()
                w.writerows(summary_rows)

    with open(out_base / "keys_manifest.json", "w", encoding="utf-8") as f:
        json.dump(keys_manifest, f, indent=2)

    if per_file_rows_all:
        p_all = out_base / "metrics_per_file_all_psk.csv"
        with open(p_all, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(per_file_rows_all[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(per_file_rows_all)

    if summary_rows_all:
        s_all = out_base / "metrics_summary_all_psk.csv"
        with open(s_all, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(summary_rows_all[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(summary_rows_all)

        agg = _aggregate_across_psk(summary_rows_all)
        agg_csv = out_base / "metrics_summary_across_psk.csv"
        with open(agg_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(agg[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(agg)
        print(f"wrote {agg_csv} ({len(agg)} rows)")

    readme = out_base / "README.txt"
    with open(readme, "w", encoding="utf-8") as f:
        f.write(
            "stego_covertness_ts 输出\n"
            f"segments: {args.segments!r} limit={args.segments_limit}\n"
            f"psk_runs: {psk_runs}\n"
            "chi2 homogeneity: active-byte m×2 table, merge adjacent bins until E>=5, df=merged_bins-1\n"
            "KL/JS: D_KL(stego||cover) 与 JS(cover,stego)，单位 bit；越小越隐蔽\n"
            "keys_manifest.json: 各次 PSK/token\n"
            "metrics_summary_across_psk.csv: 多密钥汇总（论文表）\n"
        )

    print(f"\npsk_runs={psk_runs} done under {out_base}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="HLS-TS cover vs stego 隐蔽性统计")
    p.add_argument("--segments", required=True, help=r'glob，如 ".\\out\\hls_seg*.ts"')
    p.add_argument("--segments-limit", type=int, default=6)
    p.add_argument("--out", default="scripts/stego_covertness_out")
    p.add_argument("--k-list", default="1,3")
    p.add_argument("--hidden-bytes-list", default="1024,4096,65536")
    p.add_argument("--pad-bytes-list", default="0,188", help="非承载分片尾追加随机字节（实验对比）")
    p.add_argument("--psk-hex", default="", help="64 hex；仅 --psk-runs 1 时生效，否则由 --seed 派生多把钥")
    p.add_argument("--psk-runs", type=int, default=1, help="独立 PSK/token 重复次数（>=3 用于论文多密钥验证）")
    p.add_argument("--token", default=_DEFAULT_TOKEN, help="link_token；仅 --psk-runs 1 时生效")
    p.add_argument("--session", default=_DEFAULT_SESSION, help="AEAD AAD 会话名")
    p.add_argument("--g-bytes", type=int, default=0, help="0=按密文/k 自动")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--window", type=int, default=188, help="TS 滑窗长度；0=不做窗聚合")
    p.add_argument(
        "--analyze-only",
        action="store_true",
        help="不重新嵌入，仅读取各配置目录下已有 cover/ stego/ 并统计",
    )
    args = p.parse_args()

    try:
        return run_matrix(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
