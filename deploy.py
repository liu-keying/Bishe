#!/usr/bin/env python3
"""分布式部署脚本：通过 SSH 将各节点部署到多台机器并启动，实时流式回传日志。

用法:
  python3 deploy.py up      --config deploy_config.json    # 部署并启动，实时流式输出日志
  python3 deploy.py down    --config deploy_config.json    # 停止全部节点
  python3 deploy.py sync    --config deploy_config.json    # 仅同步代码（不启动）
  python3 deploy.py status  --config deploy_config.json    # 查看各节点状态
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------

REQUIRED_NODES = ["control", "gateway", "server", "client"]
START_ORDER = ["control", "gateway", "server", "client"]
STOP_ORDER = ["client", "server", "gateway", "control"]

# 终端颜色（用于日志前缀）
COLORS = {
    "control": "\033[35m",   # 紫
    "gateway": "\033[33m",   # 黄
    "server":  "\033[36m",   # 青
    "client":  "\033[32m",   # 绿
}
RESET = "\033[0m"


class DeployConfig:
    def __init__(self, path: str):
        with open(path) as f:
            raw = json.load(f)

        self.token_secret = raw["token_secret"]
        self.ssh_user = raw.get("ssh_user", "root")
        self.ssh_key = raw.get("ssh_key") or None
        self.remote_dir = raw.get("remote_dir", "/opt/bishe")
        self.python = raw.get("python", "python3")
        self.session = raw.get("session", "bishe-1")
        self.queue = raw.get("queue", "bishe:overlay:queue")
        self.queue_rev = raw.get("queue_rev", "bishe:overlay:queue:rev")
        self.reliability = raw.get("reliability", True)
        self.mode = raw.get("mode", "inject")
        self.redis = raw.get("redis") or {}

        self.nodes: dict[str, dict] = raw.get("nodes", {})
        for role in REQUIRED_NODES:
            if role not in self.nodes:
                raise SystemExit(f"配置缺少必填节点: {role}")

        for role, nd in self.nodes.items():
            nd.setdefault("role", role)
            nd.setdefault("host", "127.0.0.1")
            nd.setdefault("port", _default_port(role))
            nd["url"] = f"http://{nd['host']}:{nd['port']}"

    # ---- URL helpers ----

    @property
    def redis_url(self) -> str:
        r = self.redis
        return f"redis://{r.get('host', '127.0.0.1')}:{r.get('port', 6379)}/0"

    @property
    def redis_host_port(self) -> str:
        r = self.redis
        return f"{r.get('host', '127.0.0.1')}:{r.get('port', 6379)}"

    def node_url(self, role: str) -> str:
        return self.nodes[role]["url"]

    # ---- SSH 工具 ----

    def _ssh_opts(self) -> list[str]:
        opts = ["-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
        if self.ssh_key:
            opts.extend(["-i", os.path.expanduser(self.ssh_key)])
        return opts

    def ssh_target(self, role: str) -> str:
        nd = self.nodes[role]
        return f"{self.ssh_user}@{nd['host']}"

    # ---- 各节点 CLI 参数 ----

    def args_for(self, role: str) -> str:
        return getattr(self, f"_args_{role}")()

    def _args_control(self) -> str:
        nd = self.nodes["control"]
        return (
            f"control --host 0.0.0.0 --port {nd['port']}"
            f" --token-secret {self.token_secret}"
        )

    def _args_gateway(self) -> str:
        nd = self.nodes["gateway"]
        parts = [
            f"gateway --host 0.0.0.0 --port {nd['port']}",
            f"--redis {self.redis_url}",
            f"--queue {self.queue}",
            f"--token-secret {self.token_secret}",
        ]
        if self.mode == "socks5":
            parts.append(f"--queue-rev {self.queue_rev}")
        if not self.reliability:
            parts.append("--no-reliability")
        return " ".join(parts)

    def _args_server(self) -> str:
        nd = self.nodes["server"]
        parts = [
            f"server --host 0.0.0.0 --port {nd['port']}",
            f"--control-url {self.node_url('control')}",
            f"--client-url {self.node_url('client')}",
            f"--gateway-url {self.node_url('gateway')}",
        ]
        if not self.reliability:
            parts.append("--no-reliability")
        else:
            gw_control_port = self.nodes["gateway"].get("control_port", 8011)
            gw_host = self.nodes["gateway"]["host"]
            parts.append(
                f"--gateway-callback-url http://{gw_host}:{gw_control_port}/overlay/recv-ack"
            )
        if nd.get("ssl_cert") and nd.get("ssl_key"):
            parts.append(f"--ssl-cert {nd['ssl_cert']} --ssl-key {nd['ssl_key']}")
        if self.mode == "socks5":
            parts.append("--socks-max-conns 50")
        return " ".join(parts)

    def _args_client(self) -> str:
        nd = self.nodes["client"]
        parts = [
            f"client --host 0.0.0.0 --port {nd['port']}",
            f"--session {self.session}",
            f"--control-url {self.node_url('control')}",
            f"--server-url {self.node_url('server')}",
            f"--gateway-url {self.node_url('gateway')}",
        ]
        socks_port = int(nd.get("socks_port") or 0)
        if socks_port > 0:
            parts.append(f"--socks-port {socks_port}")
        return " ".join(parts)


def _default_port(role: str) -> int:
    return {"control": 8009, "gateway": 8010, "server": 8002, "client": 8000}.get(role, 8000)


# ---------------------------------------------------------------------------
# SSH 原语
# ---------------------------------------------------------------------------

def _ssh(cfg: DeployConfig, target: str, cmd: str) -> subprocess.CompletedProcess:
    """在远程执行命令，stdout/stderr 透传。"""
    opts = cfg._ssh_opts()
    full = ["ssh"] + opts + [target, cmd]
    print(f"  [{target}] $ {cmd[:120]}{'...' if len(cmd) > 120 else ''}")
    return subprocess.run(full)


def _ssh_capture(cfg: DeployConfig, target: str, cmd: str) -> str:
    """远程执行并捕获 stdout。"""
    opts = cfg._ssh_opts()
    r = subprocess.run(["ssh"] + opts + [target, cmd], capture_output=True, text=True)
    return r.stdout.strip()


def _rsync(cfg: DeployConfig, target: str) -> None:
    """rsync 项目到远程主机。"""
    opts = cfg._ssh_opts()
    ssh_opts_str = " ".join(opts)
    project_root = Path(__file__).resolve().parent
    src_dir = str(project_root) + "/"
    cmd = (
        f"rsync -az -e 'ssh {ssh_opts_str}'"
        f" --exclude='__pycache__' --exclude='.git' --exclude='.venv'"
        f" --exclude='*.pyc' --exclude='deploy_config.json'"
        f" --exclude='logs/' --exclude='run/'"
        f" {src_dir} {target}:{cfg.remote_dir}/"
    )
    print(f"  rsync -> {target}:{cfg.remote_dir}/")
    subprocess.run(cmd, shell=True, check=True)


# ---------------------------------------------------------------------------
# 日志流式回传
# ---------------------------------------------------------------------------

def _stream_log(cfg: DeployConfig, role: str, stop_event: threading.Event) -> None:
    """通过 SSH tail -F 实时拉取单个节点的日志到控制机 stdout。"""
    target = cfg.ssh_target(role)
    log_file = f"{cfg.remote_dir}/logs/{role}.log"
    color = COLORS.get(role, "")
    prefix = f"{color}[{role}]{RESET} "

    opts = cfg._ssh_opts()
    # 先等日志文件出现
    _ = subprocess.run(
        ["ssh"] + opts + [target, f"while [ ! -f {log_file} ]; do sleep 0.5; done"],
        capture_output=True, timeout=30,
    )

    # tail -F（跟进轮转），持续输出直到 stop_event
    proc = subprocess.Popen(
        ["ssh"] + opts + [target, f"tail -F {log_file}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )

    def _reader() -> None:
        assert proc.stdout
        for line in iter(proc.stdout.readline, ""):
            if stop_event.is_set():
                break
            sys.stdout.write(prefix + line)
            sys.stdout.flush()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    # 等待 stop_event 或进程退出
    while not stop_event.is_set() and proc.poll() is None:
        time.sleep(0.5)

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# 操作
# ---------------------------------------------------------------------------

def do_sync(cfg: DeployConfig) -> None:
    """同步代码到所有主机（同一主机只传一次）。"""
    hosts: dict[str, str] = {}
    for role in REQUIRED_NODES:
        h = cfg.nodes[role]["host"]
        if h not in hosts:
            hosts[h] = role

    def sync_one(host: str) -> None:
        target = f"{cfg.ssh_user}@{host}"
        _ssh(cfg, target, f"mkdir -p {cfg.remote_dir} {cfg.remote_dir}/logs {cfg.remote_dir}/run")
        try:
            _rsync(cfg, target)
        except subprocess.CalledProcessError:
            print(f"  rsync 失败，检查 SSH 连通性")
            raise
        _ssh(cfg, target,
             f"cd {cfg.remote_dir} && {cfg.python} -m pip install -q -r requirements.txt")

    print(f"\n=== 同步代码到 {len(hosts)} 台主机 ===")
    with ThreadPoolExecutor(max_workers=min(4, len(hosts))) as pool:
        list(pool.map(sync_one, hosts.keys()))
    print("同步完成\n")


def do_start_one(cfg: DeployConfig, role: str) -> None:
    """启动单个节点。"""
    args = cfg.args_for(role)
    target = cfg.ssh_target(role)
    pid_file = f"{cfg.remote_dir}/run/{role}.pid"

    # 先杀旧进程，再启动
    script = (
        f"pkill -F {pid_file} 2>/dev/null || true; "
        f"sleep 0.5; "
        f"cd {cfg.remote_dir} && "
        f"nohup {cfg.python} main.py {args} > {cfg.remote_dir}/logs/{role}.log 2>&1 & "
        f"echo $! > {pid_file}"
    )
    _ssh(cfg, target, script)


def do_stop_one(cfg: DeployConfig, role: str) -> str:
    """停止单个节点。"""
    target = cfg.ssh_target(role)
    pid_file = f"{cfg.remote_dir}/run/{role}.pid"
    return _ssh_capture(
        cfg, target,
        f"kill $(cat {pid_file} 2>/dev/null) 2>/dev/null && echo stopped || echo 'not running'"
    )


def do_health(cfg: DeployConfig, role: str) -> str:
    """检查节点健康状态。"""
    url = f"{cfg.node_url(role)}/health"
    try:
        r = subprocess.run(["curl", "-sf", "--max-time", "3", url],
                           capture_output=True, text=True)
        return "OK" if r.returncode == 0 else f"FAIL({r.returncode})"
    except Exception as e:
        return f"ERR({e})"


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def cmd_up(cfg: DeployConfig) -> None:
    print("=" * 60)
    print("Bishe 分布式部署")
    print(f"  control : {cfg.node_url('control')}")
    print(f"  gateway : {cfg.node_url('gateway')}  (redis: {cfg.redis_host_port})")
    print(f"  server  : {cfg.node_url('server')}")
    print(f"  client  : {cfg.node_url('client')}")
    print(f"  mode    : {cfg.mode}  |  reliability: {cfg.reliability}")
    print("=" * 60)

    # 1. 同步代码
    do_sync(cfg)

    # 2. 按序启动各节点
    print("\n=== 启动节点 ===")
    for role in START_ORDER:
        nd = cfg.nodes[role]
        print(f"\n--- {role} ({nd['host']}:{nd['port']}) ---")
        do_start_one(cfg, role)
        time.sleep(2)
        status = do_health(cfg, role)
        marker = "✓" if status == "OK" else "✗"
        print(f"  {marker} {role}: {status}")

    # 3. 启动日志流式回传（后台线程）
    print("\n" + "=" * 60)
    print("日志实时回传（Ctrl+C 停止）")
    print("=" * 60 + "\n")
    sys.stdout.flush()

    stop_event = threading.Event()
    threads = []
    for role in START_ORDER:
        t = threading.Thread(target=_stream_log, args=(cfg, role, stop_event), daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n正在停止日志流...")
        stop_event.set()
        for t in threads:
            t.join(timeout=3)
        print("已退出日志流（节点仍在运行，用 'down' 停止）")


def cmd_down(cfg: DeployConfig) -> None:
    print("=== 停止全部节点 ===")
    for role in STOP_ORDER:
        out = do_stop_one(cfg, role)
        print(f"  {role}: {out}")
    print("已停止")


def cmd_sync(cfg: DeployConfig) -> None:
    do_sync(cfg)
    print("同步完成（未重启节点）")


def cmd_status(cfg: DeployConfig) -> None:
    print(f"{'节点':<10} {'URL':<30} {'状态':<10}")
    print("-" * 50)
    for role in START_ORDER:
        status = do_health(cfg, role)
        marker = "✓ OK" if status == "OK" else f"✗ {status}"
        print(f"{role:<10} {cfg.node_url(role):<30} {marker}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Bishe 分布式部署工具")
    p.add_argument("command", choices=["up", "down", "sync", "status"],
                   help="up=部署+启动+日志 / down=停止 / sync=仅同步代码 / status=查看状态")
    p.add_argument("--config", default="deploy_config.json",
                   help="配置文件路径 (default: deploy_config.json)")
    args = p.parse_args()

    if not os.path.exists(args.config):
        raise SystemExit(
            f"配置文件不存在: {args.config}\n"
            f"请参考 deploy_config.example.json 创建"
        )

    cfg = DeployConfig(args.config)

    if args.command == "up":
        cmd_up(cfg)
    elif args.command == "down":
        cmd_down(cfg)
    elif args.command == "sync":
        cmd_sync(cfg)
    elif args.command == "status":
        cmd_status(cfg)


if __name__ == "__main__":
    main()
