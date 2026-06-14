from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from aiohttp import web

from .hls_puller import run_hls_puller
from .reliability import ReliabilityConfig
from .stego_worker import run_stego_worker
from .crypto_box import b64d
from .token_util import TokenClaims, verify_token


@dataclass(frozen=True)
class GatewayConfig:
    host: str
    port: int
    redis_url: str
    queue_key: str
    poll_interval_s: float
    segment_interval_s: float
    token_secret: str
    control_host: str
    control_port: int
    reliability: ReliabilityConfig
    queue_key_rev: str = "bishe:overlay:queue:rev"


def _claims_from_token(cfg: GatewayConfig, token: str) -> TokenClaims:
    return verify_token(secret=cfg.token_secret, token=token)


async def _post_stop_notice(url: str, *, who: str) -> None:
    # 最小实现：只通知，不要求对方一定实现该接口
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False, trust_env=False) as client:
            await client.post(url, json={"ok": True, "who": who})
    except Exception:
        return


def _reset_gateway_state(app: web.Application) -> None:
    app["reg"] = {"client": False, "server": False}
    app["claims"] = None
    app["link_psk"] = None
    app["link_token"] = ""
    app["pull_task"] = None
    app["worker_task"] = None
    app["pull_task_rev"] = None
    app["worker_task_rev"] = None


async def handle_register(request: web.Request) -> web.Response:
    cfg: GatewayConfig = request.app["cfg"]
    body = await request.json()
    role = str(body.get("role") or "").strip().lower()
    token = str(body.get("token") or "").strip()

    if role not in ("client", "server"):
        return web.json_response({"ok": False, "error": "role must be 'client' or 'server'"}, status=400)
    if not token:
        return web.json_response({"ok": False, "error": "missing token"}, status=400)

    try:
        claims = _claims_from_token(cfg, token)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"bad token: {e!r}"}, status=400)

    reg = request.app["reg"]
    reg[role] = True
    request.app["claims"] = claims
    request.app["link_token"] = token

    psk_b64 = str(body.get("psk_b64") or "").strip()
    if psk_b64:
        try:
            raw_psk = b64d(psk_b64)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"bad psk_b64: {e!r}"}, status=400)
        if len(raw_psk) != 32:
            return web.json_response({"ok": False, "error": "psk_b64 must decode to 32 bytes"}, status=400)
        prev = request.app.get("link_psk")
        if prev is not None and prev != raw_psk:
            return web.json_response({"ok": False, "error": "psk mismatch between client and server register"}, status=400)
        request.app["link_psk"] = raw_psk

    # 两边都登记成功 -> gateway 同时启动正向 + 反向 pull/worker
    if reg.get("client") and reg.get("server") and request.app.get("pull_task") is None:
        client_url = (claims.a_url or "").strip()
        server_url = (claims.c_url or "").strip()
        if not client_url:
            return web.json_response({"ok": False, "error": "missing client_url in token"}, status=400)
        if not server_url:
            return web.json_response({"ok": False, "error": "missing server_url in token"}, status=400)

        psk_val = request.app.get("link_psk")
        token_val = str(request.app.get("link_token") or "")

        # 正向 (client→server): 从 client 的 /hls 拉 → 转发到 server 的 /recv
        logging.info(
            "gateway 注册完成，启动正向 pull+worker: client_url=%s -> server_url=%s  link_token=%s psk=%s",
            client_url,
            server_url,
            bool(token_val),
            psk_val is not None,
        )
        async def _safe_task(name: str, coro):
            try:
                await coro
            except asyncio.CancelledError:
                logging.info("gateway task %s 被取消", name)
                raise
            except Exception:
                logging.exception("gateway task %s 异常退出，5秒后重试", name)
                await asyncio.sleep(5)
                # 不重新抛出，让 task 自然结束（外部无法重启）

        # 启动共享的可靠性控制面（只启动一次，供两个 worker 共用）
        async def _start_control_server():
            from .reliability import run_control_server as start_ctrl, ReliabilityConfig
            import httpx
            from redis.asyncio import Redis
            rcfg = cfg.reliability
            if not rcfg.enabled:
                return None
            r = Redis.from_url(cfg.redis_url, decode_responses=False)
            await r.ping()
            client = httpx.AsyncClient(timeout=120.0, verify=False, trust_env=False)
            runner = await start_ctrl(
                host=cfg.control_host, port=cfg.control_port,
                redis=r, http_client=client, rcfg=rcfg)
            request.app["_control_runner"] = runner
            request.app["_control_redis"] = r
            request.app["_control_client"] = client
            return runner

        request.app["_control_task"] = asyncio.create_task(_start_control_server())

        request.app["pull_task"] = asyncio.create_task(
            _safe_task("pull_fwd", run_hls_puller(
                source_base_url=client_url,
                session_id="bishe-1",
                redis_url=cfg.redis_url,
                queue_key=cfg.queue_key,
                poll_interval_s=cfg.poll_interval_s,
                segment_interval_s=cfg.segment_interval_s,
                hls_prefix="/hls",
            ))
        )
        request.app["worker_task"] = asyncio.create_task(
            _safe_task("worker_fwd", run_stego_worker(
                redis_url=cfg.redis_url,
                queue_key=cfg.queue_key,
                server_base_url=server_url,
                psk=psk_val,
                link_token=token_val,
                reliability=cfg.reliability,
                recv_endpoint="/recv",
                direction_label="fwd",
            ))
        )

        # 反向 (server→client): 从 server 的 /hls-rev 拉 → 转发到 client 的 /overlay/recv-tunnel
        logging.info(
            "gateway 注册完成，启动反向 pull+worker: server_url=%s -> client_url=%s",
            server_url,
            client_url,
        )
        request.app["pull_task_rev"] = asyncio.create_task(
            _safe_task("pull_rev", run_hls_puller(
                source_base_url=server_url,  # 从 server 拉取
                session_id="bishe-rev-1",
                redis_url=cfg.redis_url,
                queue_key=cfg.queue_key_rev,
                poll_interval_s=cfg.poll_interval_s,
                segment_interval_s=cfg.segment_interval_s,
                hls_prefix="/hls-rev",
            ))
        )
        request.app["worker_task_rev"] = asyncio.create_task(
            _safe_task("worker_rev", run_stego_worker(
                redis_url=cfg.redis_url,
                queue_key=cfg.queue_key_rev,
                server_base_url=client_url,  # 转发到 client
                psk=psk_val,
                link_token=token_val,
                reliability=cfg.reliability,
                recv_endpoint="/overlay/recv-tunnel",
                direction_label="rev",
            ))
        )

    return web.json_response(
        {
            "ok": True,
            "role": role,
            "registered": dict(reg),
            "pull_started": request.app.get("pull_task") is not None,
        }
    )


