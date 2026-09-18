"""HTTP 入口（标准库）。

路由：
  GET  /health
  POST /events                      追加一条命令（JSON）
  GET  /events?event_code=          不可变事件日志
  GET  /snapshot/<event>?as_of=     截至业务时间的预算快照
  GET  /invoices?event_code=        发票族台账（拆票/红字/重开关联）
  POST /admin/sweep                 把过截止未决的审批标记过期
"""

from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from settlement import (
    ApprovalExpired,
    ApprovalRequired,
    DuplicateReceipt,
    EngineError,
)
from settlement.engine import event_log, invoice_register
from settlement.money import parse_time, to_json
from settlement.store import EventStore

SERVICE_NAME = '赛事预算与供应商结算引擎'

RUNTIME_DIR = os.getenv("RUNTIME_DIR", ".runtime")
STORE_PATH = os.path.join(RUNTIME_DIR, "events.jsonl")


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


def get_store() -> EventStore:
    if not hasattr(get_store, "_store"):
        get_store._store = EventStore(STORE_PATH)  # type: ignore[attr-defined]
    return get_store._store  # type: ignore[attr-defined]


def _as_of(query: dict[str, list[str]]):
    raw = (query.get("as_of") or [None])[0]
    return parse_time(raw) if raw else None


class RequestHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload) -> None:
        body = json.dumps(to_json(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as e:
            raise EngineError(f"JSON 解析失败: {e}")
        if not isinstance(data, dict):
            raise EngineError("请求体必须是 JSON 对象")
        return data

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            q = parse_qs(parsed.query)
            path = parsed.path
            store = get_store()
            if path == "/health":
                self._send(200, health_payload())
            elif path == "/events":
                ec = (q.get("event_code") or [None])[0]
                self._send(200, {"events": event_log(store.engine.state, ec)})
            elif path.startswith("/snapshot/"):
                code = path.rsplit("/", 1)[-1]
                snap = store.engine.snapshot(code, _as_of(q))
                self._send(200, snap)
            elif path == "/invoices":
                ec = (q.get("event_code") or [None])[0]
                self._send(200, {"invoices": invoice_register(store.engine.state, ec)})
            else:
                self.send_error(404, "Not Found")
        except EngineError as e:
            self._send(400, {"error": e.code if hasattr(e, "code") else "error", "message": str(e)})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": "internal", "message": "内部错误"})

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            store = get_store()
            if parsed.path == "/events":
                command = self._read_json()
                stored = store.handle(command)
                self._send(201, {"accepted": len(stored), "events": [
                    {
                        "seq": e.seq,
                        "type": e.type,
                        "occurred_at": e.occurred_at.isoformat(),
                    }
                    for e in stored
                ]})
            elif parsed.path == "/admin/sweep":
                data = self._read_json()
                at = parse_time(data["at"]) if data.get("at") else None
                swept = store.sweep(at)
                self._send(200, {"expired": len(swept)})
            else:
                self.send_error(404, "Not Found")
        except DuplicateReceipt as e:
            self._send(409, {"error": e.code, "message": str(e)})
        except (ApprovalRequired, ApprovalExpired) as e:
            self._send(402, {"error": e.code, "message": str(e)})
        except EngineError as e:
            status = 404 if e.code == "not_found" else 400
            self._send(status, {"error": e.code, "message": str(e)})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": "internal", "message": "内部错误"})

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), RequestHandler)
