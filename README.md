# 应用层隐匿通道 Demo（Python）

基于 HLS 视频流的隐匿通信系统，将数据加密嵌入 TS 分片中传输。

| 模式 | 说明 |
|------|------|
| **隐匿数据注入** (`inject`) | 数据加密嵌入 TS 分片，单向传输（client → gateway → server） |
| **SOCKS5 代理** (`socks5`) | client 提供 SOCKS5 入口，双向透明代理 TCP 流量 |

---

## 架构

| 节点 | 作用 |
|------|------|
| **control** | 控制面：client/server 密钥交换（RSA-OAEP + AES-256-GCM），签发 Token |
| **gateway** | 数据面网关：client/server 注册后自动启动拉流与转发 |
| **client** | HLS 源站 + 隐匿嵌入；可选 SOCKS5 代理入口 |
| **server** | 接收端：解密 + 统计；可选 TCP 出口（代理模式） |
| **Redis** | 仅 gateway 访问，消息队列 + 密文分片拼装缓冲 |

```
# 单向注入
发送方 → client(/overlay/embed-hls) → HLS 分片 → gateway 拉流提取 → server(/recv) 解密

# SOCKS5 代理
客户端 → SOCKS5 → client → /hls → gateway → server(/recv) → TCP → 目标
                              gateway ← /hls-rev ← server ← TCP 响应 ←┘
```

### 伪装分片

SOCKS5 模式下无需提供 TS 文件——client 和 server 自动生成结构完整的伪装 MPEG-TS 分片作为密文载体：每 188 字节以 `0x47` 同步头对齐，包含 PAT(pid 0) → PMT → 视频 PES 包，PID 和 continuity_counter 动态变化，PES 头含随机 PTS/DTS。抓包即为正常 TS 流。

---

## 快速开始

### 多机部署（deploy.py）

```bash
cp deploy_config.example.json deploy_config.json
# 编辑 deploy_config.json，填写各节点 IP

python3 deploy.py up     # 同步代码 → 安装依赖 → 启动 Redis+四节点
python3 deploy.py status # 查看状态
python3 deploy.py down   # 停止全部
```

### 本地单机（5 个终端）

```bash
# 1) Redis
docker run --rm -p 6379:6379 redis:7

# 2) control
python main.py control --host 127.0.0.1 --port 8009 --token-secret bishe-dev-secret

# 3) gateway
python main.py gateway --host 127.0.0.1 --port 8010 \
  --redis redis://127.0.0.1:6379/0 --queue bishe:overlay:queue \
  --token-secret bishe-dev-secret

# 4) server
python main.py server --host 127.0.0.1 --port 8002 \
  --control-url http://127.0.0.1:8009 --client-url http://127.0.0.1:8000 \
  --gateway-url http://127.0.0.1:8010

# 5) client
python main.py client --host 127.0.0.1 --port 8000 --session bishe-1 \
  --control-url http://127.0.0.1:8009 --server-url http://127.0.0.1:8002 \
  --gateway-url http://127.0.0.1:8010
```

---

## deploy.py 使用指南

### 配置文件

```jsonc
{
  "token_secret": "",            // 留空自动生成；control 和 gateway 必须一致
  "ssh_user": "root",           // 全局默认 SSH 用户（各节点可覆盖）
  "ssh_key": "~/.ssh/id_rsa",   // 全局默认私钥（各节点可覆盖）
  "ssh_password": null,         // 全局默认密码（与 ssh_key 二选一）
  "remote_dir": "bishe",        // 部署目录；相对路径 = 远程 $HOME/ 下

  "redis": {                    // 与 gateway 同机则自动管理
    "host": "10.0.0.1",
    "port": 6379
  },

  "nodes": {                    // 各节点分配到哪些机器（可同机）
    "control": {"host": "10.0.0.1", "port": 8009},
    "gateway": {"host": "10.0.0.1", "port": 8010, "control_port": 8011},
    "server":  {"host": "10.0.0.2", "port": 8002,
                "ssh_user": "admin", "ssh_password": "xxx"},
    "client":  {"host": "10.0.0.3", "port": 8000, "socks_port": 0,
                "ssh_key": "~/.ssh/client_key"}
  },

  "mode": "inject",             // "inject"=隐匿注入 / "socks5"=SOCKS5 代理
  "session": "bishe-1",
  "reliability": true
}
```

