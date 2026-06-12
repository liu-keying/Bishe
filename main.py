import argparse
import asyncio
import os

from bishe_demo.a_client_proxy import run_a_proxy
from bishe_demo.b_gate_pull import env_token_secret as b_gate_env_secret
from bishe_demo.b_gate_pull import run_b_gate_pull
from bishe_demo.b_pull import run_b_pull
from bishe_demo.b_reliability import ReliabilityConfig
from bishe_demo.b_worker import run_b_worker
from bishe_demo.c_server import run_c_server
from bishe_demo.e_server import env_token_secret as e_env_secret
from bishe_demo.e_server import run_e_server


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bishe-demo",
        description=(
            "应用层 Overlay 隐匿通道 demo：B 主动 GET A(类 HLS 分片、二进制体) -> Redis -> "
            "B-worker -> HTTPS/HTTP -> C"
        ),
    )
    p.add_argument(
        "role",
        choices=["a", "b-pull", "b-gate", "b-worker", "c", "e"],
        help=(
            "a=本地源站(被动出流)；b-pull=轮询 GET A 并入 Redis；b-gate=B 注册后才启动拉流；"
            "b-worker=Redis 转发 C；c=接收端；e=控制面(签发 token)"
        ),
    )

    p.add_argument("--host", default=os.environ.get("BISHE_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("BISHE_PORT", "8000")))

    p.add_argument("--redis", default=os.environ.get("BISHE_REDIS", "redis://127.0.0.1:6379/0"))
    p.add_argument("--queue", default=os.environ.get("BISHE_QUEUE", "bishe:overlay:queue"))

    p.add_argument(
        "--session",
        default=os.environ.get("BISHE_SESSION", "bishe-1"),
        help="HLS 路径 /hls/<session>/... 的起始会话，须为 bishe-<数字>，如 bishe-1；媒介分片出队后 A 自动递增",
    )
    p.add_argument("--a-url", default=os.environ.get("BISHE_A_URL", "http://127.0.0.1:8000"))
    p.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("BISHE_POLL_INTERVAL", "0.25")),
        help="b-pull 在 404 无分片时的轮询间隔(秒)",
    )
    p.add_argument(
        "--segment-interval",
        type=float,
        default=float(os.environ.get("BISHE_SEGMENT_INTERVAL", "0")),
        help="b-pull 相邻分片 GET 间隔(秒)；0=就绪后突发连拉(默认/压测)",
    )

    p.add_argument("--c-url", default=os.environ.get("BISHE_C_URL", "http://127.0.0.1:8002"))
    p.add_argument(
        "--e-url",
        default=os.environ.get("BISHE_E_URL", ""),
        help="控制面 E 的 base url（如 http://127.0.0.1:8009）；用于下发 PSK + token 并启用数据面加密",
    )
    p.add_argument(
        "--b-gate-url",
        default=os.environ.get("BISHE_B_GATE_URL", ""),
        help="可选，B-gate base url（如 http://127.0.0.1:8010）；A/C 拿到 token 后将自动注册以启动拉流",
    )
    p.add_argument("--token-secret", default=os.environ.get("BISHE_TOKEN_SECRET", ""), help="E/B-gate token 签名密钥")
    p.add_argument(
        "--ssl-cert",
        default=os.environ.get("BISHE_C_SSL_CERT", ""),
        help="C 启用 HTTPS 时的证书路径（与 --ssl-key 同时提供）",
    )
    p.add_argument(
        "--ssl-key",
        default=os.environ.get("BISHE_C_SSL_KEY", ""),
        help="C 启用 HTTPS 时的私钥路径",
    )
    p.add_argument(
        "--psk-hex",
        default=os.environ.get("BISHE_PSK_HEX", ""),
        help="C 静态 PSK（64 hex）；对比实验时可与 Zhang 发送端共用，无需 E",
    )
    p.add_argument(
        "--b-callback-url",
        default=os.environ.get("BISHE_B_CALLBACK_URL", ""),
        help="C 解密后 POST 回执到 B：如 http://127.0.0.1:8011/overlay/recv-ack",
    )
    p.add_argument(
        "--control-host",
        default=os.environ.get("BISHE_CONTROL_HOST", "127.0.0.1"),
        help="b-worker 可靠性控制面监听地址",
    )
    p.add_argument(
        "--control-port",
        type=int,
        default=int(os.environ.get("BISHE_CONTROL_PORT", "8011")),
        help="b-worker 可靠性控制面端口（recv-ack）",
    )
    p.add_argument(
        "--no-reliability",
        action="store_true",
        help="关闭 C 回执、B 超时重发与分片组装超时通知 A",
    )
    p.add_argument(
        "--c-ack-timeout-s",
        type=float,
        default=float(os.environ.get("BISHE_C_ACK_TIMEOUT_S", "30")),
        help="B 等待 C 回执超时后重发 POST /recv 的间隔(秒)",
    )
    p.add_argument(
        "--frag-assembly-timeout-s",
        type=float,
        default=float(os.environ.get("BISHE_FRAG_ASSEMBLY_TIMEOUT_S", "120")),
        help="B 收不齐 k 个密文分片后通知 A 重传(秒)",
    )
    p.add_argument(
        "--c-resend-max",
        type=int,
        default=int(os.environ.get("BISHE_C_RESEND_MAX", "3")),
        help="B 向 C 重发 /recv 的最大次数",
    )

    # ---- SOCKS5 代理参数 ----
    p.add_argument(
        "--socks-listen",
        default=os.environ.get("BISHE_SOCKS_LISTEN", "127.0.0.1"),
        help="SOCKS5 代理监听地址（A 角色）",
    )
    p.add_argument(
        "--socks-port",
        type=int,
        default=int(os.environ.get("BISHE_SOCKS_PORT", "0")),
        help="SOCKS5 代理端口，0=禁用（A 角色）",
    )
    p.add_argument(
        "--no-socks",
        action="store_true",
        help="显式禁用 SOCKS5 代理（A/C 角色）",
    )
    p.add_argument(
        "--socks-buffer-size",
        type=int,
        default=int(os.environ.get("BISHE_SOCKS_BUFFER_SIZE", "32768")),
        help="SOCKS5 TCP 读缓冲字节数",
    )
    p.add_argument(
        "--socks-connect-timeout",
        type=float,
        default=float(os.environ.get("BISHE_SOCKS_CONNECT_TIMEOUT", "30")),
        help="SOCKS5 等待 C 建连超时秒数",
    )
    p.add_argument(
        "--socks-idle-timeout",
        type=float,
        default=float(os.environ.get("BISHE_SOCKS_IDLE_TIMEOUT", "300")),
        help="SOCKS5 空闲连接超时秒数",
    )
    p.add_argument(
        "--socks-max-conns",
        type=int,
        default=int(os.environ.get("BISHE_SOCKS_MAX_CONNS", "50")),
        help="SOCKS5 最大并发隧道连接数",
    )
    p.add_argument(
        "--queue-rev",
        default=os.environ.get("BISHE_QUEUE_REV", "bishe:overlay:queue:rev"),
        help="反向 Redis 队列 key（B-gate 角色，C→A 方向）",
    )
    return p


