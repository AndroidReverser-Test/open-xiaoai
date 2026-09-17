# OH2P 直连 OpenCode 免费模型运维手册

本文记录当前 Xiaomi 智能音箱 Pro（OH2P）上的直连部署：音箱不经过 WebSocket Server，优先调用 OpenCode Zen 的免费模型，并在失败时自动切换。客户端还可通过 Exa 网页搜索补充实时资料，回答由音箱内置 TTS 播放。另有一个运行在电脑上的局域网模型管理服务，用户说出“配置模型”后，音箱会拉取最新免费模型目录和调用顺序并写入本地配置。

> [!IMPORTANT]
> 本文不记录局域网地址、SSH 密码或任何密钥。请通过自己的设备清单取得连接信息，并在首次登录后立即更改默认 SSH 密码。

## 当前环境快照

以下信息来自本次部署时的设备检查，存储空间和进程号会随运行状态变化。

| 项目 | 当前状态 |
| --- | --- |
| 设备 | Xiaomi 智能音箱 Pro（OH2P） |
| 固件 | release ROM v1.58.1 |
| 内核 | Linux 4.9.61 |
| 内核架构 | `aarch64` |
| 用户态二进制 | ARMv7，动态链接器为 `/lib/ld-linux-armhf.so.3` |
| 兼容 glibc | 2.25 |
| SSH 服务 | Dropbear，需允许 `ssh-rsa` 主机密钥算法 |
| 数据分区 | `/data`，本次部署后约有 93 MB 可用空间 |
| 自定义唤醒词服务 | 已存在于 `/data/open-xiaoai/kws` |
| 原启动脚本 | `/data/init.sh`，保留其 KWS 逻辑并附加直连客户端启动块 |
| 直连二进制 | `/data/open-xiaoai/deepseek` |
| 模型管理服务 | 电脑局域网 HTTP 服务，默认端口 `4399` |
| 音箱模型配置 | `/data/open-xiaoai/opencode-models.json` |
| 模型服务地址 | `/data/open-xiaoai/model-server-url` |
| 旧 WebSocket Client | `/data/open-xiaoai/client` 可保留作回退，但当前不运行 |

当前启动脚本的备份可通过以下命令查看：

```sh
ls -l /data/init.sh.before-*
```

本次部署中，`init.sh.before-open-xiaoai-20260807` 是未添加 Open-XiaoAI 直连逻辑前的原始 KWS 启动脚本。其他备份是部署过程中的中间版本。

## 连接音箱

本次部署通过 Windows PowerShell 自带的 OpenSSH 连接音箱。电脑和音箱必须位于同一局域网，且音箱的 SSH 服务端口为 `22`。

先检查 SSH 端口是否可达：

```powershell
Test-NetConnection -ComputerName <speaker-ip> -Port 22
```

输出中的 `TcpTestSucceeded` 为 `True` 后，以 `root` 登录。OH2P 上的 Dropbear 只提供 `ssh-rsa` 主机密钥，因此必须显式启用该算法：

```powershell
ssh -o HostKeyAlgorithms=+ssh-rsa root@<speaker-ip>
```

