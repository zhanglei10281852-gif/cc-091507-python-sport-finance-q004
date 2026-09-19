"""结算引擎领域逻辑测试。

覆盖：
  - 承诺/预提/应付/支付/释放 分层账流转
  - 退赛释放按人数计提的承诺
  - 路线变更、赛事取消只影响未结算项目
  - 发票拆票 / 红字 / 重新开票的血缘与金额守恒
  - 支付回执幂等、乱序到达不重复付款
  - 按业务时间 + 接收时间重建预算快照
  - 偏差分解（人数/单价/合同变更）恒等式
  - 超支审批队列（截止时间、决策、自动关闭）
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine import service as service_module  # noqa: E402
from engine.service import ServiceError, SettlementService  # noqa: E402
from engine.store import Store  # noqa: E402

RACE_DAY = "2026-10-18"
CC = "CC-OPS"


class FakeClock:
    def __init__(self, start: str = "2026-08-01T09:00:00+00:00"):
        self.current = start

    def __call__(self) -> str:
        return self.current

    def set(self, value: str) -> None:
        self.current = value


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        patcher = mock.patch.object(service_module, "utc_now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc = SettlementService(Store(Path(self.tmp.name) / "store.json"))

    # -- 测试数据构造 ------------------------------------------------------

    def make_budget(self, item="计时芯片", qty="10000", price="12.00",
                    business_date="2026-08-01", basis="fixed"):
        return self.svc.create_budget({
            "race_day": RACE_DAY, "cost_center": CC, "currency": "CNY",
            "business_date": business_date,
            "lines": [{"item": item, "planned_quantity": qty,
                       "unit_price": price, "quantity_basis": basis}],
        })["budget"]

    def make_contract(self, item="计时芯片", price="12.00", basis="per_head",
                      factor="1", kind="vendor", vendor="计时公司",
                      effective="2026-08-01", cost_center=CC):
        line = {"item": item, "unit_price": price, "quantity_basis": basis}
        if basis == "per_head":
            line["per_head_factor"] = factor
        else:
            line["fixed_quantity"] = factor
        return self.svc.create_contract({
            "vendor": vendor, "kind": kind, "race_day": RACE_DAY,
            "cost_center": cost_center, "currency": "CNY",
            "effective_date": effective, "lines": [line],
        })["contract"]

    def register(self, count, business_date="2026-08-05", **extra):
        payload = {"type": "registration", "race_day": RACE_DAY,
                   "business_date": business_date, "count": count}
        payload.update(extra)
        return self.svc.ingest_event(payload)["event"]

    def fulfill(self, contract_id, qty, item="计时芯片",
                business_date="2026-09-01", **extra):
        payload = {"type": "fulfillment", "race_day": RACE_DAY,
                   "business_date": business_date,
                   "contract_id": contract_id, "item": item, "quantity": qty}
        payload.update(extra)
        return self.svc.ingest_event(payload)["event"]

    def make_invoice(self, invoice_no="INV-1", qty="4500", price="12.00",
                     item="计时芯片", vendor="计时公司", contract_id=None,
                     business_date="2026-09-10", amount_lines=None):
        lines = amount_lines or [{"item": item, "quantity": qty, "unit_price": price}]
        payload = {"invoice_no": invoice_no, "vendor": vendor,
                   "currency": "CNY", "business_date": business_date,
                   "race_day": RACE_DAY, "cost_center": CC, "lines": lines}
        if contract_id:
            payload["contract_id"] = contract_id
        return self.svc.receive_invoice(payload)["invoice"]

    def line_view(self, item="计时芯片", cost_center=CC):
        view = self.svc.ledger_view(race_day=RACE_DAY, cost_center=cost_center)
        for line in view["lines"]:
            if line["item"] == item:
                return line
        return None


class LayeredLedgerTest(ServiceTestCase):
    def test_commitment_accrual_payable_payment_flow(self):
        self.make_budget()
        contract = self.make_contract()
        self.register(10000)

        line = self.line_view()
        self.assertEqual("120000.00", line["committed_open"])

        self.fulfill(contract["id"], "4000")
        line = self.line_view()
        self.assertEqual("72000.00", line["committed_open"])
        self.assertEqual("48000.00", line["accrued_open"])

        invoice = self.make_invoice(contract_id=contract["id"])
        self.assertEqual("54000.00", invoice["amount"])
        line = self.line_view()
        # 应付先消耗预提 48000，再消耗承诺 6000
        self.assertEqual("0.00", line["accrued_open"])
        self.assertEqual("66000.00", line["committed_open"])
        self.assertEqual("54000.00", line["payable_open"])

        result = self.svc.receive_payment({
            "idempotency_key": "PAY-1", "vendor": "计时公司",
            "amount": "54000", "currency": "CNY",
            "business_date": "2026-09-15",
        })
        self.assertFalse(result["deduplicated"])
        line = self.line_view()
        self.assertEqual("0.00", line["payable_open"])
        self.assertEqual("54000.00", line["paid"])

        invoice_after = self.svc._find("invoices", invoice["id"], "发票")
        self.assertEqual("paid", invoice_after["status"])

    def test_withdrawal_releases_per_head_commitment(self):
        self.make_budget()
        self.make_contract()
        self.register(10000)
        self.svc.ingest_event({
            "type": "withdrawal", "race_day": RACE_DAY,
            "business_date": "2026-08-20", "count": 2000, "refund": "200",
        })
        line = self.line_view()
        self.assertEqual("96000.00", line["committed_open"])
        self.assertEqual("24000.00", line["released"])
        # 退款进入收入侧
        reg_line = self.line_view(item="报名费", cost_center="CC-REG")
        self.assertEqual("400000.00", reg_line["refund"])


class ChangeScopeTest(ServiceTestCase):
    def test_route_change_only_releases_unsettled(self):
        self.make_budget()
        contract = self.make_contract()
        self.register(10000)
        self.fulfill(contract["id"], "5000")  # 60000 已结算（预提）

        result = self.svc.ingest_event({
            "type": "route_change", "race_day": RACE_DAY,
            "business_date": "2026-09-20", "reason": "路线临时调整",
            "impacts": [{"contract_id": contract["id"], "item": "计时芯片",
                         "cancel": True}],
        })["event"]["result"]
        self.assertEqual("60000.00", result["released"][0]["released"])
        line = self.line_view()
        self.assertEqual("0.00", line["committed_open"])
        self.assertEqual("60000.00", line["accrued_open"])  # 已结算部分不动

        # 再次变更：无未结算余额 → 冲突
        again = self.svc.ingest_event({
            "type": "route_change", "race_day": RACE_DAY,
            "business_date": "2026-09-21",
            "impacts": [{"contract_id": contract["id"], "item": "计时芯片",
                         "cancel": True}],
        })["event"]["result"]
        self.assertTrue(again["conflicts"])
        line = self.line_view()
        self.assertEqual("60000.00", line["accrued_open"])

    def test_route_change_partial_release_reports_frozen(self):
        self.make_budget()
        contract = self.make_contract()
        self.register(10000)  # 承诺 120000
        self.fulfill(contract["id"], "5000")  # 60000 转预提，未结算 60000

        result = self.svc.ingest_event({
            "type": "route_change", "race_day": RACE_DAY,
            "business_date": "2026-09-20",
            "impacts": [{"contract_id": contract["id"], "item": "计时芯片",
                         "delta_amount": "90000"}],
        })["event"]["result"]
        # 只能释放未结算的 60000，其余 30000 冻结
        self.assertEqual("60000.00", result["released"][0]["released"])
        conflict = result["conflicts"][0]
        self.assertEqual("30000.00", conflict["frozen_settled"])

    def test_cancellation_releases_all_unsettled_and_freezes_settled(self):
        self.make_budget()
        contract_a = self.make_contract()
        contract_b = self.make_contract(item="场地租赁", price="50000",
                                        basis="fixed", factor="1",
                                        kind="venue", vendor="场馆方")
        self.register(10000)  # A 承诺 120000
        self.fulfill(contract_a["id"], "5000")  # A 60000 已结算

        result = self.svc.ingest_event({
            "type": "cancellation", "race_day": RACE_DAY,
            "business_date": "2026-10-01", "reason": "赛事取消",
        })["event"]["result"]

        released = {(r["contract_id"], r["item"]): r["released"]
                    for r in result["released"]}
        self.assertEqual("60000.00", released[(contract_a["id"], "计时芯片")])
        self.assertEqual("50000.00", released[(contract_b["id"], "场地租赁")])
        frozen = result["frozen"]
        self.assertEqual(1, len(frozen))
        self.assertEqual("60000.00", frozen[0]["frozen_settled"])

        line_a = self.line_view()
        self.assertEqual("0.00", line_a["committed_open"])
        self.assertEqual("60000.00", line_a["accrued_open"])
        line_b = self.line_view(item="场地租赁")
        self.assertEqual("0.00", line_b["committed_open"])


class InvoiceLifecycleTest(ServiceTestCase):
    def test_split_invoice_links_children_and_conserves_amount(self):
        self.make_budget(item="物资", qty="100", price="10.00")
        original = self.make_invoice(invoice_no="INV-1", item="物资",
                                     qty="100", price="10.00")
        result = self.svc.split_invoice(original["id"], {
            "children": [
                {"invoice_no": "INV-1A",
                 "lines": [{"item": "物资", "quantity": "40", "unit_price": "10"}]},
                {"invoice_no": "INV-1B",
                 "lines": [{"item": "物资", "quantity": "60", "unit_price": "10"}]},
            ],
        })
        self.assertEqual("split", result["original"]["status"])
        children = result["children"]
        self.assertEqual(2, len(children))
        for child in children:
            self.assertEqual(original["id"], child["origin_id"])
            self.assertEqual("split", child["relation"])
            self.assertEqual(original["root_id"], child["root_id"])

        lineage = self.svc.invoice_lineage(children[0]["id"])
        self.assertEqual(3, len(lineage["invoices"]))
        # 应付总额守恒
        line = self.line_view(item="物资")
        self.assertEqual("1000.00", line["payable_open"])

    def test_split_rejects_unbalanced_children(self):
        self.make_budget(item="物资", qty="100", price="10.00")
        original = self.make_invoice(invoice_no="INV-1", item="物资",
                                     qty="100", price="10.00")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.split_invoice(original["id"], {
                "children": [
                    {"invoice_no": "A1",
                     "lines": [{"item": "物资", "quantity": "30", "unit_price": "10"}]},
                    {"invoice_no": "A2",
                     "lines": [{"item": "物资", "quantity": "30", "unit_price": "10"}]},
                ],
            })
        self.assertEqual("validation", ctx.exception.code)

    def test_red_letter_partial_and_full(self):
        self.make_budget(item="物资", qty="100", price="10.00")
        original = self.make_invoice(invoice_no="INV-1", item="物资",
                                     qty="100", price="10.00")
        result = self.svc.red_letter_invoice(original["id"], {
            "invoice_no": "INV-1-RED1", "amount": "400"})
        self.assertEqual("red_letter", result["red_letter"]["relation"])
        self.assertEqual(original["id"], result["red_letter"]["origin_id"])
        self.assertEqual("400.00", result["original"]["reversed_amount"])
        line = self.line_view(item="物资")
        self.assertEqual("600.00", line["payable_open"])

        # 超额冲销被拒绝
        with self.assertRaises(ServiceError) as ctx:
            self.svc.red_letter_invoice(original["id"], {
                "invoice_no": "INV-1-RED2", "amount": "700"})
        self.assertEqual("validation", ctx.exception.code)

        # 冲销剩余全部 → reversed
        result = self.svc.red_letter_invoice(original["id"], {
            "invoice_no": "INV-1-RED3"})
        self.assertEqual("reversed", result["original"]["status"])
        line = self.line_view(item="物资")
        self.assertEqual("0.00", line["payable_open"])

        lineage = self.svc.invoice_lineage(original["id"])
        self.assertEqual(3, len(lineage["invoices"]))

    def test_red_letter_on_paid_invoice_flags_refund(self):
        self.make_budget(item="物资", qty="100", price="10.00")
        original = self.make_invoice(invoice_no="INV-1", item="物资",
                                     qty="100", price="10.00")
        self.svc.receive_payment({
            "idempotency_key": "PAY-1", "vendor": "计时公司",
            "amount": "1000", "currency": "CNY",
        })
        result = self.svc.red_letter_invoice(original["id"], {
            "invoice_no": "INV-1-RED", "amount": "400"})
        self.assertEqual("400.00", result["refund_due"])

    def test_reissue_supersedes_original_and_links_lineage(self):
        self.make_budget(item="物资", qty="100", price="10.00")
        original = self.make_invoice(invoice_no="INV-1", item="物资",
                                     qty="100", price="10.00")
        result = self.svc.reissue_invoice(original["id"], {
            "invoice_no": "INV-1-NEW",
            "lines": [{"item": "物资", "quantity": "110", "unit_price": "10"}],
        })
        self.assertEqual("superseded", result["original"]["status"])
        red = result["red_letter"]
        self.assertEqual("red_letter", red["relation"])
        self.assertEqual("-1000.00", red["amount"])
        new_invoice = result["reissued"]
        self.assertEqual("reissue", new_invoice["relation"])
        self.assertEqual(original["id"], new_invoice["origin_id"])
        self.assertEqual("1100.00", new_invoice["amount"])

        line = self.line_view(item="物资")
        self.assertEqual("1100.00", line["payable_open"])
        lineage = self.svc.invoice_lineage(new_invoice["id"])
        self.assertEqual(3, len(lineage["invoices"]))

    def test_reissue_after_manual_red_letter_generates_unique_number(self):
        self.make_budget(item="物资", qty="100", price="10.00")
        original = self.make_invoice(invoice_no="INV-1", item="物资",
                                     qty="100", price="10.00")
        # 先手工红字冲销 400（占用 INV-1-RED 单号）
        self.svc.red_letter_invoice(original["id"], {
            "invoice_no": "INV-1-RED", "amount": "400"})
        # 重开：内部红字单号应自动避让，冲销剩余 600
        result = self.svc.reissue_invoice(original["id"], {
            "invoice_no": "INV-1-NEW",
            "lines": [{"item": "物资", "quantity": "60", "unit_price": "10"}],
        })
        self.assertEqual("superseded", result["original"]["status"])
        self.assertEqual("-600.00", result["red_letter"]["amount"])
        self.assertNotEqual("INV-1-RED", result["red_letter"]["invoice_no"])
        line = self.line_view(item="物资")
        self.assertEqual("600.00", line["payable_open"])
        lineage = self.svc.invoice_lineage(original["id"])
        self.assertEqual(4, len(lineage["invoices"]))

    def test_duplicate_invoice_no_rejected(self):
        self.make_budget(item="物资", qty="100", price="10.00")
        self.make_invoice(invoice_no="INV-1", item="物资", qty="10", price="10")
        with self.assertRaises(ServiceError) as ctx:
            self.make_invoice(invoice_no="INV-1", item="物资", qty="10", price="10")
        self.assertEqual("duplicate", ctx.exception.code)


class PaymentTest(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.make_budget(item="物资", qty="1000", price="10.00")

    def test_idempotent_receipt_never_pays_twice(self):
        self.make_invoice(invoice_no="INV-1", item="物资", qty="50", price="10")
        first = self.svc.receive_payment({
            "idempotency_key": "BANK-1", "vendor": "计时公司",
            "amount": "500", "currency": "CNY",
        })
        second = self.svc.receive_payment({
            "idempotency_key": "BANK-1", "vendor": "计时公司",
            "amount": "500", "currency": "CNY",
        })
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["payment"]["id"], second["payment"]["id"])
        line = self.line_view(item="物资")
        self.assertEqual("500.00", line["paid"])
        self.assertEqual(1, len(self.svc.store.collection("payments")))

    def test_out_of_order_receipts_allocate_deterministically(self):
        inv1 = self.make_invoice(invoice_no="INV-1", item="物资", qty="50",
                                 price="10", business_date="2026-09-01")
        inv2 = self.make_invoice(invoice_no="INV-2", item="物资", qty="30",
                                 price="10", business_date="2026-09-05")
        # 回执金额覆盖两张票，应按发票日顺序分摊
        result = self.svc.receive_payment({
            "idempotency_key": "BANK-2", "vendor": "计时公司",
            "amount": "800", "currency": "CNY",
        })["payment"]
        self.assertEqual(2, len(result["allocations"]))
        self.assertEqual(inv1["id"], result["allocations"][0]["invoice_id"])
        self.assertEqual("500.00", result["allocations"][0]["amount"])
        self.assertEqual(inv2["id"], result["allocations"][1]["invoice_id"])
        self.assertEqual("300.00", result["allocations"][1]["amount"])
        for inv_id in (inv1["id"], inv2["id"]):
            self.assertEqual("paid", self.svc._find("invoices", inv_id, "发票")["status"])

    def test_overpayment_becomes_credit_not_double_payment(self):
        self.make_invoice(invoice_no="INV-1", item="物资", qty="50", price="10")
        result = self.svc.receive_payment({
            "idempotency_key": "BANK-3", "vendor": "计时公司",
            "amount": "700", "currency": "CNY",
        })
        self.assertEqual("200.00", result["payment"]["credit_amount"])
        line = self.line_view(item="物资")
        self.assertEqual("500.00", line["paid"])
        self.assertEqual("0.00", line["payable_open"])

    def test_receipt_arriving_before_invoice_parks_as_credit(self):
        # 乱序：回执先于发票到达 → 记预付余额，不重复付款
        early = self.svc.receive_payment({
            "idempotency_key": "BANK-4", "vendor": "计时公司",
            "amount": "300", "currency": "CNY",
        })["payment"]
        self.assertEqual("300.00", early["credit_amount"])
        self.assertEqual([], early["allocations"])

        invoice = self.make_invoice(invoice_no="INV-9", item="物资",
                                    qty="30", price="10")
        late = self.svc.receive_payment({
            "idempotency_key": "BANK-5", "vendor": "计时公司",
            "amount": "300", "currency": "CNY",
        })["payment"]
        self.assertEqual(invoice["id"], late["allocations"][0]["invoice_id"])
        line = self.line_view(item="物资")
        self.assertEqual("300.00", line["paid"])


class SnapshotTest(ServiceTestCase):
    def test_rebuild_snapshot_by_business_and_received_time(self):
        self.clock.set("2026-08-01T09:00:00+00:00")
        self.make_budget()
        self.make_contract()
        self.clock.set("2026-08-05T09:00:00+00:00")
        self.register(10000, business_date="2026-08-05")
        self.clock.set("2026-09-01T09:00:00+00:00")
        contract = self.svc.store.collection("contracts")[0]
        self.fulfill(contract["id"], "4000", business_date="2026-09-01")

        # 8 月底时点：只有承诺
        snap = self.svc.snapshot(RACE_DAY, CC, as_of="2026-08-31")
        self.assertEqual(10000, snap["headcount"])
        self.assertEqual("120000.00", snap["items"]["计时芯片"]["committed_open"])
        self.assertEqual("0.00", snap["items"]["计时芯片"]["accrued_open"])

        # 9 月底时点：承诺 72000 + 预提 48000
        snap = self.svc.snapshot(RACE_DAY, CC, as_of="2026-09-30")
        self.assertEqual("72000.00", snap["items"]["计时芯片"]["committed_open"])
        self.assertEqual("48000.00", snap["items"]["计时芯片"]["accrued_open"])

        # 双时间轴：以 8 月 5 日上午的"已知信息"重建 9 月底视角
        # → 履约记录当时尚未到达，不可见
        snap = self.svc.snapshot(
            RACE_DAY, CC, as_of="2026-09-30",
            received_before="2026-08-05T09:00:01+00:00")
        self.assertEqual("120000.00", snap["items"]["计时芯片"]["committed_open"])
        self.assertEqual("0.00", snap["items"]["计时芯片"]["accrued_open"])

        # 更早的接收截止：预算尚未到达
        snap = self.svc.snapshot(
            RACE_DAY, CC, as_of="2026-09-30",
            received_before="2026-07-01T00:00:00+00:00")
        self.assertIsNone(snap["budget_version"])
        self.assertEqual(0, snap["headcount"])

    def test_snapshot_cash_position(self):
        self.make_budget()
        contract = self.make_contract()
        self.register(10000, fee="200", cost_center="CC-REG")
        self.fulfill(contract["id"], "4000")
        self.make_invoice(contract_id=contract["id"])
        self.svc.receive_payment({
            "idempotency_key": "PAY-1", "vendor": "计时公司",
            "amount": "54000", "currency": "CNY",
        })
        snap = self.svc.snapshot(RACE_DAY, "CC-REG")
        self.assertEqual("2000000.00", snap["cash"]["registration_income"])
        snap_ops = self.svc.snapshot(RACE_DAY, CC)
        self.assertEqual("54000.00", snap_ops["cash"]["paid_out"])


class VarianceTest(ServiceTestCase):
    def test_variance_decomposes_into_quantity_price_contract(self):
        # 基线：10000 人 × 15 元奖牌
        self.register(10000, business_date="2026-07-15")
        self.make_budget(item="奖牌", qty="10000", price="15.00",
                         business_date="2026-08-01", basis="per_head")
        contract = self.make_contract(item="奖牌", price="15.00",
                                      effective="2026-08-01")
        # 人数 +2000
        self.register(2000, business_date="2026-08-15")
        # 合同换版：单价 15 → 16
        self.svc.add_contract_version(contract["id"], {
            "effective_date": "2026-09-01", "reason": "供应商调价",
            "lines": [{"item": "奖牌", "unit_price": "16.00",
                       "quantity_basis": "per_head", "per_head_factor": "1"}],
        })
        # 实际完成 11500，按合同价预提
        self.fulfill(contract["id"], "11500", item="奖牌",
                     business_date="2026-09-10")
        # 发票单价 16.5 → 价格偏差
        self.make_invoice(invoice_no="INV-M", item="奖牌", qty="11500",
                          price="16.50", contract_id=contract["id"],
                          business_date="2026-09-15")

        result = self.svc.variance(RACE_DAY, CC, as_of="2026-09-30")
        item = result["items"]["奖牌"]
        self.assertEqual("150000.00", item["baseline_amount"])
        self.assertEqual("189750.00", item["actual_amount"])
        var = item["variance"]
        # 人数/数量：+2000 人 × 15 = 30000；少完成 500 × 16 = -8000
        self.assertEqual("22000.00", var["quantity"])
        self.assertEqual("30000.00", item["quantity_split"]["headcount_driven"])
        self.assertEqual("-8000.00", item["quantity_split"]["completion_driven"])
        # 合同变更：(16-15) × 12000
        self.assertEqual("12000.00", var["contract_change"])
        # 单价：(16.5-16) × 11500
        self.assertEqual("5750.00", var["price"])
        # 恒等：三成因之和 = 总偏差
        total = (Decimal(var["quantity"]) + Decimal(var["price"])
                 + Decimal(var["contract_change"]))
        self.assertEqual(Decimal(var["total"]), total)
        self.assertEqual(Decimal("39750.00"), total)


class ApprovalTest(ServiceTestCase):
    def test_overspend_enters_queue_with_deadline(self):
        self.make_budget(item="物资", qty="100", price="100.00")  # 预算 10000
        self.clock.set("2026-09-01T10:00:00+00:00")
        self.make_contract(item="物资", price="100.00", basis="fixed",
                           factor="150", effective="2026-09-01")

        queue = self.svc.approval_queue()["approvals"]
        self.assertEqual(1, len(queue))
        approval = queue[0]
        self.assertEqual("pending", approval["status"])
        self.assertEqual("overspend", approval["kind"])
        self.assertEqual("10000.00", approval["budget_amount"])
        self.assertEqual("15000.00", approval["projected_amount"])
        self.assertEqual("5000.00", approval["over_amount"])
        self.assertEqual("2026-09-04T10:00:00+00:00", approval["deadline"])

        # 到期未决 → expired
        self.clock.set("2026-09-05T10:00:00+00:00")
        expired = self.svc.approval_queue()["approvals"][0]
        self.assertEqual("expired", expired["status"])
        with self.assertRaises(ServiceError):
            self.svc.decide_approval(approval["id"], {"decision": "approved"})

    def test_approval_decision_and_auto_resolve(self):
        self.make_budget(item="物资", qty="100", price="100.00")
        contract = self.make_contract(item="物资", price="100.00",
                                      basis="fixed", factor="150",
                                      effective="2026-09-01")
        approval = self.svc.approval_queue()["approvals"][0]
        decided = self.svc.decide_approval(
            approval["id"], {"decision": "approved", "note": "赛事总监确认"})["approval"]
        self.assertEqual("approved", decided["status"])

        # 再次超支 → 新的审批项
        self.svc.add_contract_version(contract["id"], {
            "effective_date": "2026-09-02",
            "lines": [{"item": "物资", "unit_price": "100.00",
                       "quantity_basis": "fixed", "fixed_quantity": "180"}],
        })
        pendings = self.svc.approval_queue(status="pending")["approvals"]
        self.assertEqual(1, len(pendings))
        self.assertEqual("8000.00", pendings[0]["over_amount"])

        # 路线变更释放部分承诺 → 占用回落 → 自动关闭
        self.svc.ingest_event({
            "type": "route_change", "race_day": RACE_DAY,
            "business_date": "2026-09-03",
            "impacts": [{"contract_id": contract["id"], "item": "物资",
                         "delta_amount": "9000"}],
        })
        resolved = self.svc.approval_queue(status="resolved")["approvals"]
        self.assertEqual(1, len(resolved))


class SponsorTest(ServiceTestCase):
    def test_sponsor_benefit_cancellation_only_unsettled(self):
        sponsor = self.svc.create_contract({
            "vendor": "赞助商A", "kind": "sponsor", "race_day": RACE_DAY,
            "cost_center": "CC-SPO", "currency": "CNY",
            "effective_date": "2026-08-01",
            "lines": [{"item": "冠名权益", "unit_price": "500000",
                       "quantity_basis": "fixed", "fixed_quantity": "1"}],
        })["contract"]

        # 路线调整取消部分权益
        result = self.svc.ingest_event({
            "type": "sponsor_change", "race_day": RACE_DAY,
            "business_date": "2026-09-01",
            "contract_id": sponsor["id"], "item": "冠名权益",
            "delta_amount": "-200000", "reason": "路线调整取消沿途曝光位",
        })["event"]["result"]
        self.assertFalse("conflict" in result)
        line = self.line_view(item="冠名权益", cost_center="CC-SPO")
        self.assertEqual("300000.00", line["income_committed_open"])

        # 赞助款到账 300000
        self.svc.receive_payment({
            "idempotency_key": "SP-1", "vendor": "赞助商A",
            "amount": "300000", "currency": "CNY",
            "direction": "inbound", "contract_id": sponsor["id"],
            "item": "冠名权益",
        })
        line = self.line_view(item="冠名权益", cost_center="CC-SPO")
        self.assertEqual("0.00", line["income_committed_open"])
        self.assertEqual("300000.00", line["income_received"])

        # 再取消 → 已全部结算，只能冻结
        result = self.svc.ingest_event({
            "type": "sponsor_change", "race_day": RACE_DAY,
            "business_date": "2026-09-10",
            "contract_id": sponsor["id"], "item": "冠名权益",
            "delta_amount": "-100000",
        })["event"]["result"]
        self.assertEqual("100000.00", result["conflict"]["frozen_settled"])

        snap = self.svc.snapshot(RACE_DAY, "CC-SPO")
        self.assertEqual("300000.00", snap["cash"]["sponsor_received"])


class RobustnessTest(ServiceTestCase):
    """边界与失败原子性：非法输入不得留下部分状态。"""

    def test_invoice_rejects_empty_and_negative_lines(self):
        self.make_budget(item="物资", qty="100", price="10.00")
        with self.assertRaises(ServiceError):
            self.svc.receive_invoice({
                "invoice_no": "INV-E", "vendor": "计时公司", "currency": "CNY",
                "race_day": RACE_DAY, "cost_center": CC, "lines": []})
        with self.assertRaises(ServiceError):
            self.svc.receive_invoice({
                "invoice_no": "INV-N", "vendor": "计时公司", "currency": "CNY",
                "race_day": RACE_DAY, "cost_center": CC,
                "lines": [{"item": "物资", "quantity": "-1", "unit_price": "10"}]})
        self.assertEqual([], self.svc.store.collection("invoices"))

    def test_reissue_with_duplicate_number_leaves_original_untouched(self):
        self.make_budget(item="物资", qty="100", price="10.00")
        original = self.make_invoice(invoice_no="INV-1", item="物资",
                                     qty="100", price="10.00")
        self.make_invoice(invoice_no="INV-2", item="物资", qty="10", price="10")
        entries_before = len(self.svc.store.collection("entries"))
        with self.assertRaises(ServiceError) as ctx:
            self.svc.reissue_invoice(original["id"], {
                "invoice_no": "INV-2",
                "lines": [{"item": "物资", "quantity": "50", "unit_price": "10"}]})
        self.assertEqual("duplicate", ctx.exception.code)
        # 原票未被冲红，账本无新增分录
        untouched = self.svc._find("invoices", original["id"], "发票")
        self.assertEqual("received", untouched["status"])
        self.assertEqual("0.00", untouched["reversed_amount"])
        self.assertEqual(entries_before, len(self.svc.store.collection("entries")))

    def test_split_with_duplicate_child_numbers_is_atomic(self):
        self.make_budget(item="物资", qty="100", price="10.00")
        original = self.make_invoice(invoice_no="INV-1", item="物资",
                                     qty="100", price="10.00")
        with self.assertRaises(ServiceError):
            self.svc.split_invoice(original["id"], {
                "children": [
                    {"invoice_no": "SAME",
                     "lines": [{"item": "物资", "quantity": "50", "unit_price": "10"}]},
                    {"invoice_no": "SAME",
                     "lines": [{"item": "物资", "quantity": "50", "unit_price": "10"}]},
                ],
            })
        untouched = self.svc._find("invoices", original["id"], "发票")
        self.assertEqual("received", untouched["status"])
        self.assertEqual(1, len(self.svc.store.collection("invoices")))

    def test_failed_event_rolls_back_entries(self):
        self.make_budget()
        contract = self.make_contract()
        self.register(100)
        entries_before = len(self.svc.store.collection("entries"))
        events_before = len(self.svc.store.collection("events"))
        with self.assertRaises(ServiceError):
            # fulfillment 引用不存在的合同行 → 失败应回滚
            self.svc.ingest_event({
                "type": "fulfillment", "race_day": RACE_DAY,
                "business_date": "2026-09-01",
                "contract_id": contract["id"], "item": "不存在", "quantity": 10})
        self.assertEqual(entries_before, len(self.svc.store.collection("entries")))
        self.assertEqual(events_before, len(self.svc.store.collection("events")))

    def test_event_idempotency_key_deduplicates(self):
        self.make_budget()
        self.make_contract()
        first = self.svc.ingest_event({
            "type": "registration", "race_day": RACE_DAY,
            "business_date": "2026-08-05", "count": 100,
            "idempotency_key": "REG-BATCH-1"})
        second = self.svc.ingest_event({
            "type": "registration", "race_day": RACE_DAY,
            "business_date": "2026-08-05", "count": 100,
            "idempotency_key": "REG-BATCH-1"})
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(100, self.svc.headcount(RACE_DAY))


class PersistenceTest(ServiceTestCase):
    def test_state_survives_reload(self):
        self.make_budget()
        self.make_contract()
        self.register(10000)
        store_path = Path(self.tmp.name) / "store.json"
        reloaded = SettlementService(Store(store_path))
        line = [l for l in reloaded.ledger_view(race_day=RACE_DAY)["lines"]
                if l["item"] == "计时芯片"][0]
        self.assertEqual("120000.00", line["committed_open"])


if __name__ == "__main__":
    unittest.main()