async def handle_stop(request: web.Request) -> web.Response:
    cfg: GatewayConfig = request.app["cfg"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    role = str(body.get("role") or "").strip().lower()
    token = str(body.get("token") or "").strip()
    if role not in ("client", "server"):
        return web.json_response({"ok": False, "error": "role must be 'client' or 'server'"}, status=400)
    if not token:
        return web.json_response({"ok": False, "error": "missing token"}, status=400)
    try:
        claims = _claims_from_token(cfg, token)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"bad token: {e!r}"}, status=400)

    pull_task = request.app.get("pull_task")
    worker_task = request.app.get("worker_task")
    pull_task_rev = request.app.get("pull_task_rev")
    worker_task_rev = request.app.get("worker_task_rev")
    for t in (pull_task, worker_task, pull_task_rev, worker_task_rev):
        if t:
            t.cancel()

    # 通知 client/server（尽力而为）
    await _post_stop_notice(claims.a_url.rstrip("/") + "/overlay/stop-notice", who="gateway")
    await _post_stop_notice(claims.c_url.rstrip("/") + "/stop-notice", who="gateway")

    _reset_gateway_state(request.app)

    return web.json_response({"ok": True, "stopped_by": role})


async def handle_health(request: web.Request) -> web.Response:
    reg = request.app["reg"]
    claims: TokenClaims | None = request.app.get("claims")
    return web.json_response(
        {
            "ok": True,
            "registered": dict(reg),
            "client_url": claims.a_url if claims else "",
            "server_url": claims.c_url if claims else "",
            "pull_started": request.app.get("pull_task") is not None,
            "worker_started": request.app.get("worker_task") is not None,
            "pull_started_rev": request.app.get("pull_task_rev") is not None,
            "worker_started_rev": request.app.get("worker_task_rev") is not None,
        }
    )


async def run_gateway(
    *,
    host: str,
    port: int,
    redis_url: str,
    queue_key: str,
    poll_interval_s: float,
    segment_interval_s: float = 0.0,
    token_secret: str,
    control_host: str = "127.0.0.1",
    control_port: int | None = None,
    reliability: ReliabilityConfig | None = None,
    queue_key_rev: str = "bishe:overlay:queue:rev",
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cport = int(control_port) if control_port is not None else int(port) + 1
    rcfg = reliability if reliability is not None else ReliabilityConfig()
    cfg = GatewayConfig(
        host=host,
        port=port,
        redis_url=redis_url,
        queue_key=queue_key,
        poll_interval_s=poll_interval_s,
        segment_interval_s=max(0.0, float(segment_interval_s)),
        token_secret=token_secret,
        control_host=control_host,
        control_port=cport,
        reliability=rcfg,
        queue_key_rev=queue_key_rev,
    )

    app = web.Application()
    app["cfg"] = cfg
    app["reg"] = {"client": False, "server": False}
    app["claims"] = None
    app["link_psk"] = None
    app["link_token"] = ""
    app["pull_task"] = None
    app["worker_task"] = None
    app["pull_task_rev"] = None
    app["worker_task_rev"] = None
    app.router.add_get("/health", handle_health)
    app.router.add_post("/register", handle_register)
    app.router.add_post("/stop", handle_stop)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    logging.info("gateway 启动: http://%s:%d (POST /register role=client|server)", host, port)
    await site.start()

    while True:
        await asyncio.sleep(3600)


def env_token_secret() -> str:
    return os.environ.get("BISHE_TOKEN_SECRET", "bishe-dev-secret")
