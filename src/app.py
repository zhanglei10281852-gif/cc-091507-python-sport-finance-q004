"""HTTP 接口层：标准库实现，全部 JSON 响应。

路由总览：
  GET  /health
  POST /contracts                     创建合同（含首版条款，形成承诺）
  GET  /contracts[/{id}]
  POST /contracts/{id}/versions       合同换版（只影响未结算部分）
  POST /budgets                       预算版本（按赛事日+成本中心）
  GET  /budgets/snapshot              重建当时预算快照
  GET  /budgets/variance              偏差分解（人数/单价/合同变更）
  POST /events                        报名/退赛/赞助变更/路线变更/取消/完成量
  GET  /events
  POST /invoices                      接收供应商发票（形成应付）
  GET  /invoices[/{id}]
  POST /invoices/{id}/split           拆票
  POST /invoices/{id}/red-letter      红字发票
  POST /invoices/{id}/reissue         重新开票
  GET  /invoices/{id}/lineage         发票血缘（原单 + 全部子单）
  POST /payments                      支付回执（幂等，乱序安全）
  GET  /payments
  GET  /ledger                        分层账视图
  GET  /approvals                     超支审批队列（带截止时间）
  POST /approvals/{id}/decision       审批决策
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from engine.money import MoneyError
from engine.service import ServiceError, SettlementService
from engine.store import Store

SERVICE_NAME = '赛事预算与供应商结算引擎'


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


def build_service(runtime_dir: str | Path) -> SettlementService:
    """加载公开领域参考（币种等）并构建服务。"""
    currencies: tuple[str, ...] = ("CNY", "HKD", "USD")
    domain_path = Path(__file__).resolve().parents[1] / "reference" / "domain.json"
    try:
        domain = json.loads(domain_path.read_text(encoding="utf-8"))
        if domain.get("currencies"):
            currencies = tuple(domain["currencies"])
    except (OSError, json.JSONDecodeError):
        pass
    store = Store(Path(runtime_dir) / "store.json")
    return SettlementService(store, currencies=currencies)


Handler = Callable[[SettlementService, dict[str, str], dict[str, str], Any], Any]

ROUTES: list[tuple[str, re.Pattern[str], Handler]] = []


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    """注册路由；pattern 中 {name} 捕获路径参数。"""
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def decorator(fn: Handler) -> Handler:
        ROUTES.append((method, regex, fn))
        return fn

    return decorator


# ---------------------------------------------------------------------------
# 合同 / 预算
# ---------------------------------------------------------------------------

@route("POST", "/contracts")
def create_contract(svc, params, query, body):
    return svc.create_contract(body), 201


@route("GET", "/contracts")
def list_contracts(svc, params, query, body):
    return {"contracts": svc.store.collection("contracts")}


@route("GET", "/contracts/{contract_id}")
def get_contract(svc, params, query, body):
    return {"contract": svc._find("contracts", params["contract_id"], "合同")}


@route("POST", "/contracts/{contract_id}/versions")
def add_version(svc, params, query, body):
    return svc.add_contract_version(params["contract_id"], body), 201


@route("POST", "/budgets")
def create_budget(svc, params, query, body):
    return svc.create_budget(body), 201


@route("GET", "/budgets/snapshot")
def budget_snapshot(svc, params, query, body):
    _require_query(query, "race_day", "cost_center")
    return svc.snapshot(
        query["race_day"], query["cost_center"],
        as_of=query.get("as_of"), received_before=query.get("received_before"))


@route("GET", "/budgets/variance")
def budget_variance(svc, params, query, body):
    _require_query(query, "race_day", "cost_center")
    return svc.variance(
        query["race_day"], query["cost_center"],
        as_of=query.get("as_of"), received_before=query.get("received_before"))


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------

@route("POST", "/events")
def ingest_event(svc, params, query, body):
    result = svc.ingest_event(body)
    return result, (200 if result.get("deduplicated") else 201)


@route("GET", "/events")
def list_events(svc, params, query, body):
    events = svc.store.collection("events")
    if query.get("race_day"):
        events = [e for e in events if e["race_day"] == query["race_day"]]
    if query.get("type"):
        events = [e for e in events if e["type"] == query["type"]]
    return {"events": events}


# ---------------------------------------------------------------------------
# 发票
# ---------------------------------------------------------------------------

@route("POST", "/invoices")
def receive_invoice(svc, params, query, body):
    result = svc.receive_invoice(body)
    return result, (200 if result.get("deduplicated") else 201)


@route("GET", "/invoices")
def list_invoices(svc, params, query, body):
    invoices = svc.store.collection("invoices")
    if query.get("vendor"):
        invoices = [i for i in invoices if i["vendor"] == query["vendor"]]
    if query.get("status"):
        invoices = [i for i in invoices if i["status"] == query["status"]]
    return {"invoices": invoices}


@route("GET", "/invoices/{invoice_id}")
def get_invoice(svc, params, query, body):
    return {"invoice": svc._find("invoices", params["invoice_id"], "发票")}


@route("GET", "/invoices/{invoice_id}/lineage")
def invoice_lineage(svc, params, query, body):
    return svc.invoice_lineage(params["invoice_id"])


@route("POST", "/invoices/{invoice_id}/split")
def split_invoice(svc, params, query, body):
    return svc.split_invoice(params["invoice_id"], body), 201


@route("POST", "/invoices/{invoice_id}/red-letter")
def red_letter(svc, params, query, body):
    return svc.red_letter_invoice(params["invoice_id"], body), 201


@route("POST", "/invoices/{invoice_id}/reissue")
def reissue(svc, params, query, body):
    return svc.reissue_invoice(params["invoice_id"], body), 201


# ---------------------------------------------------------------------------
# 支付 / 账层 / 审批
# ---------------------------------------------------------------------------

@route("POST", "/payments")
def receive_payment(svc, params, query, body):
    result = svc.receive_payment(body)
    return result, (200 if result.get("deduplicated") else 201)


@route("GET", "/payments")
def list_payments(svc, params, query, body):
    return {"payments": svc.store.collection("payments")}


@route("GET", "/ledger")
def ledger_view(svc, params, query, body):
    return svc.ledger_view(
        race_day=query.get("race_day"), cost_center=query.get("cost_center"))


@route("GET", "/approvals")
def approvals(svc, params, query, body):
    return svc.approval_queue(status=query.get("status"))


@route("POST", "/approvals/{approval_id}/decision")
def decide(svc, params, query, body):
    return svc.decide_approval(params["approval_id"], body)


@route("GET", "/health")
def health(svc, params, query, body):
    return health_payload()


def _require_query(query: dict[str, str], *names: str) -> None:
    missing = [n for n in names if not query.get(n)]
    if missing:
        raise ServiceError("validation", f"缺少查询参数: {', '.join(missing)}")


class RequestHandler(BaseHTTPRequestHandler):
    service: SettlementService  # 由 create_server 注入

    def _handle(self) -> None:
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        body: Any = None
        if self.command in ("POST", "PUT", "PATCH"):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send(400, {"error": {"code": "bad_json",
                                               "message": "请求体不是合法 JSON"}})
                    return
            else:
                body = {}
        try:
            for method, regex, handler in ROUTES:
                if method != self.command:
                    continue
                match = regex.match(parsed.path)
                if not match:
                    continue
                with self.service.store.lock:
                    result = handler(self.service, match.groupdict(), query, body)
                status = 200
                if isinstance(result, tuple):
                    result, status = result
                self._send(status, result)
                return
            self._send(404, {"error": {"code": "not_found", "message": "路由不存在"}})
        except ServiceError as exc:
            self._send(exc.status, {"error": exc.to_dict()})
        except MoneyError as exc:
            self._send(400, {"error": {"code": "bad_amount", "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001 - 兜底，保证 JSON 错误响应
            self._send(500, {"error": {"code": "internal", "message": str(exc)}})

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    host: str, port: int, service: SettlementService | None = None
) -> ThreadingHTTPServer:
    if service is None:
        service = build_service(".runtime")
    handler_class = type("BoundRequestHandler", (RequestHandler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler_class)