async def _amain() -> None:
    args = build_parser().parse_args()

    if args.role == "a":
        sid = (args.session or "").strip() or None
        socks_on = not args.no_socks and args.socks_port > 0
        await run_a_proxy(
            host=args.host,
            port=args.port,
            session_id=sid,
            e_url=(args.e_url or "").strip(),
            c_url=args.c_url,
            b_gate_url=(args.b_gate_url or "").strip(),
            socks_host=args.socks_listen,
            socks_port=args.socks_port,
            socks_enabled=socks_on,
            socks_buffer_size=args.socks_buffer_size,
            socks_connect_timeout=args.socks_connect_timeout,
            socks_idle_timeout=args.socks_idle_timeout,
        )
        return
    if args.role == "b-pull":
        await run_b_pull(
            a_base_url=args.a_url,
            session_id=(args.session or "").strip() or "bishe-fixed-session",
            redis_url=args.redis,
            queue_key=args.queue,
            poll_interval_s=args.poll_interval,
            segment_interval_s=float(args.segment_interval),
        )
        return
    if args.role == "b-gate":
        secret = (args.token_secret or "").strip() or b_gate_env_secret()
        rcfg_gate = ReliabilityConfig(
            enabled=not args.no_reliability,
            c_ack_timeout_s=float(args.c_ack_timeout_s),
            c_resend_max=int(args.c_resend_max),
            frag_assembly_timeout_s=float(args.frag_assembly_timeout_s),
        )
        await run_b_gate_pull(
            host=args.host,
            port=args.port,
            redis_url=args.redis,
            queue_key=args.queue,
            poll_interval_s=args.poll_interval,
            segment_interval_s=float(args.segment_interval),
            token_secret=secret,
            control_host=args.control_host,
            control_port=int(args.control_port),
            reliability=rcfg_gate,
            queue_key_rev=(args.queue_rev or "").strip() or "bishe:overlay:queue:rev",
        )
        return
    if args.role == "b-worker":
        rcfg = ReliabilityConfig(
            enabled=not args.no_reliability,
            c_ack_timeout_s=float(args.c_ack_timeout_s),
            c_resend_max=int(args.c_resend_max),
            frag_assembly_timeout_s=float(args.frag_assembly_timeout_s),
        )
        await run_b_worker(
            redis_url=args.redis,
            queue_key=args.queue,
            c_base_url=args.c_url,
            control_host=args.control_host,
            control_port=int(args.control_port),
            reliability=rcfg,
        )
        return
    if args.role == "c":
        cert = (args.ssl_cert or "").strip() or None
        key = (args.ssl_key or "").strip() or None
        socks_on = not args.no_socks
        await run_c_server(
            host=args.host,
            port=args.port,
            ssl_certfile=cert,
            ssl_keyfile=key,
            e_url=(args.e_url or "").strip(),
            a_url=(args.a_url or "").strip(),
            b_gate_url=(args.b_gate_url or "").strip(),
            psk_hex=(args.psk_hex or "").strip(),
            b_callback_url=(args.b_callback_url or "").strip(),
            socks_enabled=socks_on,
            socks_max_conns=args.socks_max_conns,
            socks_idle_timeout=args.socks_idle_timeout,
        )
        return
    if args.role == "e":
        secret = (args.token_secret or "").strip() or e_env_secret()
        await run_e_server(host=args.host, port=args.port, token_secret=secret)
        return

    raise SystemExit(f"未知 role: {args.role}")


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
