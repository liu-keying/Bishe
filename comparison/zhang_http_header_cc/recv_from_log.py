"""
Receiver: recover secret bytes by parsing the JSONL written by server.py (Web log).

Run from repo root:
  python comparison/zhang_http_header_cc/recv_from_log.py --log zhang_cc.jsonl --msg-id <uuid> --out recovered.bin
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from comparison.zhang_http_header_cc.protocol import b64url_decode, parse_from_headers
from comparison.zhang_http_header_cc.psk_util import aead_bytes, load_psk_hex, open_ciphertext


def recover_from_records(records: list[dict], *, msg_id: str) -> bytes:
    parts: dict[int, tuple[int, str]] = {}
    for rec in records:
        ua = str(rec.get("user_agent") or "")
        ck = str(rec.get("cookie") or "")
        ph = parse_from_headers(user_agent=ua, cookie=ck)
        if ph is None:
            continue
        if ph.msg_id.lower() != str(msg_id).strip().lower():
            continue
        if ph.seq in parts and parts[ph.seq][1] != ph.merged_b64():
            raise ValueError(f"conflicting chunk for seq={ph.seq}")
        parts[ph.seq] = (ph.total, ph.merged_b64())

    if not parts:
        raise ValueError("no matching log lines for msg_id")

    totals = {int(v[0]) for v in parts.values()}
    if len(totals) != 1:
        raise ValueError(f"inconsistent total metadata across chunks: {sorted(totals)}")
    total = int(next(iter(totals)))
    if set(parts.keys()) != set(range(total)):
        raise ValueError(f"expected chunk indices 0..{total - 1}, got {sorted(parts.keys())}")

    b64 = "".join(parts[i][1] for i in range(total))
    return b64url_decode(b64)


def recover_plaintext_from_records(
    records: list[dict],
    *,
    msg_id: str,
    psk: bytes,
    aad: bytes,
) -> bytes:
    wire = recover_from_records(records, msg_id=msg_id)
    return open_ciphertext(psk=psk, wire=wire, aad=aad)


def iter_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Recover Zhang-style header CC payload from JSONL logs.")
    p.add_argument("--log", required=True, help="JSONL log path (from server.py)")
    p.add_argument("--msg-id", required=True, help="UUID printed by sender.py")
    p.add_argument("--out", default="", help="Write recovered bytes to this path (default: stdout as hex preview)")
    p.add_argument("--psk-hex", default="", help="32-byte PSK as 64 hex chars (or set BISHE_PSK_HEX / --psk-file)")
    p.add_argument("--psk-file", default="", help="File containing 64 hex chars of PSK")
    p.add_argument(
        "--aad",
        default="zhang-http-header-cc/v1",
        help="AEAD associated data (UTF-8); must match sender --aad",
    )
    args = p.parse_args()

    log_path = Path(args.log).resolve()
    rows = iter_jsonl(log_path)
    psk = load_psk_hex(psk_hex=str(args.psk_hex), psk_file=str(args.psk_file))
    aad = aead_bytes(aad_text=str(args.aad))
    data = recover_plaintext_from_records(rows, msg_id=str(args.msg_id), psk=psk, aad=aad)
    out = str(args.out or "").strip()
    if out:
        Path(out).write_bytes(data)
        logging.info("wrote %d bytes -> %s", len(data), out)
    else:
        preview = data[:64]
        logging.info("recovered %d bytes preview=%r", len(data), preview)
    print(f"bytes={len(data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
