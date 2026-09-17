#!/usr/bin/env python3
"""Small LAN service for discovering and ordering OpenCode free models."""

from __future__ import annotations

import json
import ipaddress
import os
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
MODELS_DEV_URL = "https://models.dev/api.json"
ZEN_CHAT_URL = "https://opencode.ai/zen/v1/chat/completions"
DEFAULT_HOST = os.environ.get("OPENCODE_MODEL_SERVER_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("OPENCODE_MODEL_SERVER_PORT", "4399"))
REFRESH_INTERVAL_SECONDS = int(os.environ.get("OPENCODE_MODEL_REFRESH_SECONDS", "300"))
UPSTREAM_TIMEOUT_SECONDS = int(os.environ.get("OPENCODE_MODEL_UPSTREAM_TIMEOUT_SECONDS", "60"))
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_PATH = Path(
    os.environ.get("OPENCODE_MODEL_STATE", str(SCRIPT_DIR / "opencode-models.json"))
)
MAX_ORDER_SIZE = 64


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_local_request(client_host: str, local_host: str) -> bool:
    """Accept loopback or a connection originating from its local destination address."""
    try:
        client_address = ipaddress.ip_address(client_host.split("%", 1)[0])
        local_address = ipaddress.ip_address(local_host.split("%", 1)[0])
    except ValueError:
        return False

    if isinstance(client_address, ipaddress.IPv6Address) and client_address.ipv4_mapped:
        client_address = client_address.ipv4_mapped
    if isinstance(local_address, ipaddress.IPv6Address) and local_address.ipv4_mapped:
        local_address = local_address.ipv4_mapped

    return client_address.is_loopback or (
        not local_address.is_unspecified and client_address == local_address
    )


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "open-xiaoai-opencode-model-server/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"HTTP {response.status} from {url}")
        return json.loads(response.read())


def as_number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def is_free_model(model: dict[str, Any]) -> bool:
    cost = model.get("cost")
    if isinstance(cost, dict):
        input_cost = as_number(cost.get("input"))
        output_cost = as_number(cost.get("output"))
        if input_cost == 0 and output_cost == 0:
            return True
    return False


def compact_model(model_id: str, metadata: dict[str, Any] | None, live: dict[str, Any] | None) -> dict[str, Any]:
    metadata = metadata or {}
    cost = metadata.get("cost") if isinstance(metadata.get("cost"), dict) else {}
    limit = metadata.get("limit") if isinstance(metadata.get("limit"), dict) else {}
    modalities = metadata.get("modalities") if isinstance(metadata.get("modalities"), dict) else {}
    status = metadata.get("status") or "active"
    return {
        "id": model_id,
        "name": metadata.get("name") or model_id,
        "status": status,
        "free": True,
        "provider": "opencode",
        "available_in_zen_catalog": live is not None,
        "reasoning": bool(metadata.get("reasoning", False)),
        "tool_call": bool(metadata.get("tool_call", False)),
        "cost": {
            "input": cost.get("input", 0),
            "output": cost.get("output", 0),
            "cache_read": cost.get("cache_read", 0),
            "cache_write": cost.get("cache_write", 0),
        },
        "limit": {
            "context": limit.get("context"),
            "input": limit.get("input"),
            "output": limit.get("output"),
        },
        "modalities": {
            "input": modalities.get("input", ["text"]),
            "output": modalities.get("output", ["text"]),
        },
        "created": live.get("created") if live else None,
        "config": metadata,
    }


def discover_models() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Merge the live Zen catalog with Models.dev's cost and lifecycle metadata."""
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        live_future = executor.submit(fetch_json, ZEN_MODELS_URL)
        metadata_future = executor.submit(fetch_json, MODELS_DEV_URL)
        try:
            live_payload = live_future.result()
        except Exception as error:  # noqa: BLE001 - stale cache is handled by caller
            live_payload = {"data": []}
            errors["zen"] = str(error)
        try:
            metadata_payload = metadata_future.result()
        except Exception as error:  # noqa: BLE001 - stale cache is handled by caller
            metadata_payload = {}
            errors["models_dev"] = str(error)

    live_items = live_payload.get("data", []) if isinstance(live_payload, dict) else []
    live_models = {
        item.get("id"): item
        for item in live_items
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    }
    provider = metadata_payload.get("opencode", {}) if isinstance(metadata_payload, dict) else {}
    metadata_items = provider.get("models", {}) if isinstance(provider, dict) else {}
    if not isinstance(metadata_items, dict):
        metadata_items = {}

    # Only keep IDs currently advertised by Zen. Models.dev verifies their price;
    # its lifecycle status is surfaced separately because Zen can still advertise
    # deprecated compatibility IDs such as deepseek-v4-flash-free.
    free_live_ids = [
        model_id
        for model_id in live_models
        if is_free_model(metadata_items.get(model_id, {}))
        or (not metadata_items and model_id.endswith("-free"))
    ]
    model_ids = [
        model_id
        for model_id in free_live_ids
        if metadata_items.get(model_id, {}).get("status") != "deprecated"
    ] + [
        model_id
        for model_id in free_live_ids
        if metadata_items.get(model_id, {}).get("status") == "deprecated"
    ]
    models = [compact_model(model_id, metadata_items.get(model_id), live_models.get(model_id)) for model_id in model_ids]
    return models, errors


def read_state() -> dict[str, Any]:
    try:
        with STATE_PATH.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_name(f".{STATE_PATH.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, STATE_PATH)


class ModelStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.state = read_state()
        self.refreshing = False

    def _saved_order(self) -> list[str]:
        order = self.state.get("order", [])
        return [model_id for model_id in order if isinstance(model_id, str)] if isinstance(order, list) else []

    def _merge_order(self, models: list[dict[str, Any]], saved_order: list[str] | None = None) -> list[str]:
        discovered = [model["id"] for model in models]
        existing = saved_order if saved_order is not None else self._saved_order()
        return list(dict.fromkeys([model_id for model_id in existing if model_id in discovered] + discovered))

    def _planner_model(self, models: list[dict[str, Any]], order: list[str]) -> str | None:
        by_id = {model["id"]: model for model in models}
        for model_id in order:
            if by_id.get(model_id, {}).get("tool_call"):
                return model_id
        return order[0] if order else None

    def _response(self, stale: bool = False, errors: dict[str, str] | None = None) -> dict[str, Any]:
        models = self.state.get("models", [])
        models = models if isinstance(models, list) else []
        order = self._merge_order(models)
        self.state["order"] = order
        return {
            "ok": True,
            "version": 1,
            "fetched_at": self.state.get("fetched_at"),
            "stale": stale,
            "errors": errors or self.state.get("errors", {}),
            "source": {
                "zen_models_url": ZEN_MODELS_URL,
                "models_dev_url": MODELS_DEV_URL,
            },
            "order": order,
            "planner_model": self._planner_model(models, order),
            "models": models,
        }

    def snapshot(self, refresh: bool = True) -> dict[str, Any]:
        with self.lock:
            fetched_at = self.state.get("fetched_at")
            should_refresh = refresh and (not fetched_at or self._age_seconds(fetched_at) >= REFRESH_INTERVAL_SECONDS)
        if should_refresh:
            self.refresh()
        with self.lock:
            return self._response(stale=bool(self.state.get("stale", False)))

    @staticmethod
    def _age_seconds(timestamp: Any) -> float:
        if not isinstance(timestamp, str):
            return float("inf")
        try:
            value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - value).total_seconds()
        except ValueError:
            return float("inf")

    def refresh(self) -> dict[str, Any]:
        with self.lock:
            if self.refreshing:
                return self._response(stale=bool(self.state.get("stale", False)))
            self.refreshing = True
        try:
            models, errors = discover_models()
            with self.lock:
                if not models:
                    if self.state.get("models"):
                        self.state["stale"] = True
                        self.state["errors"] = errors or {"catalog": "没有发现免费模型"}
                        return self._response(stale=True, errors=errors)
                    raise RuntimeError("没有发现可用的免费模型")
                custom_order = bool(self.state.get("custom_order", False))
                order = self._merge_order(models, self._saved_order() if custom_order else [])
                self.state = {
                    "version": 1,
                    "fetched_at": now_iso(),
                    "stale": False,
                    "errors": errors,
                    "custom_order": custom_order,
                    "order": order,
                    "models": models,
                }
                write_state(self.state)
                return self._response()
        finally:
            with self.lock:
                self.refreshing = False

    def set_order(self, order: Any) -> dict[str, Any]:
        if not isinstance(order, list) or not order or len(order) > MAX_ORDER_SIZE:
            raise ValueError(f"order 必须是 1 到 {MAX_ORDER_SIZE} 个模型 ID")
        if any(not isinstance(model_id, str) or not model_id or len(model_id) > 120 for model_id in order):
            raise ValueError("order 包含无效模型 ID")
        if len(set(order)) != len(order):
            raise ValueError("order 不能包含重复模型")
        with self.lock:
            known = {model["id"] for model in self.state.get("models", []) if isinstance(model, dict)}
            unknown = [model_id for model_id in order if model_id not in known]
            if unknown:
                raise ValueError(f"未知模型: {', '.join(unknown)}")
            missing = [model_id for model_id in known if model_id not in order]
            if missing:
                raise ValueError("order 必须包含全部可用模型")
            self.state["custom_order"] = True
            self.state["order"] = order
            self.state["stale"] = bool(self.state.get("stale", False))
            write_state(self.state)
            return self._response(stale=self.state["stale"])

    def speaker_config(self) -> dict[str, Any]:
        response = self.snapshot(refresh=True)
        return {
            "version": 1,
            "generated_at": now_iso(),
            "fetched_at": response.get("fetched_at"),
            "stale": response.get("stale", False),
            "api_url": ZEN_CHAT_URL,
            "order": response["order"],
            "planner_model": response.get("planner_model"),
            "models": [
                {
                    "id": model["id"],
                    "name": model.get("name", model["id"]),
                    "status": model.get("status", "active"),
                    "tool_call": model.get("tool_call", False),
                }
                for model in response["models"]
            ],
        }


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenCode 免费模型顺序</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <main class="shell">
    <header>
      <p class="eyebrow">OPEN-XIAOAI / ZEN MODEL CONTROL</p>
      <h1>免费模型调用顺序</h1>
      <p class="lede">实时合并 OpenCode Zen 模型目录与 Models.dev 配置。拖动模型调整音箱的故障切换顺序。</p>
    </header>
    <section class="toolbar">
      <button id="refresh" class="secondary">刷新目录</button>
      <button id="save">保存顺序</button>
      <span id="status" class="status">正在读取...</span>
    </section>
    <section class="meta">
      <div><span>当前规划模型</span><strong id="planner">-</strong></div>
      <div><span>目录更新时间</span><strong id="fetched">-</strong></div>
      <div><span>音箱同步接口</span><strong>/api/speaker/config</strong></div>
    </section>
    <section>
      <div id="models" class="models"></div>
      <p class="hint">标记为 deprecated 的模型仍可能被 Zen 目录接受，但建议优先使用 active 模型。</p>
    </section>
  </main>
  <script src="/static/app.js"></script>
</body>
</html>"""

STYLE_CSS = """:root {
  color-scheme: light;
  --ink: #19232c;
  --muted: #6d7880;
  --line: #d9dfdc;
  --paper: #f5f6f1;
  --card: #fffef9;
  --accent: #ee6d4d;
  --accent-dark: #bd4f35;
}
* { box-sizing: border-box; }
body { margin: 0; color: var(--ink); background: var(--paper); font: 16px/1.5 Georgia, "Microsoft YaHei", serif; }
.shell { width: min(920px, calc(100% - 32px)); margin: 0 auto; padding: 52px 0 72px; }
.eyebrow { margin: 0 0 12px; color: var(--accent-dark); font: 700 12px/1.2 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .14em; }
h1 { max-width: 640px; margin: 0; font-size: clamp(38px, 8vw, 74px); line-height: .98; letter-spacing: -.055em; }
.lede { max-width: 610px; margin: 24px 0 34px; color: var(--muted); font-size: 18px; }
.toolbar { display: flex; align-items: center; gap: 12px; margin: 0 0 18px; }
button { border: 0; border-radius: 999px; padding: 11px 18px; color: #fff; background: var(--accent); cursor: pointer; font: 700 14px ui-monospace, SFMono-Regular, Consolas, monospace; }
button:hover { background: var(--accent-dark); }
button.secondary { color: var(--ink); background: #e2e7e1; }
.status { color: var(--muted); font-size: 14px; }
.meta { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; margin-bottom: 28px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.meta div { min-width: 0; padding: 15px 14px 16px 0; }
.meta span, .meta strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meta span { color: var(--muted); font: 12px ui-monospace, SFMono-Regular, Consolas, monospace; text-transform: uppercase; }
.meta strong { margin-top: 5px; font-size: 15px; }
.models { display: grid; gap: 10px; }
.model { display: grid; grid-template-columns: 38px 1fr auto; align-items: center; gap: 14px; padding: 15px 17px; border: 1px solid var(--line); border-radius: 12px; background: var(--card); box-shadow: 0 3px 0 rgba(25,35,44,.03); }
.model.dragging { opacity: .45; border-color: var(--accent); }
.rank { color: var(--accent-dark); font: 700 21px ui-monospace, SFMono-Regular, Consolas, monospace; }
.model-name { margin: 0; font-size: 18px; }
.model-id { margin: 2px 0 0; color: var(--muted); font: 12px ui-monospace, SFMono-Regular, Consolas, monospace; word-break: break-all; }
.badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.badge { padding: 4px 8px; border-radius: 999px; color: #4e5c52; background: #e6eee7; font: 11px ui-monospace, SFMono-Regular, Consolas, monospace; }
.badge.deprecated { color: #874c3b; background: #f6dfd6; }
.badge.offline { color: #6d7880; background: #ececeb; }
.hint { color: var(--muted); font-size: 13px; }
@media (max-width: 640px) { .shell { padding-top: 32px; } .meta { grid-template-columns: 1fr; } .meta div { padding: 11px 0; } .model { grid-template-columns: 28px 1fr; gap: 10px; } .badges { grid-column: 2; justify-content: flex-start; } }
"""

APP_JS = """const modelsElement = document.querySelector('#models');
const statusElement = document.querySelector('#status');
const plannerElement = document.querySelector('#planner');
const fetchedElement = document.querySelector('#fetched');
let catalog = null;

function setStatus(message, error = false) {
  statusElement.textContent = message;
  statusElement.style.color = error ? '#bd4f35' : '';
}

function render() {
  if (!catalog) return;
  const byId = Object.fromEntries(catalog.models.map(model => [model.id, model]));
  plannerElement.textContent = catalog.planner_model || '-';
  fetchedElement.textContent = catalog.fetched_at || '未获取';
  modelsElement.replaceChildren();
  catalog.order.forEach((modelId, index) => {
    const model = byId[modelId] || { id: modelId, name: modelId, status: 'unknown' };
    const item = document.createElement('article');
    item.className = 'model';
    item.draggable = true;
    item.dataset.id = modelId;
    item.innerHTML = `<div class="rank">${String(index + 1).padStart(2, '0')}</div>
      <div><h2 class="model-name"></h2><p class="model-id"></p></div>
      <div class="badges"><span class="badge status-badge"></span>${model.available_in_zen_catalog === false ? '<span class="badge offline">metadata only</span>' : ''}</div>`;
    item.querySelector('.model-name').textContent = model.name;
    item.querySelector('.model-id').textContent = model.id;
    const badge = item.querySelector('.status-badge');
    badge.textContent = model.status || 'active';
    if (model.status === 'deprecated') badge.classList.add('deprecated');
    item.addEventListener('dragstart', () => item.classList.add('dragging'));
    item.addEventListener('dragend', () => { item.classList.remove('dragging'); syncOrderFromDom(); });
    item.addEventListener('dragover', event => {
      event.preventDefault();
      const dragging = modelsElement.querySelector('.dragging');
      if (dragging && dragging !== item) {
        const box = item.getBoundingClientRect();
        modelsElement.insertBefore(dragging, event.clientY < box.top + box.height / 2 ? item : item.nextSibling);
      }
    });
    modelsElement.append(item);
  });
}

function syncOrderFromDom() {
  catalog.order = [...modelsElement.querySelectorAll('.model')].map(item => item.dataset.id);
  render();
}

async function load(path = '/api/models') {
  setStatus('正在读取目录...');
  try {
    const response = await fetch(path);
    const body = await response.json();
    if (!response.ok || !body.ok) throw new Error(body.error || `HTTP ${response.status}`);
    catalog = body;
    render();
    setStatus(body.stale ? '使用缓存，目录刷新失败' : `${body.models.length} 个免费模型`);
  } catch (error) { setStatus(error.message, true); }
}

document.querySelector('#refresh').addEventListener('click', () => load('/api/refresh'));
document.querySelector('#save').addEventListener('click', async () => {
  try {
    const response = await fetch('/api/order', { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({order: catalog.order}) });
    const body = await response.json();
    if (!response.ok || !body.ok) throw new Error(body.error || `HTTP ${response.status}`);
    catalog = body;
    render();
    setStatus('调用顺序已保存');
  } catch (error) { setStatus(error.message, true); }
});
load();
"""


STORE = ModelStore()
REFRESH_STOP = threading.Event()


def refresh_loop() -> None:
    while not REFRESH_STOP.wait(REFRESH_INTERVAL_SECONDS):
        try:
            STORE.refresh()
        except Exception as error:  # noqa: BLE001 - the cached catalog remains usable
            print(f"Background model refresh failed: {error}")


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenXiaoaiModelServer/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def send_json(self, body: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def send_text(self, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("无效请求体长度") from error
        if length > 128 * 1024:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self.send_text(INDEX_HTML, "text/html")
        elif path == "/static/style.css":
            self.send_text(STYLE_CSS, "text/css")
        elif path == "/static/app.js":
            self.send_text(APP_JS, "application/javascript")
        elif path == "/api/models":
            self.handle_models()
        elif path.startswith("/api/models/"):
            self.handle_model_detail(unquote(path.removeprefix("/api/models/")))
        elif path == "/api/config":
            self.handle_speaker_config()
        elif path == "/api/refresh":
            self.handle_refresh()
        elif path == "/api/speaker/config":
            self.handle_speaker_config()
        elif path == "/healthz":
            self.send_json({"ok": True, "service": "opencode-model-server"})
        else:
            self.send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/order":
            self.send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        local_host = self.connection.getsockname()[0]
        if not is_local_request(self.client_address[0], local_host):
            self.send_json({"ok": False, "error": "只能从运行服务的本机修改调用顺序"}, HTTPStatus.FORBIDDEN)
            return
        try:
            payload = self.read_json()
            body = STORE.set_order(payload.get("order") if isinstance(payload, dict) else None)
            self.send_json(body)
        except ValueError as error:
            self.send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
        except OSError as error:
            self.send_json({"ok": False, "error": f"保存失败: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_models(self) -> None:
        try:
            self.send_json(STORE.snapshot())
        except RuntimeError as error:
            self.send_json({"ok": False, "error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)

    def handle_refresh(self) -> None:
        try:
            self.send_json(STORE.refresh())
        except Exception as error:  # noqa: BLE001 - API returns a useful error
            self.send_json({"ok": False, "error": f"刷新目录失败: {error}"}, HTTPStatus.BAD_GATEWAY)

    def handle_model_detail(self, model_id: str) -> None:
        if not model_id or "/" in model_id:
            self.send_json({"ok": False, "error": "无效模型 ID"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            response = STORE.snapshot()
            model = next((model for model in response["models"] if model.get("id") == model_id), None)
            if model is None:
                self.send_json({"ok": False, "error": "模型不存在"}, HTTPStatus.NOT_FOUND)
            else:
                self.send_json({"ok": True, "model": model})
        except RuntimeError as error:
            self.send_json({"ok": False, "error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)

    def handle_speaker_config(self) -> None:
        try:
            self.send_json(STORE.speaker_config())
        except RuntimeError as error:
            self.send_json({"ok": False, "error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)


def main() -> None:
    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), Handler)
    threading.Thread(target=refresh_loop, name="model-refresh", daemon=True).start()
    print(f"OpenCode model server listening on http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    print(f"State file: {STATE_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping")
    finally:
        REFRESH_STOP.set()
        server.server_close()


if __name__ == "__main__":
    main()
