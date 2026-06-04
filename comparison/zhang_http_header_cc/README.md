# Zhang et al. [7] HTTP 头字段隐蔽信道（对比基线）

本目录为**独立对比实验**，不修改 `bishe_demo/` 与 `main.py` 等既有实现。思路参考 Zhang R, Gan Y, Yin Y F 一文：在 HTTP 请求的 **User-Agent**、**Cookie** 等头字段中承载编码后的秘密信息；**接收端**通过解析 Web 服务器写入的访问日志（此处为 **JSONL**，一行一条请求）恢复数据。

## 文献

Zhang R, Gan Y, Yin Y F. Research on Construction Methods for Network Covert Channels Based on HTTP[J]. *Advanced Materials Research*, 2012, 220-223: 2528-2533.

## 本实现的协议要点（可写进论文“对比基线”小节）

- **密文**（PSK1 AEAD 输出）再做 **Base64URL**（无 padding），然后按每请求容量切分为多段。
- 每一段再拆成两段子串：**`zh_a` 放在 Cookie**，**`zh_b` 放在 User-Agent 尾部**（与 [7] 中利用多头部字段的思想一致；便于在日志中同时观测两类头）。
- **元数据**（`msg_id`、`seq`、`total`）同时出现在 Cookie 与 UA 尾部，接收端以 UA 尾部正则解析为主，并与 Cookie 中的 `m=<uuid>` 交叉校验。

## 机密性（与主 demo 对齐的 AEAD）

为与主系统数据面一致，**HTTP 头上承载的是密文**，不是明文：

- 复用 `bishe_demo.psk_aead` 的 **PSK1 / AES-256-GCM**（`encrypt_hidden` / `decrypt_hidden`）。
- 发送前：`16 字节随机前缀 || 明文` 作为 `encrypt_hidden` 的 `hidden`，得到 **PSK1 密文**；再对该密文做 Base64URL 后塞进头字段。
- 接收端：从日志拼回密文 → **AEAD 解密** → 去掉前 16 字节得到**明文**。

两端必须使用**同一 32 字节 PSK**（64 位十六进制）及**同一 `--aad` 字符串**（默认 `zhang-http-header-cc/v1`）。PSK 可通过 `--psk-hex`、`--psk-file` 或环境变量 **`BISHE_PSK_HEX`** 提供（与主项目习惯一致）。

## 依赖

与主项目相同：`aiohttp`、`httpx`（见仓库根目录 `requirements.txt`）。

## 与 HLS 并列对比

```powershell
python scripts\compare_hls_vs_zhang.py ^
  --segments "D:\PyProjects\Bishe\*.ts" --segments-limit 6 --k 3 ^
  --hidden-bytes 1024,4096,65536 --hls-repeats 3 --zhang-repeats 3 ^
  --zhang-url http://127.0.0.1:8011/ --zhang-log scripts\zhang_cc.jsonl --psk-hex <64hex>
```

## 用法（在仓库根目录执行）

### 1）启动“写日志的 Web 服务器”（接收端的数据来源）

```powershell
Set-Location d:\PyProjects\Bishe
python comparison\zhang_http_header_cc\server.py --host 127.0.0.1 --port 8011 --log d:\PyProjects\Bishe\scripts\zhang_cc.jsonl
```

### 2）发送端：AEAD 加密后嵌入头字段并发起 GET

```powershell
# 示例 PSK：32 字节 = 64 个十六进制字符（可与主 demo 使用同一把密钥，便于叙述“同一 PSK 下对比承载方式”）
# python -c "import secrets; print(secrets.token_hex(32))"
python comparison\zhang_http_header_cc\sender.py --url http://127.0.0.1:8011/ --text "hello" --psk-hex <64位hex>
# 或
python comparison\zhang_http_header_cc\sender.py --url http://127.0.0.1:8011/ --payload .\hidden.bin --psk-file .\psk_hex.txt
```

终端会打印 `msg_id=<UUID>`，供下一步恢复使用。

### 3）接收端：解析 JSONL 日志、AEAD 解密并还原明文

```powershell
python comparison\zhang_http_header_cc\recv_from_log.py --log d:\PyProjects\Bishe\scripts\zhang_cc.jsonl --msg-id <上一步的UUID> --out recovered.bin --psk-hex <64位hex>
```

### 4）对比压测（吞吐 / 请求数 / 正确率）

需保持 **`--log` 与 server 的 `--log` 一致**（同一文件路径），且 server 已启动：

```powershell
python comparison\zhang_http_header_cc\bench_zhang_header_cc.py --url http://127.0.0.1:8011/ --log d:\PyProjects\Bishe\scripts\zhang_cc.jsonl --hidden-bytes 4096 --repeats 5 --psk-hex <64位hex>
```

输出含 `elapsed_s_*`、`requests_*`、`throughput_avg_Bps`、`ok_rate`，可与现有 HLS 方案 bench 结果并列制表。

压测末尾会额外打印与 **`scripts/bench_overlay_embed_hls_scheme_b.py`** 对齐的字段，便于直接抄表：

- **`expected_unique` / `got_unique` / `loss` / `loss_rate`**：以「每条随机消息是否解密还原成功」为粒度；成功记 1 条 unique，失败记 loss（与 HLS 的 `expected_unique` / `got_unique` 命名一致；语义上 HLS 看 C 的 `/stats`，Zhang 看本地解密校验）。
- **`e2e_ms_p50` / `e2e_ms_p95` / `e2e_ms_p99`**：每轮 **端到端墙钟时间**（发起 GET 序列 → 日志落盘 → 读日志解密校验）的百分位，单位 ms。说明：HLS 的 `e2e_ms_*` 来自 **C 端统计**；Zhang 为 **本机客户端测得的闭环时延**，论文中应一句说明二者定义边界。
- **`throughput_avg_Bps`**：**成功轮次**的 goodput，`sum(hidden_bytes) / sum(该轮 e2e 时间)`。另附 **`throughput_attempt_Bps`**（含失败轮、按全部轮耗时平均的尝试吞吐）供参考。

## 可调参数

- `sender.py` / `bench_zhang_header_cc.py`：`--cookie-chars`、`--ua-chars` 控制每请求在两类头中的 **Base64URL 字符预算**（过大可能触发代理/服务器的头长度限制）。
