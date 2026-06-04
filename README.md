# 应用层隐匿通道 Demo（Python）

B 端以 **HLS 拉流** 形态访问 A：`GET master.m3u8` → `GET index.m3u8` → `GET seg-*.ts`，从 TS 尾部取出隐匿载荷后经 **B-gate** 转发到 **C**。

| 节点 | 作用 |
|------|------|
| **E** | 控制面：A/C 交换公钥后下发 **token + PSK** |
| **B-gate** | 数据面网关：A、C 均 `POST /register` 后，在进程内启动拉流与转发（无需单独起 `b-pull` / `b-worker`） |
| **A** | 本地源站：`POST /overlay/embed-hls` 嵌入隐匿数据；对外提供 HLS 路径 |
| **C** | 接收端：`POST /recv` 解密并统计 |

链路：**发送方 POST A** → **B-gate 拉 A 分片** → **POST C**。

## 环境

- Python 3.10+（建议 3.11/3.12）
- Redis

```bash
pip install -r requirements.txt
```

```bash
docker run --rm -p 6379:6379 redis:7
```

以下示例默认本机地址；`--token-secret` 在 **E** 与 **B-gate** 上必须一致（也可用环境变量 `BISHE_TOKEN_SECRET`，默认 `bishe-dev-secret`）。

## 启动（推荐：B-gate）

开 **5 个终端**（或后台运行），按顺序执行。

### 1) Redis

见上文 `docker run`。

### 2) E（控制面，8009）

```bash
python main.py e --host 127.0.0.1 --port 8009 --token-secret bishe-dev-secret
```

### 3) B-gate（8010，控制面 8011）

```bash
python main.py b-gate --host 127.0.0.1 --port 8010 --redis redis://127.0.0.1:6379/0 --queue bishe:overlay:queue --token-secret bishe-dev-secret
```

健康检查：`GET http://127.0.0.1:8010/health`（`pull_started` 在 A、C 都注册后为 `true`）。

### 4) C（8002）

须与 A 使用同一 **E** 完成密钥交换，并向 B-gate 注册后，拉流才会开始。

```bash
python main.py c --host 127.0.0.1 --port 8002 --e-url http://127.0.0.1:8009 --a-url http://127.0.0.1:8000 --b-gate-url http://127.0.0.1:8010
```

### 5) A（8000）

```bash
python main.py a --host 127.0.0.1 --port 8000 --session bishe-1 --e-url http://127.0.0.1:8009 --c-url http://127.0.0.1:8002 --b-gate-url http://127.0.0.1:8010
```

日志中应依次出现：A/C 从 E 获取 PSK → 向 B-gate 注册 → B-gate 打印「注册完成，启动 b-pull + b-worker」（进程内，无需你再起这两个命令）。

**会话 ID**：A 与 B-gate 内拉流均使用 `bishe-1` 起始；每消费一片媒介分片，A 会递增并在响应头带 `X-Next-Session`，拉流侧会自动跟随。

## 发送隐匿数据

准备若干 **TS 分片**（仓库内示例：`scripts/_test_ts/hls_seg*.ts`）和任意二进制 **`hidden.bin`**。

```powershell
cd D:\PyProjects\Bishe
curl.exe -X POST "http://127.0.0.1:8000/overlay/embed-hls" `
  -F "segment=@.\scripts\_test_ts\hls_seg000.ts" `
  -F "segment=@.\scripts\_test_ts\hls_seg001.ts" `
  -F "hidden=@hidden.bin"