| 字段 | 说明 |
|------|------|
| `mode` | `inject`=单向注入，`socks5`=SOCKS5 代理（自动启用反向队列和 SOCKS5 端口） |
| `socks_port` | SOCKS5 端口，设为 0 则禁用（inject 模式自动忽略） |
| `ssh_*` | 全局凭据，每个节点可用自己的 `ssh_user`/`ssh_key`/`ssh_password` 覆盖 |
| `remote_dir` | 远程部署目录，相对路径展开为 `$HOME/<dir>` |
| `redis.host` | 与 `gateway.host` 同机则自动通过 apt/docker 管理 Redis |
| `control_port` | gateway 的可靠性回执端口（server 解密后向此端口发送 ACK） |

### 子命令

| 命令 | 作用 |
|------|------|
| `python3 deploy.py up` | 同步代码 → 启动 Redis → 启动四节点 → 定期拉取日志到 `./bishe-logs/` |
| `python3 deploy.py down` | 停止全部节点和 Redis |
| `python3 deploy.py sync` | 仅同步代码（不重启） |
| `python3 deploy.py status` | 检查各节点 `/health` |

### 工作流程

1. **按 host 去重**，并行 rsync 项目 → 创建 venv → pip install
2. **启动 Redis**（与 gateway 同机时自动通过 apt 或 docker）
3. **按序启动节点**：control → gateway → server → client
4. **健康检查**：每节点 curl `/health`（最多等 10 秒）
5. **定期拉取日志**：每 5 秒 scp 各节点日志到 `./bishe-logs/`

### SSH 凭据

支持三级优先级（节点 > 全局 > 默认），密码和密钥二选一：

```jsonc
{
  "ssh_user": "root",           // 全局默认
  "ssh_password": "global-pwd", // 全局默认
  "nodes": {
    "server": {
      "ssh_user": "admin",              // 覆盖全局
      "ssh_password": "server-pass-123"
    },
    "client": {
      "ssh_key": "~/.ssh/client_key"    // 用密钥替代密码
    }
  }
}
```

---

## 手动部署参考

每个节点独立启动。`<>` 替换为实际 IP/端口。

### control

控制面，最先启动。`--token-secret` 需和 gateway 一致。

```bash
python main.py control \
  --host <ip> --port 8009 \
  --token-secret bishe-dev-secret
```

### gateway

数据面网关，依赖 Redis。`--control-port` 是可靠性回执端口（server 解密后向此端口发 ACK）。

**inject 模式：**
```bash
python main.py gateway \
  --host <ip> --port 8010 \
  --redis redis://127.0.0.1:6379/0 \
  --queue bishe:overlay:queue \
  --token-secret bishe-dev-secret \
  --control-port 8011
```

**socks5 模式**（多一个反向队列）：
```bash
python main.py gateway \
  --host <ip> --port 8010 \
  --redis redis://127.0.0.1:6379/0 \
  --queue bishe:overlay:queue \
  --queue-rev bishe:overlay:queue:rev \
  --token-secret bishe-dev-secret \
  --control-port 8011
```

关掉可靠性回执（不等待 server ACK）：
```bash
python main.py gateway ... --no-reliability
```

### server

接收端。`--control-url` 和 `--client-url` 用于 PSK 交换，必须填对方可达的地址。

```bash
python main.py server \
  --host <ip> --port 8002 \
  --control-url http://<control-ip>:8009 \
  --client-url http://<client-ip>:8000 \
  --gateway-url http://<gateway-ip>:8010
```

启用可靠性回执：
```bash
python main.py server ... \
  --gateway-callback-url http://<gateway-ip>:8011/overlay/recv-ack
```

启用 HTTPS（先准备证书）：
```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
  -days 365 -nodes -subj "/CN=<server-ip>"

python main.py server \
  --host <ip> --port 8443 --ssl-cert cert.pem --ssl-key key.pem \
  --control-url http://<control-ip>:8009 \
  --client-url http://<client-ip>:8000 \
  --gateway-url http://<gateway-ip>:8010
```

### client

源站。`--server-url` 用于 PSK 交换，必须填 server 可达的地址。

