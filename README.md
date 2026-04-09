# 基于“流媒体 API 风格”的应用层隐匿通道 Demo（Python）

链路：**B 像播放器一样走 HLS 形态** — `GET master.m3u8` → `GET index.m3u8` → `GET seg-xxxxxx.ts`（分片为类 TS 二进制 + 业务头）→ **Redis** → **B-worker** → **HTTP/HTTPS** → **C**。

- **A（本地源站）**：**`POST /proxy` 已关闭**；请用 **`POST /overlay/control`** + **`POST /overlay/embed`**（multipart：`video` + `hidden`）将隐匿数据附在视频字节后入队。对外仍提供 **`master.m3u8` → `index.m3u8` → `seg-*.ts`**。
- **B**：
  - `b-pull`：解析 m3u8 文本得到 URI，**按 HLS 习惯拉列表再拉分片**，拼 `OverlayEnvelope` 后写入 Redis。
  - `b-worker`：从 Redis 取出并 **POST** 到 C（`--c-url` 可为 `https://...`，默认校验证书关闭便于自签）。
- **C**：`POST /recv` 接收；可选 **HTTPS**（`--ssl-cert` + `--ssl-key`）。

**注意**：`--session` 在 **A** 与 **b-pull** 上起始值必须一致，格式为 **`bishe-<数字>`**（默认 `bishe-1`）。**每完成一对控制+媒介分片**（媒介 `seg` 从 A 出队）后，A 将当前会话递增为 `bishe-2`、`bishe-3`…，并在分片响应头里带 **`X-Next-Session`**；**b-pull** 会自动跟随，下一对仍按「先 POST control 再 POST embed」即可。

## 依赖

- Python 3.10+（建议 3.11/3.12）
- Redis（本机或容器均可）

```bash
pip install -r requirements.txt
```

```bash
docker run --rm -p 6379:6379 redis:7
```

## 启动四个进程

### 1) 启动 C（HTTP）

```bash
python main.py c --host 127.0.0.1 --port 8002
```

### 2) 启动 B pull（轮询 GET A → Redis）

```bash
python main.py b-pull --a-url http://127.0.0.1:8000 --session bishe-1 --redis redis://127.0.0.1:6379/0 --queue bishe:overlay:queue
```

### 3) 启动 B worker（Redis → C）

```bash
python main.py b-worker --redis redis://127.0.0.1:6379/0 --queue bishe:overlay:queue --c-url http://127.0.0.1:8002
```

### 4) 启动 A（被动出流）

```bash
python main.py a --host 127.0.0.1 --port 8000 --session bishe-1
```

## 发送隐匿数据（视频载体，需两步）

**顺序**：同一 A 进程、同一 `session` 下，**先 control 再 embed**；`b-pull` / `b-worker` / C 保持运行。  
项目根目录已带 **`ctrl.json`**（合法 JSON，避免 PowerShell 吃掉引号）。

### 1）控制帧（任选一种，在 `D:\PyProjects\Bishe` 下执行）

**方式 A — `curl.exe` + 文件（推荐，不依赖 PowerShell 引号规则）：**

```powershell
cd D:\PyProjects\Bishe
curl.exe -X POST "http://127.0.0.1:8000/overlay/control" -H "Content-Type: application/json" --data-binary "@ctrl.json"
```

**方式 B — 纯 PowerShell（用对象转 JSON，不会丢引号）：**

```powershell
$body = @{ c_url = "http://127.0.0.1:8002"; extract = @{ method = "append_marker" } } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/overlay/control" -ContentType "application/json; charset=utf-8" -Body $body
```

### 2）媒体帧（`video` / `hidden` 换成你的真实路径）

```powershell
cd D:\PyProjects\Bishe
curl.exe -X POST "http://127.0.0.1:8000/overlay/embed?c=http://127.0.0.1:8002" -F "video=@cover.mp4" -F "hidden=@hidden.bin"
```

若文件不在当前目录，写绝对路径，例如 `-F "video=@D:\PyProjects\Bishe\cover.mp4"`。

说明：`hidden` 经 **尾部 MAGIC+长度** 附在 `video` 字节之后；B 拉片后由 worker **`extract_trailer`** 拆出再 **POST** 到 C。

## C 使用 HTTPS（示例）

自签证书（示例）：

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"
```

```bash
python main.py c --host 127.0.0.1 --port 8443 --ssl-cert cert.pem --ssl-key key.pem
```

```bash
python main.py b-worker --c-url https://127.0.0.1:8443 ...
```

## 后续扩展（建议）

- 调整 `b-pull` 轮询与分片大小，使 TLS 外可见的时序/体积更接近真实拉片。
- 多段 fMP4、真实 m3u8 索引与 GET 组合。
- Redis Streams、多 worker、重试与幂等。