```

说明：

- 须先完成 E + A/C + B-gate 启动（A 上有 PSK），否则 `/overlay/embed-hls` 会返回 503。
- `hidden` 先整包 **AEAD 加密** 得到密文，再按 **`k` 切成 k 段**；B-gate 收齐同组 k 段后拼接，再 POST 给 C 解密。

### 密文分片参数 `k`（不是“只塞最后一段”）

上传 `n` 个 `segment` 时，可用查询参数 **`k`**（或 `cipher_k`）指定密文被切成几份、塞进几个 TS：

| 参数 | 含义 |
|------|------|
| `k` | 密文分片份数，须 `1 ≤ k ≤ n`；**不写时默认 `k = n`**（每个 segment 各承载一片） |
| `g_bytes` / `g_bits` / `g` | 每片密文槽位长度（字节）；默认 `g_bytes = ⌈len(密文)/k⌉`，密文不足会随机补齐到 `k × g_bytes` |

流程（A 侧）：

1. 密文均分为 `k` 段，每段长度 `g_bytes`。
2. 在 `n` 个分片下标里 **随机无放回抽取 `k` 个位置**（`random.sample`，与先后顺序无关，**不一定是最后一个**）。
3. **被抽中的分片**：在 TS 内部用 `psk_hmac_inplace` **等长替换** 写入 `HMAC 标记 + 密文片段`（offset 由 token 与分片序号派生）。
4. **其余分片**：写入等长随机填充（`embed_pad_inplace`），体积与承载片一致，外观更接近“每片都有扰动”。
5. 同一次嵌入共用 `cipher_group`；B-gate 按组收齐 `k` 片后再交给 C。

示例：`n=6` 个 segment，只拆成 **3** 份密文并随机落到 3 个分片上（例如第 1、3、5 片，每次可能不同）：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/overlay/embed-hls?k=3" `
  -F "segment=@.\scripts\_test_ts\hls_seg000.ts" `
  -F "segment=@.\scripts\_test_ts\hls_seg001.ts" `
  -F "segment=@.\scripts\_test_ts\hls_seg002.ts" `
  -F "segment=@.\scripts\_test_ts\hls_seg003.ts" `
  -F "segment=@.\scripts\_test_ts\hls_seg004.ts" `
  -F "segment=@.\scripts\_test_ts\hls_seg005.ts" `
  -F "hidden=@hidden.bin"
```

`k=1` 时整段密文只占 **1 个** 随机选中的 TS；`k=n` 且上传 `n` 片时，**每片各带一段密文**（下标 0…n−1 全覆盖）。

## 查看 C 是否收到

```bash
curl http://127.0.0.1:8002/stats
```

`recv_count` / `recv_bytes` 增加即表示端到端打通。

## C 使用 HTTPS（可选）

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"
```

```bash
python main.py c --host 127.0.0.1 --port 8443 --ssl-cert cert.pem --ssl-key key.pem --e-url http://127.0.0.1:8009 --a-url http://127.0.0.1:8000 --b-gate-url http://127.0.0.1:8010
```

A 的 `--c-url` 改为 `https://127.0.0.1:8443`；token 中的 `c_url` 由 C 启动时上报给 E，须与 C 实际监听地址一致。

## 可靠性回执（可选，默认可不配）

需要「C 解密后通知 B、B 超时重发」时，在 **C 的完整启动命令** 末尾加上 `--b-callback-url`（`...` 不是参数，不要照抄）：

```bash
python main.py c --host 127.0.0.1 --port 8002 --e-url http://127.0.0.1:8009 --a-url http://127.0.0.1:8000 --b-gate-url http://127.0.0.1:8010 --b-callback-url http://127.0.0.1:8011/overlay/recv-ack
```

B-gate 在 **8011** 监听 `POST /overlay/recv-ack`（默认 = `b-gate` 端口 8010 + 1）。启动后日志应出现 `C 启用 B 回执: ...`。

若只想简单演示、不要重试逻辑，给 **b-gate** 加 `--no-reliability`（C 无需 `--b-callback-url`）：

```bash
python main.py b-gate --host 127.0.0.1 --port 8010 --redis redis://127.0.0.1:6379/0 --queue bishe:overlay:queue --token-secret bishe-dev-secret --no-reliability
```

## 停止链路

向 B-gate 发送（需有效 token，一般由 E 下发给 A/C）：

```bash
curl -X POST http://127.0.0.1:8010/stop -H "Content-Type: application/json" -d "{\"role\":\"a\",\"token\":\"<token>\"}"
```

## 端口一览

| 服务 | 默认端口 |
|------|----------|
| A | 8000 |
| C | 8002 |
| E | 8009 |
| B-gate | 8010 |
| B-gate 控制面（recv-ack） | 8011 |
| Redis | 6379 |

环境变量前缀均为 `BISHE_*`，与 `main.py --help` 一致。