**inject 模式：**
```bash
python main.py client \
  --host <ip> --port 8000 \
  --session bishe-1 \
  --control-url http://<control-ip>:8009 \
  --server-url http://<server-ip>:8002 \
  --gateway-url http://<gateway-ip>:8010
```

**socks5 模式**（加 `--socks-port` 开启代理）：
```bash
python main.py client \
  --host <ip> --port 8000 --socks-port 1080 \
  --session bishe-1 \
  --control-url http://<control-ip>:8009 \
  --server-url http://<server-ip>:8002 \
  --gateway-url http://<gateway-ip>:8010
```

### 使用

**注入数据：**
```bash
curl -X POST "http://<client-ip>:8000/overlay/embed-hls?k=1" \
  -F "segment=@hls_seg000.ts" -F "hidden=@hidden.bin"
curl http://<server-ip>:8002/stats
```

**SOCKS5 代理：**
```bash
curl --socks5 <client-ip>:1080 http://httpbin.org/get
# 浏览器配置 SOCKS5 代理 <client-ip>:1080
```

### 停止链路

```bash
curl -X POST http://<gateway-ip>:8010/stop \
  -H "Content-Type: application/json" \
  -d '{"role":"client","token":"<token>"}'
```

### 密文分片参数

| 参数 | 含义 |
|------|------|
| `k` | 密文分片份数，`1 ≤ k ≤ n`，默认 `k=n` |
| `g_bytes` | 每片密文槽位长度（字节），默认 `⌈len(密文)/k⌉`

---

## 技术要点

### 密钥协商

1. client 和 server 各自生成 RSA-2048 密钥对，携公钥向 control 请求
2. control 待双方公钥到齐后，生成 32 字节 PSK（AES-256-GCM）
3. 用各方的 RSA 公钥加密下发 PSK + HMAC 签名的 Token
4. client/server 拿到 PSK 和 Token 后向 gateway 注册，gateway 验签通过后启动拉流

### 隐匿嵌入（stego）

`psk_hmac_inplace` 方案：用 HMAC(token, hls_index, frag_idx) 派生 tag 和伪随机偏移，在 TS 分片内等长替换写入 `tag || cipher_fragment`。不改变分片总长度，外观为合法 TS 流。非承载分片写入等长随机填充。

### 端到端加密

`msg_id(16B) || "PSK1"(4B) || nonce(12B) || AES-256-GCM(body, aad=session_id)`

### SOCKS5 隧道

握手通过 HLS 发送 `TunnelCtl(connect)` → server 建连 TCP → 回传 `TunnelCtl(connected)` → 数据双向经 PSK1 AEAD 加密嵌入 HLS 分片转发。每个 TCP 连接分配唯一 `conn_id`（UUID）。

### 会话轮换

client 每发完一批分片递增 `session_id`（`bishe-1` → `bishe-2` → …），gateway 通过 `X-Next-Session` 响应头自动跟随。

---

## 项目结构

```
bishe_demo/
├── client_proxy.py    # client：HLS 源站 + 隐匿嵌入 + SOCKS5 入口
├── gateway.py         # gateway：注册编排，启动拉流和 worker，管理控制面
├── server.py          # server：接收解密 + 反向 HLS + TCP 出口
├── control_server.py  # control：密钥协商 + token 签发
├── hls_puller.py      # HLS 拉流客户端（gateway 内部使用）
├── stego_worker.py    # stego 提取 + 密文拼装 + 转发（gateway 内部使用）
├── hls_shared.py      # 共享 HLS 路由（client/server 复用）
├── reliability.py     # 可靠性：server 回执 + 超时重发
├── tunnel.py          # SOCKS5 隧道数据模型 + TCP 出口连接池
├── stego.py           # 隐匿嵌入/提取算法（psk_hmac_inplace）
├── psk_aead.py        # PSK1 格式 AEAD 加解密
├── crypto_box.py      # RSA 密钥对 + OAEP 封装
├── token_util.py      # HMAC 签名 token
├── common.py          # OverlayEnvelope + HLS 路径工具
└── hls_facade.py      # m3u8 播放列表解析
main.py                # 统一入口
deploy.py              # 分布式部署脚本
deploy_config.example.json
```
