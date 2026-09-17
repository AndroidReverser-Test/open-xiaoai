# OpenCode 免费模型管理服务

这是一个只使用 Python 标准库的局域网 HTTP 服务，负责：

- 从 `https://opencode.ai/zen/v1/models` 获取实时模型目录。
- 从 `https://models.dev/api.json` 获取价格、上下文长度、模态、工具调用和生命周期配置。
- 合并出 OpenCode 的零输入、零输出价格模型，并保留音箱当前的调用顺序。
- 通过网页拖动模型顺序，并将顺序原子写入本机 `opencode-models.json`。
- 给小爱音箱提供只读的 `/api/speaker/config` 接口。

## 启动

在仓库根目录运行：

```powershell
python tools/opencode-model-server/server.py
```

运行离线单测：

```powershell
python -m unittest tools/opencode-model-server/test_server.py
```

默认监听 `0.0.0.0:4399`，网页地址为 `http://127.0.0.1:4399/`。可通过环境变量修改：

```powershell
$env:OPENCODE_MODEL_SERVER_PORT = "4399"
$env:OPENCODE_MODEL_REFRESH_SECONDS = "300"
$env:OPENCODE_MODEL_UPSTREAM_TIMEOUT_SECONDS = "60"
python tools/opencode-model-server/server.py
```

参数说明：

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `OPENCODE_MODEL_SERVER_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `OPENCODE_MODEL_SERVER_PORT` | `4399` | HTTP 监听端口 |
| `OPENCODE_MODEL_REFRESH_SECONDS` | `300` | 缓存有效期和后台刷新间隔 |
| `OPENCODE_MODEL_UPSTREAM_TIMEOUT_SECONDS` | `60` | 单个上游请求超时 |
| `OPENCODE_MODEL_STATE` | 脚本目录下的 `opencode-models.json` | 本机状态文件路径 |

状态文件默认为 `tools/opencode-model-server/opencode-models.json`，该文件不应提交到仓库。可用 `OPENCODE_MODEL_STATE` 指定其他位置。

也可以用 PowerShell 脚本在隐藏进程中启动或停止：

```powershell
powershell -ExecutionPolicy Bypass -File tools/opencode-model-server/start.ps1
powershell -ExecutionPolicy Bypass -File tools/opencode-model-server/stop.ps1
```

## 部署音箱

交叉编译出 `deepseek` 后，可以使用部署脚本上传、校验、原子替换并重启音箱端进程。脚本需要 Paramiko，但不会将密码写入参数或文件：

```powershell
python -m pip install paramiko
$env:SPEAKER_PASSWORD = "音箱当前 SSH 密码"
python tools/opencode-model-server/deploy_speaker.py `
  --host <speaker-ip> `
  --binary packages/client-rust/target/armv7-unknown-linux-gnueabihf/release/deepseek `
  --server-url http://<model-server-ip>:4399
Remove-Item Env:SPEAKER_PASSWORD
```

部署脚本会把服务地址写入 `/data/open-xiaoai/model-server-url`，并保留本地模型配置；首次说“配置模型”时再从服务端同步顺序。该地址也是自动发现的初始提示：如果电脑 DHCP 地址变化，音箱会探测同一 `/24` 局域网内端口 `4399` 的服务，校验 `/healthz` 标识后保存新地址并继续同步。

## 实现细节

服务并行请求 Zen 实时 `/models` 目录和 Models.dev 元数据，只保留当前 Zen 仍公布且 `cost.input == 0`、`cost.output == 0` 的模型。active 模型排在前面，Models.dev 标记为 `deprecated` 但 Zen 仍公布的模型排在后面。Models.dev 暂时不可用时，服务退化为保留 Zen 中 ID 以 `-free` 结尾的模型，并在响应的 `errors`/`stale` 字段中体现异常。

服务启动时加载状态文件；缓存超过 300 秒时，访问 `/api/models` 或 `/api/speaker/config` 会按需刷新，后台线程也会定期刷新。刷新失败且已有缓存时保留旧模型，不会让音箱失去可用候选。状态写入先落到同目录临时文件，再使用 `os.replace` 原子替换。

`custom_order` 为 `false` 时刷新使用发现顺序；通过 `PUT /api/order` 保存后变为 `true`，后续刷新会保留仍存在的旧顺序，把新模型追加到末尾，并删除已不再发现的模型。保存接口要求完整列出当前所有模型、不得重复、不得包含未知 ID，最多 64 个。

服务端不保存 API key，也不代理模型问答请求。音箱只在用户说“配置模型”时读取 `/api/speaker/config`；真正的 Chat Completions 请求仍由音箱直连 OpenCode Zen。

## 接口

- `GET /api/models`：返回模型详细配置和当前顺序，缓存超过 5 分钟时自动刷新。
- `GET /api/models/<model-id>`：返回单个模型的完整 Models.dev 配置。
- `GET /api/refresh`：立即重新获取两个上游目录。
- `PUT /api/order`：请求体为 `{ "order": ["model-id", "..."] }`。
- `GET /api/speaker/config`：音箱同步用的精简配置。
- `GET /api/config`：`/api/speaker/config` 的别名。
- `GET /healthz`：健康检查。

服务只应在可信局域网中开放。模型和音箱配置接口可被局域网读取，但保存顺序的 `PUT /api/order` 仅接受运行服务的本机请求；本机可通过回环地址或自己的网卡地址访问，其他设备仍会被拒绝。不要将端口暴露到公网。

`GET /api/models` 返回的每个模型包含统一摘要字段和完整的 Models.dev 原始配置：`id`、`name`、`status`、`free`、`available_in_zen_catalog`、`reasoning`、`tool_call`、`cost`、`limit`、`modalities`、`created`、`config`。音箱接口只保留 `order`、`planner_model` 以及用于展示的模型名称和状态，避免把完整 Models.dev 数据写入音箱。

状态文件中的 `custom_order`、`order` 和 `models` 是服务端内部持久化格式；音箱同步接口不会原样转发整个状态文件，而是返回如下最小字段：

```json
{
  "version": 1,
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

`order` 示例为了可读性只列出部分 ID；真实接口会返回当前全部模型。服务端每次成功刷新或保存顺序都会更新本机状态文件，音箱只有在收到“配置模型”语音命令后才拉取该接口。
