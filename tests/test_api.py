"""HTTP 接口端到端测试：真实起服务、走 JSON 路由。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import build_service, create_server  # noqa: E402


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        service = build_service(cls.tmp.name)
        cls.server = create_server("127.0.0.1", 0, service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method: str, path: str, body: dict | None = None,
             expect: int = 200) -> dict:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                status = response.status
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = json.loads(exc.read().decode("utf-8"))
        self.assertEqual(expect, status, f"{method} {path} → {status}: {payload}")
        return payload

    def test_health(self):
        payload = self.call("GET", "/health")
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_is_json_404(self):
        payload = self.call("GET", "/nope", expect=404)
        self.assertEqual("not_found", payload["error"]["code"])

    def test_full_settlement_journey(self):
        # 预算 + 合同 + 报名
        self.call("POST", "/budgets", {
            "race_day": "2026-10-18", "cost_center": "CC-OPS", "currency": "CNY",
            "business_date": "2026-08-01",
            "lines": [{"item": "饮水站", "planned_quantity": "20",
                       "unit_price": "3000"}],
        }, expect=201)
        contract = self.call("POST", "/contracts", {
            "vendor": "后勤公司", "kind": "vendor",
            "race_day": "2026-10-18", "cost_center": "CC-OPS",
            "currency": "CNY", "effective_date": "2026-08-01",
            "lines": [{"item": "饮水站", "unit_price": "3000",
                       "quantity_basis": "fixed", "fixed_quantity": "20"}],
        }, expect=201)["contract"]

        # 完成量申报 → 预提
        self.call("POST", "/events", {
            "type": "fulfillment", "race_day": "2026-10-18",
            "business_date": "2026-10-18",
            "contract_id": contract["id"], "item": "饮水站", "quantity": "18",
        }, expect=201)

        # 赛后补开发票 → 应付
        invoice = self.call("POST", "/invoices", {
            "invoice_no": "INV-POST-1", "vendor": "后勤公司", "currency": "CNY",
            "business_date": "2026-10-25", "contract_id": contract["id"],
            "lines": [{"item": "饮水站", "quantity": "18", "unit_price": "3000"}],
        }, expect=201)["invoice"]
        self.assertEqual("54000.00", invoice["amount"])

        # 支付回执（重复发送验证幂等）
        first = self.call("POST", "/payments", {
            "idempotency_key": "BANK-POST-1", "vendor": "后勤公司",
            "amount": "54000", "currency": "CNY",
        }, expect=201)
        second = self.call("POST", "/payments", {
            "idempotency_key": "BANK-POST-1", "vendor": "后勤公司",
            "amount": "54000", "currency": "CNY",
        }, expect=200)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["payment"]["id"], second["payment"]["id"])

        # 快照：已付 54000，未结算承诺 6000（as_of 取赛事之后）
        snap = self.call(
            "GET", "/budgets/snapshot?race_day=2026-10-18&cost_center=CC-OPS"
            "&as_of=2026-10-31")
        item = snap["items"]["饮水站"]
        self.assertEqual("60000.00", item["budget_amount"])
        self.assertEqual("54000.00", item["paid"])
        self.assertEqual("6000.00", item["committed_open"])
        # 预算 60000 已被 6000 未结算承诺 + 54000 已付全部占用
        self.assertEqual("0.00", item["remaining"])

        # 偏差分解接口可用且恒等
        variance = self.call(
            "GET", "/budgets/variance?race_day=2026-10-18&cost_center=CC-OPS"
            "&as_of=2026-10-31")
        var = variance["items"]["饮水站"]["variance"]
        total = (float(var["quantity"]) + float(var["price"])
                 + float(var["contract_change"]))
        self.assertAlmostEqual(float(var["total"]), total, places=2)

    def test_validation_error_is_json_400(self):
        payload = self.call("POST", "/events", {"type": "registration"}, expect=400)
        self.assertEqual("validation", payload["error"]["code"])


if __name__ == "__main__":
    unittest.main()