首次连接时，先核对主机密钥指纹再接受；出现密码提示时输入该设备当前的 root SSH 密码。不要把密码写入命令行、脚本或仓库。初始固件的 SSH 说明见[刷机与 SSH 教程](flash.md#ssh-连接到小爱音箱)。

认证成功后，可通过同一选项执行单条远程命令，例如检查用户态架构和数据分区空间：

```powershell
ssh -o HostKeyAlgorithms=+ssh-rsa root@<speaker-ip> "uname -m; df -k /data"
```

## 运行架构

直连模式的二进制源文件是 [`packages/client-rust/src/bin/deepseek.rs`](../packages/client-rust/src/bin/deepseek.rs)。运行链路如下：

1. 小爱系统将最终语音识别结果写入 `/tmp/mico_aivs_lab/instruction.log`。
2. `deepseek` 监听该文件，只处理 `SpeechRecognizer/RecognizeResult` 的最终结果；同一 `dialog_id` 的重复最终结果只处理一次。
3. 客户端重启 `mico_aivs_lab`，中断原小爱的默认回复。
4. 客户端使用内置 Rustls TLS 实现向 OpenCode Zen Chat Completions 接口发起 HTTPS 请求，不使用音箱内置的旧版 `curl`。
5. 客户端先让本地模型配置中的 `planner_model` 判断当前问题是否需要实时网页资料；用户明确说“搜索”“搜一下”“查一下”“联网”或“网上”时会强制请求搜索工具。
6. 若模型请求搜索，客户端只允许一次固定的 `web_search` 函数调用，验证不超过 200 字的查询后，向 OpenCode CLI 同样使用的 Exa MCP 搜索端点发起 HTTPS 请求。
7. Exa 结果限制为三个、6,000 字符上下文和 256 KiB 响应；客户端不跟随重定向、不访问模型或网页返回的任意 URL，并将结果作为 `tool` 消息回传模型。
8. 搜索规划或检索失败不会中断普通回答；若已确定需要实时资料，模型会说明无法确认实时网页信息。
9. 最终请求按照 `/data/open-xiaoai/opencode-models.json` 中的 `order` 顺序尝试，并携带系统提示词、当前识别文本、最近三轮完整对话及可用的搜索结果，复现 OpenCode CLI 的免费模型请求封装。
10. 最终回答的每个候选模型只请求一次；遇到超时、连接或 TLS 失败、限流、5xx 响应、损坏 SSE 流、空响应或其他非认证错误时，立即切换到下一免费模型，不会重试同一候选请求。
11. 候选链由模型管理服务发现并保存；首次没有本地配置时使用当前 CLI 可见的零价模型回退顺序：`big-pickle`、`hy3-free`、`ling-3.0-flash-fin-free`、`mimo-v2.5-free`、`muse-spark-1.2-contributor-free`、`nemotron-3-ultra-free`、`nemotron-3.5-lightning-free`。包括搜索步骤在内的整轮请求共享 180 秒总等待上限。静态 key 遇到明确认证错误时会切换到 OpenCode CLI 的 `public` 凭证并将原请求重试一次；如果当前已经是 `public`，则不重复认证并继续尝试后续模型。网关将“不支持模型”包装成 401/403 时也会继续回退。
12. 客户端从 SSE 事件中的 `choices[0].delta.content` 拼接最终回答，忽略独立的思考内容。调用 `/usr/sbin/tts_play.sh` 前，会将英文双引号、反斜杠、换行和其他控制字符规范化为该脚本可接受的文本，并按音箱固件的安全字节上限优先在句末分段后逐段播放。
13. 日志监听和问答任务彼此独立，监听器只解析最终识别结果并立即送入单一活动任务管理器。收到“闭嘴”“别说了”“停止说话”等明确停止命令时，客户端会取消当前搜索或模型请求、清空待处理问题、终止 `miplayer` 并重启 `mico_aivs_lab` 阻止原小爱接管；停止命令本身不会播放确认语音。普通新问题也会抢占尚未完成的旧问题，避免多路回答重叠。

直连问答链路不依赖以下组件；模型管理服务只在执行“配置模型”时被音箱访问：

- 本机或 NAS 上的 WebSocket 业务服务
- `server.txt`
- WebSocket `ws://<host>:4399`

## 模型管理服务实现

模型管理服务源码是 [`tools/opencode-model-server/server.py`](../tools/opencode-model-server/server.py)。它只依赖 Python 标准库，使用 `ThreadingHTTPServer` 提供局域网 HTTP 服务，默认监听 `0.0.0.0:4399`。网页、CSS 和 JavaScript 直接内嵌在 Python 文件中，不需要安装 Web 框架、Node.js 或前端依赖。

### 上游数据源

服务同时请求两个数据源：

| 数据源 | 用途 | 当前超时 |
| --- | --- | --- |
| `https://opencode.ai/zen/v1/models` | 确认 Zen 当前实际公布的模型 ID | 60 秒 |
| `https://models.dev/api.json` 的 `opencode.models` | 获取价格、名称、状态、上下文、输出上限、模态、推理和工具调用能力 | 60 秒 |

两个请求通过 `ThreadPoolExecutor(max_workers=2)` 并行执行。请求使用 `Accept: application/json` 和 `User-Agent: open-xiaoai-opencode-model-server/1.0`。

### 免费模型判定

一个模型进入音箱候选列表时必须满足以下条件：

1. 模型 ID 当前存在于 Zen `/models` 响应中。
2. Models.dev 中该模型的 `cost.input` 和 `cost.output` 都是数值 `0`。
3. 如果 Models.dev 整体暂时不可用，服务只能退化为接受 Zen 中 ID 以 `-free` 结尾的模型；此时名称、生命周期和能力字段可能不完整。

服务不会仅凭 Models.dev 的零价格配置加入一个已经不在 Zen 实时目录中的模型。这样可以避免把仅存在于元数据、但网关已不再公布的旧模型同步到音箱。

筛选后先保留 `status` 不是 `deprecated` 的模型，再附加仍被 Zen 公布但 Models.dev 标记为 `deprecated` 的兼容模型。每一组内部保持 Zen `/models` 的返回顺序。Models.dev 没有 `status` 字段时按 `active` 处理。

### 当前发现快照

2026-08-29 的实际联调中，服务发现了 9 个输入和输出价格均为零、且仍在 Zen 实时目录中的模型。此表是发现结果，不代表用户后来保存的自定义调用顺序；实时顺序以 `GET /api/models` 的 `order` 为准。

| 模型 ID | 状态 | 上下文 | 最大输出 | 输入模态 | 推理 | 工具调用 |
| --- | --- | ---: | ---: | --- | --- | --- |
| `big-pickle` | active | 200,000 | 32,000 | text | 是 | 是 |
| `muse-spark-1.2-contributor-free` | active | 1,048,576 | 131,072 | text、image、video、pdf、audio | 是 | 是 |
| `mimo-v2.5-free` | active | 200,000 | 32,000 | text、image、audio、video | 是 | 是 |
| `hy3-free` | active | 190,000 | 64,000 | text | 是 | 是 |
| `ling-3.0-flash-fin-free` | active | 262,144 | 32,768 | text | 是 | 是 |
| `nemotron-3-ultra-free` | active | 1,000,000 | 128,000 | text | 是 | 是 |
| `nemotron-3.5-lightning-free` | active | 262,144 | 262,144 | text | 是 | 是 |
| `deepseek-v4-flash-free` | deprecated，Zen 仍公布 | 200,000 | 128,000 | text | 是 | 是 |
| `laguna-s-2.1-free` | deprecated，Zen 仍公布 | 256,000 | 32,000 | text | 是 | 是 |

所有表中模型的 `cost.input` 和 `cost.output` 都是 `0`。deprecated 模型不会被静默删除，网页会明确标记，并默认排在 active 模型之后；用户仍可在本机网页中调整它们的位置。

### 刷新、缓存和降级

服务同时使用三种刷新入口：

1. 启动后后台线程每 300 秒调用一次刷新。
2. 读取 `/api/models` 或 `/api/speaker/config` 时，如果缓存不存在或 `fetched_at` 已超过 300 秒，会同步刷新。
3. 用户访问 `/api/refresh` 时立即刷新。

`ModelStore` 使用 `threading.RLock` 保护内存状态，并用 `refreshing` 防止同一进程重复执行上游刷新。若已有刷新正在进行，其他请求直接读取当前状态。

如果刷新没有发现任何模型，但磁盘缓存里已有模型，服务不会清空候选链，而是返回旧缓存并设置：

```json
{
  "stale": true,
  "errors": {
    "zen": "上游错误信息",
    "models_dev": "上游错误信息"
  }
}
```

只有首次启动且没有任何可用缓存时，模型接口才返回 HTTP `503`。手工 `/api/refresh` 的未处理上游错误返回 HTTP `502`。

### 顺序合并规则

状态中的 `custom_order` 决定刷新后的顺序：

| `custom_order` | 刷新行为 |
| --- | --- |
| `false` 或不存在 | 使用本轮发现顺序，即 active 在前、deprecated 在后 |
| `true` | 保留用户原顺序中仍存在的模型，删除已经消失的模型，再把新发现模型追加到末尾 |

保存新顺序时要求：

- `order` 是 1 到 64 个字符串。
- 不允许重复模型 ID。
- 不允许未知模型 ID。
- 必须恰好包含当前发现的全部模型，网页当前只负责排序，不负责禁用模型。
- 保存成功后设置 `custom_order: true`。

搜索规划模型 `planner_model` 取有序列表中第一个 `tool_call: true` 的模型；如果没有模型声明工具调用能力，则退回列表第一个模型。

### 本机状态文件

默认状态文件是 `tools/opencode-model-server/opencode-models.json`，已加入 `.gitignore`。可用 `OPENCODE_MODEL_STATE` 改到其他路径。写入时先生成同目录的 `.opencode-models.json.tmp`，完成 JSON 写入后使用 `os.replace` 原子替换，避免进程中断留下半个 JSON 文件。

文件结构如下，`models[].config` 保存对应 Models.dev 原始模型配置：

```json
{
  "version": 1,
  "fetched_at": "2026-08-29T09:50:31+00:00",
  "stale": false,
  "errors": {},
  "custom_order": true,
  "order": [
    "big-pickle",
    "muse-spark-1.2-contributor-free"
  ],
  "models": [
    {
      "id": "big-pickle",
      "name": "Big Pickle",
      "status": "active",
      "free": true,
      "provider": "opencode",
      "available_in_zen_catalog": true,
      "reasoning": true,
      "tool_call": true,
      "cost": { "input": 0, "output": 0 },
      "limit": { "context": 200000, "input": 160000, "output": 32000 },
      "modalities": { "input": ["text"], "output": ["text"] },
      "config": { "id": "big-pickle", "name": "Big Pickle" }
    }
  ]
}
```

上例为字段结构的简化示例，实际状态文件的 `order` 必须包含当前全部发现模型，`models` 也会包含完整发现结果。

### HTTP 接口

| 方法和路径 | 调用方 | 行为 |
| --- | --- | --- |
| `GET /` | 电脑浏览器 | 返回拖动排序网页 |
| `GET /static/style.css` | 浏览器 | 返回内嵌样式 |
| `GET /static/app.js` | 浏览器 | 返回内嵌排序逻辑 |
| `GET /api/models` | 浏览器或运维工具 | 返回完整模型配置、顺序、数据源、错误和 stale 状态；缓存过期时刷新 |
| `GET /api/models/<model-id>` | 运维工具 | 返回一个模型及其完整 `config` |
| `GET /api/refresh` | 浏览器或运维工具 | 强制刷新两个上游数据源 |
| `PUT /api/order` | 仅服务所在电脑 | 校验并保存完整调用顺序 |
| `GET /api/speaker/config` | 音箱 | 返回音箱同步用精简配置 |
| `GET /api/config` | 音箱或运维工具 | `/api/speaker/config` 的别名 |
| `GET /healthz` | 健康检查 | 返回服务存活状态，不访问上游 |

`PUT /api/order` 只接受运行服务的电脑自身发起的请求：可以通过 `127.0.0.1`、`::1` 或该电脑自己的网卡地址访问，局域网其他设备会收到 HTTP `403`。因此排序网页必须在运行服务的电脑上打开；推荐使用 `http://127.0.0.1:4399/`，使用本机局域网地址也可以保存。所有 GET 接口不要求认证，只应在可信局域网中开放。

完整模型响应的主要结构：

```json
{
  "ok": true,
  "version": 1,
  "fetched_at": "2026-08-29T09:50:31+00:00",
  "stale": false,
  "errors": {},
  "source": {
    "zen_models_url": "https://opencode.ai/zen/v1/models",
    "models_dev_url": "https://models.dev/api.json"
  },
  "order": ["big-pickle", "mimo-v2.5-free"],
  "planner_model": "big-pickle",
  "models": []
}
```

保存顺序的请求示例：

```powershell
$body = @{
  order = @(
    "big-pickle",
    "mimo-v2.5-free"
  )
} | ConvertTo-Json

Invoke-RestMethod -Method Put `
  -Uri http://127.0.0.1:4399/api/order `
  -ContentType application/json `
  -Body $body
```

实际请求必须包含 `/api/models` 当前列出的全部模型；上例为结构说明，不是可直接提交的完整当前列表。

音箱接口响应结构：

```json
{
  "version": 1,
  "generated_at": "2026-08-29T09:50:31+00:00",
  "fetched_at": "2026-08-29T09:50:31+00:00",
  "stale": false,
  "api_url": "https://opencode.ai/zen/v1/chat/completions",
  "order": ["big-pickle", "mimo-v2.5-free"],
  "planner_model": "big-pickle",
  "models": [
    {
      "id": "big-pickle",
      "name": "Big Pickle",
      "status": "active",
      "tool_call": true
    }
  ]
}
```

当前音箱客户端只反序列化 `order` 和 `planner_model`。响应中的 `api_url`、`models`、时间和 stale 状态用于运维观察，Rust 的 Serde 会忽略这些额外字段；Zen Chat Completions 地址仍是客户端编译时常量。

## 音箱模型同步实现

### 配置文件

音箱使用两个持久化文件：

| 文件 | 内容 |
| --- | --- |
| `/data/open-xiaoai/model-server-url` | 单行 HTTP 服务根地址，例如 `http://<model-server-ip>:4399` |
| `/data/open-xiaoai/opencode-models.json` | 已同步的模型调用顺序和搜索规划模型 |

音箱本地模型文件只保存运行所需的最小字段：

```json
{
  "order": [
    "big-pickle",
    "mimo-v2.5-free",
    "deepseek-v4-flash-free"
  ],
  "planner_model": "big-pickle"
}
```

客户端在每次普通语音问答开始时重新读取该文件，因此网页保存顺序并再次说“配置模型”后，下一轮问题立即使用新顺序，不需要重启 `deepseek`。

如果文件不存在、无法解析、没有模型或包含无效 ID，客户端记录错误并使用编译时内置回退链：

```text
big-pickle
hy3-free
ling-3.0-flash-fin-free
mimo-v2.5-free
muse-spark-1.2-contributor-free
nemotron-3-ultra-free
nemotron-3.5-lightning-free
```

模型 ID 必须非空、不超过 120 字节、不能包含空白或控制字符。读取本地文件时重复 ID 会被去重，模型数量上限为 64；`planner_model` 不在 `order` 中时自动改为第一个模型。

### “配置模型”命令

命令判定不是精确相等，而是识别文本包含“配置模型”。例如“配置模型”和“请帮我配置模型”都会触发。处理流程如下：

1. 按普通请求一样使用 `dialog_id` 去重，并重启 `mico_aivs_lab` 打断原小爱回复。
2. 读取 `/data/open-xiaoai/model-server-url`，去掉末尾 `/` 后拼接 `/api/speaker/config`。
3. 使用 Rustls HTTP/1.1 客户端发起 GET；连接超时 10 秒，请求等待上限 90 秒，不跟随重定向。
4. 如果已保存地址不可用，从音箱当前局域网 IPv4（无法取得时使用旧服务地址）推导同一 `/24` 网段，以最多 24 个并发请求探测端口 `4399` 的 `/healthz`。单个探测连接上限 350 毫秒、总请求上限 750 毫秒，且只接受 `service: "opencode-model-server"` 的响应。
5. 发现服务后重新读取 `/api/speaker/config`，并通过 `.new` 文件加 `rename` 原子更新 `/data/open-xiaoai/model-server-url`，以后优先使用新地址。
6. 要求配置接口返回 HTTP 2xx，反序列化 `order` 和 `planner_model`，再执行模型 ID、数量和规划模型校验。
7. 将精简配置格式化写入 `/data/open-xiaoai/opencode-models.json.new`。
8. 使用 `tokio::fs::rename` 原子替换 `/data/open-xiaoai/opencode-models.json`。
9. 成功时播报模型数量和第一优先模型；失败时保留旧模型文件并播报“模型配置服务暂时不可用，已保留原来的模型调用顺序”。

同步失败不会删除旧配置，也不会阻断之后使用旧顺序继续问答。

### 动态模型调用

最终回答请求不再遍历编译时固定数组，而是遍历本轮读取到的 `ModelSettings.order`。每个 ID最多调用一次；非认证失败进入下一个模型。静态 key 的明确 `AuthError` 会先切换到 `public` 并重试当前请求一次；`public` 认证失败不会再次重试凭证，而是继续遍历候选模型。Zen 返回的 `ModelError`（即使 HTTP 状态为 401/403）也会继续回退，用户配置的 key 不会被覆盖。

搜索规划同样使用本地 `planner_model`，不再固定为 DeepSeek。服务端优先选择支持工具调用的模型，音箱端则确保规划模型属于当前 `order`。

## 播报抢占与停止指令

直连客户端使用单一活动问答任务，文件监听回调不再等待网页搜索、模型请求或 TTS 完成。因此播放期间写入 `instruction.log` 的新最终识别结果可以立即参与调度，不会排在旧回答后面等待。

当前支持以下明确停止说法及其标点、空格变体：

- “闭嘴”“请闭嘴”“你闭嘴”“住口”
- “别说了”“别再说了”“不要说了”“不用说了”“别说话了”
- “停止说话”“停止播报”“停止播放”“停止回答”
- “停一下”“停下来”“快停下”“快停下来”
- “安静”“安静点”“安静一点”
- “别念了”“不要念了”“别讲了”“不要讲了”
- “好了别说了”“行了别说了”

命令使用有限的短句精确匹配，不使用简单的包含判断，因此“闭嘴是什么意思”或“不要停止说话”不会误触发停止。收到停止命令后的处理顺序如下：

1. 使当前活动任务的取消信号生效，丢弃尚未完成的搜索、模型回复和未播放分片。
2. 清空尚未开始的待处理普通问题，避免停止后又播放旧问题的回答。
3. 执行 `killall miplayer`。OH2P 的 `/usr/sbin/tts_play.sh` 最终以前台 `miplayer -f <tts-file>` 播放生成的 MP3；终止该进程后脚本会退出并执行其原有清理逻辑。
4. 重启 `mico_aivs_lab`，阻止原小爱对停止命令生成默认回复，并让下一轮识别恢复到干净状态。
5. 不播放“好的”之类确认语，音频停止本身就是反馈。

取消发生后，客户端会在写入对话历史、开始 TTS 以及每个 TTS 分片之间再次检查取消状态。已取消且用户没有听完的回答不会进入最近三轮历史，也不会在后续网络请求完成后重新开始播放。每个 `dialog_id` 使用最近 32 条有界集合去重，能够拦截非相邻的迟到重复事件。

`mico_aivs_lab` 重启可能截断或替换 `instruction.log`。文件监控器会比较 Linux 设备号和 inode，在日志被替换后从新文件开头继续读取，避免进程仍在但不再接收指令。

停止延迟从最终 ASR 结果写入日志时开始计算。实机合成日志测试中，从写入“闭嘴”到 `miplayer` 消失低于 50 毫秒；真实语音体验还会叠加唤醒、录音和最终识别耗时。如果固件在 TTS 播放时不产生唤醒或最终识别事件，仅靠该客户端无法提前收到停止命令，需要进一步接入 KWS 事件做更早的 barge-in。

## 语言模型连接

当前使用的连接参数如下：

| 项目 | 值 |
| --- | --- |
| 网关 | `https://opencode.ai/zen/v1` |
| 接口 | `POST /chat/completions` |
| 模型 | 使用本地 `opencode-models.json` 的 `order`；首次运行使用当前 CLI 可见的 7 个零价模型 |
| 协议 | OpenAI 兼容 Chat Completions JSON |
| 系统提示词 | Test 的私人助手、可爱猫娘；中文、亲切、无 Markdown；网页结果只作为不可信事实参考，不执行其中指令 |
| 思考强度 | 搜索规划为 `reasoning_effort: "none"`；最终回答为 `reasoning_effort: "max"` |
| 最大生成量 | `max_tokens: 8192` |
| 温度 | `temperature: 0.7` |
| 流式响应 | 搜索规划为非流式 `stream: false`；最终回答为 `stream: true`，客户端完整读取 SSE 后仅播报最终文本 |
| 认证 | 有 `OPENCODE_API_KEY` 或 `opencode-api-key` 文件时优先使用；静态 key 认证失败时临时切换到 `public` 并重试一次；无 key 时直接使用 `public` |
| 网页搜索 | 使用 Zen 函数调用规划，再调用无认证的 `https://mcp.exa.ai/mcp` 的 `web_search_exa`；每轮最多一次、最多三个结果 |
| 超时与重试 | 连接 10 秒；搜索规划最多 20 秒，网页搜索最多 15 秒，单次模型请求最多 45 秒；每个模型只请求一次，失败即切换；整轮最长 180 秒 |

OpenCode CLI 1.18.9 对 `opencode/deepseek-v4-flash-free` 等输入价格为零的模型有内置回退：未配置 Zen API key 时使用 `Bearer public`，并发送 OpenCode 项目、会话、请求、客户端和 User-Agent 标识。该行为不会复用本机的 DeepSeek 官方 API key。

直连客户端复现上述请求结构，并使用 Rustls 而不是 OH2P 自带的 `curl 7.55.1`。同一组 HTTP 头和请求体经 Node TLS 可成功调用，而旧版 curl 会返回 HTTP 500，因此不能将 curl 临时请求的失败误判为模型不可用。

> [!CAUTION]
> 这是当前 OpenCode CLI 免费模型的实现行为，不是稳定的外部 API 承诺。OpenCode 可能随时调整模型、请求格式或可用性；如需稳定生产使用，应配置自己的 Zen API key。

如已拥有 Zen API key，客户端按以下优先级读取它：

1. 进程环境变量 `OPENCODE_API_KEY`。
2. 音箱上的 `/data/open-xiaoai/opencode-api-key` 文件。

不要将自己的 API key 写入仓库、命令历史、`init.sh` 或公开日志。没有该 key 时，当前实现仍会使用免费模型回退。

## 网页搜索

Zen Chat Completions 没有可直接开启的原生 `web_search` 工具类型。客户端通过 OpenAI 兼容的函数调用协议自行执行搜索，流程如下：

1. 用本地模型配置中的 `planner_model` 发起一次非流式搜索规划请求，仅暴露固定的 `web_search` 函数。
2. 用户明确说“搜索”“搜一下”“查一下”“联网”或“网上”时强制该函数调用；其余问题由规划模型判断是否需要实时资料。
3. 客户端只接受该函数，清理并限制搜索词后调用固定的 `https://mcp.exa.ai/mcp` 端点及 `web_search_exa` 工具。
4. 客户端将最多三个搜索结果作为 `role: "tool"` 消息附加到本轮上下文，再由免费模型候选链生成最终回答。最终请求不再暴露工具，因此不会产生多轮搜索。

该实现不需要 `OPENCODE_ENABLE_EXA`、Exa API key 或其他配置文件。搜索规划最多等待 20 秒，Exa 请求最多等待 15 秒；任一步失败都会回退到普通回答，不会阻断音箱回复。若检索已经被规划但失败，回答会明确说明无法确认实时网页信息。

搜索结果是未经验证的第三方内容。客户端不会访问结果中出现的链接，不允许模型选择 URL 或工具，并限制单次结果上下文为 6,000 字符、网络响应为 256 KiB。

## 构建与部署

### 启动电脑上的模型管理服务

在电脑上启动服务，服务会主动合并 OpenCode Zen 实时目录和 Models.dev 模型配置。首次访问网页或音箱同步时会获取当前目录，之后默认每 5 分钟刷新一次。

```powershell
python tools/opencode-model-server/server.py
```

需要后台隐藏运行时使用仓库脚本：

```powershell
powershell -ExecutionPolicy Bypass -File tools/opencode-model-server/start.ps1
powershell -ExecutionPolicy Bypass -File tools/opencode-model-server/stop.ps1
```

启动脚本从 PATH 查找 `python`，将标准输出和错误输出写入 `%LOCALAPPDATA%\open-xiaoai\`，并在端口 `4399` 已有监听者时不重复启动。停止脚本只停止命令行包含 `opencode-model-server/server.py` 的 Python 进程。

网页管理地址：`http://127.0.0.1:4399/`。拖动模型卡片后点击“保存顺序”，配置会原子写入 `tools/opencode-model-server/opencode-models.json`。音箱读取的精简接口是：

```text
http://<电脑局域网IP>:4399/api/speaker/config
```

如果 Windows 防火墙拦截局域网访问，请允许 Python 在“专用网络”通信；不要将该端口暴露到公网。电脑局域网 IP 变化后，说“配置模型”会触发同网段自动发现并更新音箱上的 `model-server-url` 文件；跨网段或非 `/24` 网络仍需手工更新。模型目录刷新可能需要几十秒，单次配置接口请求等待上限为 90 秒。

Windows 防火墙规则需要管理员 PowerShell 执行。规则只开放 TCP `4399`，服务本身仍只允许本机保存顺序：

```powershell
New-NetFirewallRule `
  -DisplayName "Open-XiaoAI OpenCode Model Server" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 4399 `
  -Profile Private,Public
```

如果不需要音箱直连而只在电脑本机使用网页，可以不添加该规则，并将服务绑定到 `127.0.0.1`：

```powershell
$env:OPENCODE_MODEL_SERVER_HOST = "127.0.0.1"
python tools/opencode-model-server/server.py
```

### 交叉编译

音箱虽然报告为 `aarch64` 内核，用户态需要兼容 ARMv7 和 glibc 2.25。请使用项目提供的运行环境构建：

```powershell
Set-Location packages/client-rust

docker run --rm -e CC_armv7_unknown_linux_gnueabihf=arm-linux-gnueabihf-gcc `
  -v "${PWD}:/app" idootop/open-xiaoai-runtime:oh2p `
  cargo build --bin deepseek --target armv7-unknown-linux-gnueabihf --release --locked
```

输出文件为：

```text
target/armv7-unknown-linux-gnueabihf/release/deepseek
```

### 上传、校验与原子替换

Dropbear 不一定支持 SFTP，因此 `scp` 使用旧协议 `-O`。将 `<speaker-ip>` 替换为音箱地址，先上传到 `.new` 文件：

```powershell
scp -O -o HostKeyAlgorithms=+ssh-rsa `
  target/armv7-unknown-linux-gnueabihf/release/deepseek `
  root@<speaker-ip>:/data/open-xiaoai/deepseek.new
```

上传后先比较 SHA-256；本地与音箱输出相同（忽略大小写）后，才替换正在使用的二进制：

```powershell
(Get-FileHash -Algorithm SHA256 target/armv7-unknown-linux-gnueabihf/release/deepseek).Hash
ssh -o HostKeyAlgorithms=+ssh-rsa root@<speaker-ip> `
  'sha256sum /data/open-xiaoai/deepseek.new'

ssh -o HostKeyAlgorithms=+ssh-rsa root@<speaker-ip> `
  'chmod +x /data/open-xiaoai/deepseek.new && mv /data/open-xiaoai/deepseek.new /data/open-xiaoai/deepseek'
```

同时将模型管理服务地址写入音箱。将 `<model-server-ip>` 替换为运行模型服务的电脑局域网地址：

```powershell
ssh -o HostKeyAlgorithms=+ssh-rsa root@<speaker-ip> `
  'printf "%s\\n" "http://<model-server-ip>:4399" > /data/open-xiaoai/model-server-url'
```

本地模型顺序会在第一次说“配置模型”时从服务端同步，无需手工创建 `opencode-models.json`。

也可以使用仓库内的 `tools/opencode-model-server/deploy_speaker.py` 自动完成上传、SHA-256 校验、原子替换、服务地址写入和 `deepseek` 重启：

```powershell
python -m pip install paramiko
$env:SPEAKER_PASSWORD = "音箱当前 SSH 密码"
python tools/opencode-model-server/deploy_speaker.py `
  --host <speaker-ip> `
  --binary packages/client-rust/target/armv7-unknown-linux-gnueabihf/release/deepseek `
  --server-url http://<model-server-ip>:4399
Remove-Item Env:SPEAKER_PASSWORD
```

该脚本的具体步骤是：

1. 读取 `SPEAKER_PASSWORD`，不把密码写入仓库文件。
2. 通过 SSH 创建 `/data/open-xiaoai`，上传到 `/data/open-xiaoai/deepseek.new`。
3. 比较本地和远端 `sha256sum`，不一致时停止，不替换现有程序。
4. 设置执行权限后将 `.new` 文件移动为 `/data/open-xiaoai/deepseek`。
5. 把服务地址先写到 `model-server-url.new`，再原子移动为 `model-server-url`。
6. 使用 BusyBox `start-stop-daemon` 停止旧 PID 并启动新客户端。
7. 输出音箱架构、服务地址、PID 和进程信息。

部署脚本当前使用 Paramiko 的 `AutoAddPolicy` 接受首次 SSH 主机密钥，适合可信局域网的临时部署；正式环境应先人工核对音箱主机指纹，或改用手工 `scp -O`/`ssh` 流程。

替换后按下文的“当前会话中可手动启动或重启服务”执行重启命令，使新二进制生效。

### 本次实际部署结果

本次联调使用项目提供的 `idootop/open-xiaoai-runtime:oh2p` 镜像完成 ARMv7 交叉编译。最终产物信息如下：

| 项目 | 结果 |
| --- | --- |
| 目标 | `armv7-unknown-linux-gnueabihf` |
| 文件 | `target/armv7-unknown-linux-gnueabihf/release/deepseek` |
| 文件大小 | 1,952,968 字节 |
| SHA-256 | `dcb608e93a7fbadba1b7db5ba86e01d002ffe49a8ce377a4876f2d0051aca5c4` |
| ELF | 32-bit ARM EABI5，动态链接器 `/lib/ld-linux-armhf.so.3` |
| 音箱文件 | `/data/open-xiaoai/deepseek` |
| 音箱进程 | `/data/open-xiaoai/deepseek`，PID 写入 `/tmp/open-xiaoai-deepseek.pid` |

本地文件与音箱文件 SHA-256 一致。音箱上的 `model-server-url` 已配置为电脑局域网模型服务地址，`opencode-models.json` 已成功写入同步结果。进程和服务重启后均保持运行。

### 开机启动

当前设备的 `/data/init.sh` 已保留原 KWS 服务，并在网络等待后加入以下启动块。不要直接覆盖已有的 KWS 启动脚本；应在其保留原逻辑的前提下添加该块。

```sh
# Start the OpenCode Zen voice client without blocking the existing KWS service.
if [ -x /data/open-xiaoai/deepseek ]; then
    /sbin/start-stop-daemon -K -q -p /tmp/open-xiaoai-deepseek.pid || true
    rm -f /tmp/open-xiaoai-deepseek.pid
    /sbin/start-stop-daemon -S -b -m -p /tmp/open-xiaoai-deepseek.pid \
        -x /data/open-xiaoai/deepseek
fi
```

`start-stop-daemon` 由 BusyBox 提供。它用于脱离 SSH 会话后台运行客户端，并将 PID 写入 `/tmp/open-xiaoai-deepseek.pid`。

当前会话中可手动启动或重启服务：

```sh
/sbin/start-stop-daemon -K -q -p /tmp/open-xiaoai-deepseek.pid || true
rm -f /tmp/open-xiaoai-deepseek.pid
/sbin/start-stop-daemon -S -b -m -p /tmp/open-xiaoai-deepseek.pid \
  -x /data/open-xiaoai/deepseek
```

## 日常使用

1. 确保音箱已联网，KWS 和 `deepseek` 进程均已启动。
2. 使用原小爱唤醒方式或已经配置的自定义唤醒词开始说话。
3. 说“配置模型”，音箱会访问电脑上的 `/api/speaker/config`，更新 `/data/open-xiaoai/opencode-models.json`，并播报同步结果。
4. 正常说出问题即可，不需要以“请”或“你”开头。说“搜索今天北京天气”或“联网查一下 OpenCode 最新消息”可明确请求网页搜索；其他需要实时信息的问题由模型自动判断。
5. 音箱会停止原小爱的回复，等待搜索和模型完成，再用系统 TTS 播报结果。
6. 思考或播报过程中说“闭嘴”“别说了”“停止说话”等明确停止命令，客户端会取消当前问答并立即停止 TTS，不会再播报确认语。直接说出新的普通问题也会取消尚未完成的旧问题，并在清理完成后处理新问题。

直连客户端仅在进程内保存最近三轮完整对话（用户识别文本及对应模型回复）。下一轮请求会携带这些历史，因此模型可以理解前三句用户语音的上下文。历史不会写入磁盘，音箱重启、客户端重启、空闲超过 5 分钟后都会清除；说“清空对话”或“结束对话”也会立即清除。

## 验证记录

本次实现已完成以下验证：

| 检查 | 结果 |
| --- | --- |
| 服务 `/healthz` | HTTP 200，返回 `{"ok":true,"service":"opencode-model-server"}` |
| 服务发现 | 成功合并 9 个免费模型，`stale: false` |
| 模型详情 | `GET /api/models/big-pickle` 返回价格、上下文和完整 `config` |
| 顺序持久化 | 本机通过回环地址或本机局域网地址调用 `PUT /api/order` 均可保存，读取结果一致 |
| 远程写保护 | 局域网来源 `PUT /api/order` 返回 HTTP 403 |
| 音箱健康访问 | 音箱可通过配置的局域网地址访问 `/healthz` 和 `/api/speaker/config` |
| 语音同步 | 写入一条最终识别事件“配置模型”后，音箱成功播报更新结果并写入 9 个模型 |
| 服务自动发现 | 将音箱地址改为同网段无效地址后触发“配置模型”，音箱发现当前电脑服务、原子更新地址并同步 9 个模型 |
| 模型故障回退 | 受控测试中 `hy3-free` 的 401 `ModelError`、`mimo-v2.5-free` 的 500 均继续切换，`big-pickle` 最终成功 |
| 认证恢复 | 远端测试使用无效静态 key，触发切换到 `public` 并重试；随后继续模型链并由 `big-pickle` 成功回复 |
| 停止当前 TTS | OH2P 上播放一段 9.72 秒测试语音，执行 `killall miplayer` 后约 1 秒内脚本退出；部署后写入最终识别事件“闭嘴”，首个 50 毫秒轮询前播放器已停止 |
| 取消模型请求 | 模型请求启动并触发 `mico_aivs_lab` 实际重启后写入“闭嘴”，连续 10 秒未出现已取消回答的迟到 TTS，客户端进程保持运行 |
| 停止后普通回复 | 停止测试后写入“清空对话”，约 1.55 秒开始 TTS 并正常播放结束，证明日志重开和普通回复链路正常 |
| Rust 单元测试 | `cargo test --locked` 全部通过，DeepSeek 客户端 14 个测试通过 |
| Python 单元测试 | `python -m unittest tools/opencode-model-server/test_server.py`，4 个测试通过 |
| Python 语法检查 | `py_compile` 通过 |
| PowerShell 语法检查 | `start.ps1`、`stop.ps1` 通过 |
| ARMv7 构建 | 交叉编译成功，产物可识别为 ARMv7/glibc 动态链接程序 |
| 长回复播报回归 | 现场生成长回复并拆为 3 个 TTS 片段，均完成播放，未新增 `mibrain_service` 崩溃 |

验证时使用的语音日志包含识别文本，不应复制到公开渠道。进程号、模型目录、上游状态和 IP 地址都可能变化，排障时应以当前命令输出为准。

## 状态检查与排障

检查直连进程和 PID：

```sh
cat /tmp/open-xiaoai-deepseek.pid
ps | grep 'open-xiaoai/deepseek' | grep -v grep
```

检查语音识别日志是否持续产生最终结果。该日志包含语音转写内容，不要在公开渠道分享：

```sh
tail -f /tmp/mico_aivs_lab/instruction.log
```

OH2P 可能为同一轮语音多次写入同一个 `dialog_id` 的最终识别结果。当前客户端会自动去重；如果旧版客户端对同一轮问题重复请求模型、反复重启语音服务或打断播报，请重新部署当前 `deepseek` 二进制。

检查模型网络可达性但不生成回答：

```sh
curl -sS --connect-timeout 10 -o /dev/null -w '%{http_code}\n' \
  https://opencode.ai/zen/v1/models
```

常见问题：

| 现象 | 检查方向 |
| --- | --- |
| 没有任何回复 | 确认 `deepseek` 进程、`instruction.log` 和 KWS 服务都在运行 |
| 只听到原小爱回复 | 直连进程未捕获最终识别事件，检查 `instruction.log` 格式和进程状态 |
| 提示模型不可用 | 客户端会在每个免费模型失败后立即切换到下一个候选；包括 Zen 将“不支持模型”返回为 401/403 的情况。静态 key 遇到明确 `AuthError` 会先切换到 `public` 重试一次，当前已是 `public` 时继续候选链。确认运行的是包含 Rustls 的新版 `deepseek` 二进制；旧版 curl 实现会被 Zen 返回 HTTP 500。若配置了自己的 key，检查 `OPENCODE_API_KEY` 或 `opencode-api-key` 文件 |
| 没有使用网页搜索 | 明确说“搜索”或“联网查一下”后重试。搜索规划使用本地配置的 `planner_model`；如果该模型或 Zen 暂时不可用，客户端仍会给出普通模型回答 |
| “配置模型”提示同步失败 | 确认电脑模型服务正在运行，并从电脑访问 `/healthz` 和 `/api/speaker/config`；新版音箱客户端会在旧地址失败后自动探测同一 `/24` 网段，若电脑和音箱跨网段、端口不是 `4399` 或防火墙拦截，仍需修正 `/data/open-xiaoai/model-server-url` |
| 同步后没有使用新顺序 | 检查 `/data/open-xiaoai/opencode-models.json` 的 `order` 字段和 `deepseek` 进程是否为当前版本；下一轮语音请求会重新读取该文件 |
| 提示无法确认实时网页信息 | Exa 搜索服务可能超时、限流或临时不可用；客户端会在 15 秒内停止搜索并回退到普通回答 |
| Chat Completions 返回 `Internal server error` | 不要用音箱 curl 临时请求作为结论；该端点会拒绝旧 curl TLS。通过新版客户端的日志和实际语音请求验证 |
| 回复很慢 | `reasoning_effort: "max"` 会显著增加思考时间；单次候选最多等待 45 秒，整个兜底链最长等待 180 秒。检查公网网络质量和 Zen 服务状态 |
| 有模型回复记录但没有播报 | 先升级到当前 `deepseek`。旧版会把模型原文直接拼入原厂 TTS 脚本的 JSON，英文双引号、反斜杠或换行会导致 `Failed to parse message data`；过长文本还可能触发 OH2P 的 TTS 服务崩溃。当前客户端会先清理文本，并按 768 字节上限优先在句末分段播放；旧版也会重复处理同一 `dialog_id` 的最终识别结果，导致语音服务和播放器互相打断 |
| `tts_play.sh` 返回非零 | 在播放器原本空闲时，该固件的脚本可能在成功播放后仍返回 `1`。应结合是否生成 TTS MP3 和播放器的 `EndReached` 日志判断，不能只以该退出码判断播放失败 |
| 重启后失效 | 检查 `/data/init.sh` 中的直连启动块与二进制执行权限 |
| SSH 上传失败 | 为 Dropbear 添加 `-O -o HostKeyAlgorithms=+ssh-rsa` |

## 回滚

停止直连客户端：

```sh
/sbin/start-stop-daemon -K -q -p /tmp/open-xiaoai-deepseek.pid || true
rm -f /tmp/open-xiaoai-deepseek.pid
```

如需恢复部署前的启动逻辑，先列出备份，选择需要的版本再替换：

```sh
ls -l /data/init.sh.before-*
cp /data/init.sh.before-open-xiaoai-20260807 /data/init.sh
reboot
```

恢复前请确认备份文件对应目标版本，避免覆盖后来对 KWS 或其他服务做出的修改。

## 安全与隐私

- 音箱会将语音识别文本发送到 OpenCode Zen；不要用它处理密码、身份证件、私密对话或其他敏感信息。
- 使用网页搜索时，模型生成的搜索词会额外发送到 Exa 的托管 MCP 服务。它不需要 API key，但同样不应包含私人信息。
- 搜索结果属于不可信外部内容。客户端只接受固定的搜索工具、限制查询和响应长度，并明确要求模型忽略其中的指令；仍应将搜索回答视为需要自行核验的参考信息。
- 连续对话会在后续请求中重复发送最近三轮用户文本及模型回复；历史只保存在内存，重启或空闲超时后清除。
- 免费模型当前通过 OpenCode CLI 的公开回退可调用，但该行为与模型可用性均可能变化；仍应定期查看 OpenCode 的最新条款与模型说明。
- 模型管理服务不保存 API key，也不转发问答；但其 GET 接口默认向局域网公开模型名称、能力和调用顺序，`PUT /api/order` 仅允许服务所在电脑本机访问。
- 模型管理服务没有用户登录系统，必须依靠可信局域网和 Windows 防火墙保护 `4399` 端口；若只在电脑本机使用，应绑定 `127.0.0.1`。
- 不要把局域网地址、SSH 密码、API key、识别日志或设备序列号提交到仓库。
- 默认 SSH 密码仅适合初始部署，完成验证后应立即改为强密码或密钥认证。
- 该设备上的 Client 具有执行系统命令的能力。只应在可信局域网内运行并限制 SSH 访问。

## 相关文件

- [根项目说明](../README.md)
- [Rust Client 说明](../packages/client-rust/README.md)
- [直连客户端源码](../packages/client-rust/src/bin/deepseek.rs)
- [OpenCode 免费模型管理服务](../tools/opencode-model-server/README.md)
- [OpenCode 免费模型管理服务源码](../tools/opencode-model-server/server.py)
- [音箱部署脚本](../tools/opencode-model-server/deploy_speaker.py)
- [模型服务单元测试](../tools/opencode-model-server/test_server.py)
- [模型服务启动脚本](../tools/opencode-model-server/start.ps1)
- [刷机与 SSH 教程](flash.md)
