"""端到端业务场景测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from settlement import (
    ApprovalExpired,
    ApprovalRequired,
    DuplicateReceipt,
    SettlementEngine,
)
from settlement.errors import Conflict, EngineError, NotFound
from settlement.money import parse_time
from settlement.store import EventStore

RACE = "2026-03-15T00:00:00+00:00"


def cmd(t: str, at: str, payload: dict, received: str | None = None) -> dict:
    return {"type": t, "occurred_at": at, "received_at": received or at, "payload": payload}


def build_scenario() -> SettlementEngine:
    e = SettlementEngine()

    def send(t, at, p, rcv=None):
        e.handle(cmd(t, at, p, rcv))

    # 赛事与成本中心
    send("event_created", "2026-01-05T09:00Z", {
        "code": "MAR", "name": "城市马拉松", "race_date": RACE})
    for code, name, direction, budget, hours in [
        ("REG", "报名收入", -1, 200000, 48),
        ("SPO", "赞助收入", -1, 60000, 48),
        ("VEN", "场地与交通", 1, 100000, 48),
        ("OPS", "运营保障", 1, 60000, 72),
        ("VOL", "志愿者", 1, 20000, 48),
        ("SWAG", "参赛物资", 1, 30000, 48),
    ]:
        send("cost_center_created", "2026-01-05T09:00Z", {
            "code": code, "name": name, "direction": direction,
            "budget": budget, "approval_hours": hours})

    # 报名：单价 200，预算按 1000 人编制；实际 1000 报名、80 退赛 = 920
    send("commitment_created", "2026-01-10T09:00Z", {
        "id": "C-REG", "event_code": "MAR", "cost_center": "REG",
        "kind": "registration", "title": "报名费", "unit_price": 200,
        "planned_qty": 1000})
    for i in range(10):
        send("registration", f"2026-02-{i+1:02d}T10:00Z",
             {"id": f"R{i}", "commitment_id": "C-REG", "delta": 100})
    send("withdrawal", "2026-03-10T10:00Z",
         {"id": "W1", "commitment_id": "C-REG", "delta": 80})

    # 赞助：固定 50000；路线调整取消 10000 权益
    send("commitment_created", "2026-01-12T09:00Z", {
        "id": "C-SPO", "event_code": "MAR", "cost_center": "SPO",
        "kind": "sponsor", "title": "冠名赞助", "vendor": "星河银行",
        "fixed": 50000, "planned_qty": 0})
    send("scope_adjusted", "2026-02-20T09:00Z", {
        "commitment_id": "C-SPO", "delta_fixed": -10000,
        "reason": "路线改道取消起终点广告牌"})

    # 场地租赁 80000
    send("commitment_created", "2026-01-15T09:00Z", {
        "id": "C-VENUE", "event_code": "MAR", "cost_center": "VEN",
        "kind": "venue", "title": "起终点场地", "vendor": "城投场地",
        "fixed": 80000})

    # 接驳巴士：10 辆 * 1000
    send("commitment_created", "2026-01-15T09:00Z", {
        "id": "C-BUS", "event_code": "MAR", "cost_center": "VEN",
        "kind": "contract", "title": "接驳巴士", "vendor": "迅达客运",
        "unit_price": 1000, "planned_qty": 10})
    send("registration", "2026-03-14T08:00Z",
         {"id": "B1", "commitment_id": "C-BUS", "delta": 10})

    # 医疗：每人 10 元，按实际 920 人
    send("commitment_created", "2026-01-15T09:00Z", {
        "id": "C-MED", "event_code": "MAR", "cost_center": "OPS",
        "kind": "contract", "title": "医疗保障", "vendor": "安康医疗",
        "unit_price": 10, "planned_qty": 1000})
    send("registration", "2026-03-15T06:00Z",
         {"id": "M1", "commitment_id": "C-MED", "delta": 920})

    # 计时芯片：预算 45/人，2 月合同涨到 48，另加 500 固定费；
    # 供应商发票按 52000 补开（高于合同口径，形成超支）
    send("commitment_created", "2026-01-15T09:00Z", {
        "id": "C-CHIP", "event_code": "MAR", "cost_center": "OPS",
        "kind": "contract", "title": "计时芯片", "vendor": "精准计时",
        "unit_price": 45, "planned_qty": 1000})
    send("contract_versioned", "2026-02-01T09:00Z", {
        "commitment_id": "C-CHIP", "unit_price": 48,
        "note": "新增芯片回收服务"})
    send("scope_adjusted", "2026-02-05T09:00Z", {
        "commitment_id": "C-CHIP", "delta_fixed": 500, "reason": "燃油附加"})
    send("registration", "2026-03-15T06:00Z",
         {"id": "P1", "commitment_id": "C-CHIP", "delta": 920})

    # 志愿者补贴：200 人 * 50
    send("commitment_created", "2026-01-20T09:00Z", {
        "id": "C-VOL", "event_code": "MAR", "cost_center": "VOL",
        "kind": "volunteer", "title": "志愿者补贴",
        "unit_price": 50, "planned_qty": 200})
    send("registration", "2026-03-14T08:00Z",
         {"id": "V1", "commitment_id": "C-VOL", "delta": 200})

    # T 恤：赛事前取消（完全未结算）
    send("commitment_created", "2026-01-20T09:00Z", {
        "id": "C-TEE", "event_code": "MAR", "cost_center": "SWAG",
        "kind": "contract", "title": "参赛 T 恤", "vendor": "速印制衣",
        "fixed": 30000})
    send("cancellation", "2026-03-10T12:00Z", {
        "commitment_id": "C-TEE", "reason": "物资改由赞助实物提供"})

    # 赛后发票
    # 医疗：先开 9200，随后红字全额冲回并重开 8800（80 退赛不计服务）
    send("invoice_received", "2026-03-20T10:00Z", {
        "id": "INV-MED", "event_code": "MAR", "vendor": "安康医疗",
        "invoice_no": "MED-001", "issued_at": "2026-03-20T10:00Z",
        "lines": [{"commitment_id": "C-MED", "amount": 9200}]},
        rcv="2026-03-25T10:00Z")  # 票据延迟到达
    send("invoice_received", "2026-03-26T10:00Z", {
        "id": "INV-MED-R", "event_code": "MAR", "vendor": "安康医疗",
        "invoice_no": "MED-001-R", "issued_at": "2026-03-26T10:00Z",
        "kind": "red", "original_id": "INV-MED",
        "lines": [{"commitment_id": "C-MED", "amount": -9200}]})
    send("invoice_received", "2026-03-26T11:00Z", {
        "id": "INV-MED-2", "event_code": "MAR", "vendor": "安康医疗",
        "invoice_no": "MED-002", "issued_at": "2026-03-26T11:00Z",
        "kind": "reissue", "original_id": "INV-MED",
        "replaces_id": "INV-MED",
        "lines": [{"commitment_id": "C-MED", "amount": 8800}]})

    # 场地：一张 80000 原票拆成两张
    send("invoice_received", "2026-03-20T10:00Z", {
        "id": "INV-VEN", "event_code": "MAR", "vendor": "城投场地",
        "invoice_no": "VEN-001", "issued_at": "2026-03-20T10:00Z",
        "lines": [{"commitment_id": "C-VENUE", "amount": 80000}]})
    send("invoice_received", "2026-03-21T10:00Z", {
        "id": "INV-VEN-A", "event_code": "MAR", "vendor": "城投场地",
        "invoice_no": "VEN-001-A", "issued_at": "2026-03-21T10:00Z",
        "kind": "normal", "original_id": "INV-VEN",
        "lines": [{"commitment_id": "C-VENUE", "amount": 50000}]})
    send("invoice_received", "2026-03-22T10:00Z", {
        "id": "INV-VEN-B", "event_code": "MAR", "vendor": "城投场地",
        "invoice_no": "VEN-001-B", "issued_at": "2026-03-22T10:00Z",
        "kind": "normal", "original_id": "INV-VEN",
        "lines": [{"commitment_id": "C-VENUE", "amount": 30000}]})

    # 巴士：先开 6000，随后取消剩余服务（只影响未结算）
    send("invoice_received", "2026-03-20T10:00Z", {
        "id": "INV-BUS-1", "event_code": "MAR", "vendor": "迅达客运",
        "invoice_no": "BUS-001", "issued_at": "2026-03-20T10:00Z",
        "lines": [{"commitment_id": "C-BUS", "amount": 6000}]})
    send("cancellation", "2026-03-21T18:00Z", {
        "commitment_id": "C-BUS", "reason": "4 辆线路因封路取消"})

    # 志愿者
    send("invoice_received", "2026-03-20T10:00Z", {
        "id": "INV-VOL", "event_code": "MAR", "vendor": "志愿者协会",
        "invoice_no": "VOL-001", "issued_at": "2026-03-20T10:00Z",
        "lines": [{"commitment_id": "C-VOL", "amount": 10000}]})

    # 赞助商按调整后金额开票
    send("invoice_received", "2026-03-22T10:00Z", {
        "id": "INV-SPO", "event_code": "MAR", "vendor": "星河银行",
        "invoice_no": "SPO-001", "issued_at": "2026-03-22T10:00Z",
        "lines": [{"commitment_id": "C-SPO", "amount": 40000}]})

    # 计时芯片补开发票 52000，超出 OPS 预算 60000
    send("invoice_received", "2026-03-22T15:00Z", {
        "id": "INV-CHIP", "event_code": "MAR", "vendor": "精准计时",
        "invoice_no": "CHIP-001", "issued_at": "2026-03-22T15:00Z",
        "lines": [{"commitment_id": "C-CHIP", "amount": 52000}]})

    return e


def center(snap: dict, code: str) -> dict:
    return next(c for c in snap["cost_centers"] if c["cost_center"] == code)


def commitment(snap: dict, cid: str) -> dict:
    return next(c for c in snap["commitments"] if c["id"] == cid)


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.e = build_scenario()

    def test_quantities(self) -> None:
        snap = self.e.snapshot("MAR", parse_time("2026-04-01T00:00Z"))
        self.assertEqual(commitment(snap, "C-REG")["actual_qty"], 920)
        # 退赛超过报名被拒绝
        with self.assertRaises(Conflict):
            self.e.handle(cmd("withdrawal", "2026-03-11T10:00Z",
                              {"id": "W2", "commitment_id": "C-REG", "delta": 99999}))

    def test_pre_race_accrual_is_zero_post_race_accrues(self) -> None:
        before = self.e.snapshot("MAR", parse_time("2026-03-14T23:59Z"))
        # 服务日前支出类不确认挣得、不预提
        self.assertEqual(commitment(before, "C-VENUE")["earned"], 0)
        self.assertEqual(commitment(before, "C-VENUE")["accrued"], 0)
        self.assertEqual(commitment(before, "C-VENUE")["committed"], 80000)
        # 收入随报名即时确认
        self.assertEqual(commitment(before, "C-REG")["earned"], 184000)
        # 赛后、发票前：按完成量预提
        after = self.e.snapshot("MAR", parse_time("2026-03-19T23:59Z"))
        self.assertEqual(commitment(after, "C-MED")["earned"], 9200)
        self.assertEqual(commitment(after, "C-MED")["accrued"], 9200)
        self.assertEqual(commitment(after, "C-VENUE")["accrued"], 80000)

    def test_late_invoice_arrival_projection(self) -> None:
        # 医疗票 03-20 开具、03-25 才到：03-24 快照仍是预提
        s = self.e.snapshot("MAR", parse_time("2026-03-24T00:00Z"))
        self.assertEqual(commitment(s, "C-MED")["invoiced"], 0)
        self.assertEqual(commitment(s, "C-MED")["accrued"], 9200)
        s2 = self.e.snapshot("MAR", parse_time("2026-03-25T12:00Z"))
        self.assertEqual(commitment(s2, "C-MED")["invoiced"], 9200)

    def test_red_and_reissue_net_to_reissued(self) -> None:
        s = self.e.snapshot("MAR", parse_time("2026-04-01T00:00Z"))
        self.assertEqual(commitment(s, "C-MED")["invoiced"], 8800)
        # 台账中三张票关联原单，旧票与红字有效系数为 0
        from settlement.engine import invoice_register
        book = {i["id"]: i for i in invoice_register(self.e.state, "MAR")}
        self.assertEqual(book["INV-MED"]["effective_amount"], 0)
        self.assertEqual(book["INV-MED-R"]["effective_amount"], 0)
        self.assertEqual(book["INV-MED-2"]["effective_amount"], 8800)
        self.assertEqual(book["INV-MED-2"]["replaces_id"], "INV-MED")
        # 被替代的原票不得付款
        with self.assertRaises(Conflict):
            self.e.handle(cmd("payment_received", "2026-04-02T10:00Z", {
                "id": "P-BAD", "invoice_id": "INV-MED", "receipt_no": "RX",
                "amount": 100}))
        # 红字必须为负
        with self.assertRaises(EngineError):
            self.e.handle(cmd("invoice_received", "2026-04-03T10:00Z", {
                "id": "INV-X", "event_code": "MAR", "vendor": "安康医疗",
                "invoice_no": "X", "kind": "red",
                "original_id": "INV-MED-2",
                "lines": [{"commitment_id": "C-MED", "amount": 10}]}))

    def test_split_invoices_and_payment_caps(self) -> None:
        from settlement.engine import invoice_register
        book = {i["id"]: i for i in invoice_register(self.e.state, "MAR")}
        # 拆满后原票可付额度为 0
        self.assertEqual(book["INV-VEN"]["effective_amount"], 0)
        self.assertEqual(book["INV-VEN-A"]["effective_amount"], 50000)
        with self.assertRaises(Conflict):
            self.e.handle(cmd("payment_received", "2026-04-01T10:00Z", {
                "id": "P-PARENT", "invoice_id": "INV-VEN",
                "receipt_no": "R-PARENT", "amount": 100}))
        # 超额支付被拒绝
        with self.assertRaises(Conflict):
            self.e.handle(cmd("payment_received", "2026-04-01T10:00Z", {
                "id": "P-OVER", "invoice_id": "INV-VEN-A",
                "receipt_no": "R-OVER", "amount": 50001}))

    def test_duplicate_receipt_never_pays_twice(self) -> None:
        self.e.handle(cmd("payment_received", "2026-04-01T10:00Z", {
            "id": "P-V1", "invoice_id": "INV-VEN-A",
            "receipt_no": "R-VEN-1", "amount": 50000},
            received="2026-04-02T08:00Z"))
        # 同一回执号再次提交（乱序重放）→ 拒绝
        with self.assertRaises(DuplicateReceipt):
            self.e.handle(cmd("payment_received", "2026-04-11T10:00Z", {
                "id": "P-V1-DUP", "invoice_id": "INV-VEN-A",
                "receipt_no": "R-VEN-1", "amount": 50000}))
        # B 票支付业务时间 04-05，但回执 04-10 才到（迟到）
        self.e.handle(cmd("payment_received", "2026-04-05T10:00Z", {
            "id": "P-V2", "invoice_id": "INV-VEN-B",
            "receipt_no": "R-VEN-2", "amount": 30000},
            received="2026-04-10T08:00Z"))
        # 04-03 重建：V1 已到账，V2 回执尚未到达 → 仍未付
        s = self.e.snapshot("MAR", parse_time("2026-04-03T00:00Z"))
        self.assertEqual(commitment(s, "C-VENUE")["paid"], 50000)
        # 04-11 重建：两张都到账
        s2 = self.e.snapshot("MAR", parse_time("2026-04-11T00:00Z"))
        self.assertEqual(commitment(s2, "C-VENUE")["paid"], 80000)
        # 但按业务时间 04-04 重建账面：V2 支付尚未发生
        s3 = self.e.snapshot("MAR", parse_time("2026-04-04T00:00Z"))
        self.assertEqual(commitment(s3, "C-VENUE")["paid"], 50000)

    def test_cancellation_only_affects_unsettled(self) -> None:
        # T 恤赛前取消：承诺清零、全额释放，取消后发票拒收
        s = self.e.snapshot("MAR", parse_time("2026-04-01T00:00Z"))
        tee = commitment(s, "C-TEE")
        self.assertEqual(tee["committed"], 0)
        self.assertEqual(tee["released"], 30000)
        self.assertEqual(tee["status"], "canceled")
        with self.assertRaises(Conflict):
            self.e.handle(cmd("invoice_received", "2026-03-22T10:00Z", {
                "id": "INV-TEE", "event_code": "MAR", "vendor": "速印制衣",
                "invoice_no": "TEE-001", "issued_at": "2026-03-22T10:00Z",
                "lines": [{"commitment_id": "C-TEE", "amount": 30000}]}))
        # 巴士取消时已挣得 10000、已开 6000：冻结 10000
        bus = commitment(s, "C-BUS")
        self.assertEqual(bus["committed"], 10000)
        self.assertEqual(bus["invoiced"], 6000)
        # 超过冻结额度（还差 4000 可结算）的发票拒收
        with self.assertRaises(Conflict):
            self.e.handle(cmd("invoice_received", "2026-03-23T10:00Z", {
                "id": "INV-BUS-X", "event_code": "MAR", "vendor": "迅达客运",
                "invoice_no": "BUS-002", "issued_at": "2026-03-23T10:00Z",
                "lines": [{"commitment_id": "C-BUS", "amount": 5000}]}))
        # 额度内补开 4000 可以
        self.e.handle(cmd("invoice_received", "2026-03-23T10:00Z", {
            "id": "INV-BUS-2", "event_code": "MAR", "vendor": "迅达客运",
            "invoice_no": "BUS-002", "issued_at": "2026-03-23T10:00Z",
            "lines": [{"commitment_id": "C-BUS", "amount": 4000}]}))
        s2 = self.e.snapshot("MAR", parse_time("2026-04-01T00:00Z"))
        self.assertEqual(commitment(s2, "C-BUS")["invoiced"], 10000)

    def test_sponsor_scope_release(self) -> None:
        s = self.e.snapshot("MAR", parse_time("2026-04-01T00:00Z"))
        spo = commitment(s, "C-SPO")
        self.assertEqual(spo["committed"], 40000)
        self.assertEqual(spo["released"], 10000)
        self.assertEqual(spo["invoiced"], 40000)

    def test_variance_bridge_identity(self) -> None:
        s = self.e.snapshot("MAR", parse_time("2026-03-01T00:00Z"))
        for row in s["commitments"]:
            v = row["variance"]
            self.assertAlmostEqual(
                v["baseline"] + v["qty_variance"] + v["price_variance"]
                + v["contract_variance"], v["current"], places=2,
                msg=f"差异桥不平: {row['id']} {v}")
        chip = commitment(s, "C-CHIP")["variance"]
        # 赛前按 1000 人预测：单价 45->48 价差 3000；燃油附加 500 走合同
        self.assertEqual(chip["baseline"], 45000)
        self.assertEqual(chip["qty_variance"], 0)
        self.assertEqual(chip["price_variance"], 3000)
        self.assertEqual(chip["contract_variance"], 500)
        # 赛后按 920 人：人数差 -80*45 = -3600；价差 920*3 = 2760
        s2 = self.e.snapshot("MAR", parse_time("2026-03-16T00:00Z"))
        chip2 = commitment(s2, "C-CHIP")["variance"]
        self.assertEqual(chip2["qty_variance"], -3600)
        self.assertEqual(chip2["price_variance"], 2760)
        self.assertEqual(chip2["current"], 44660)

    def test_overspend_approval_queue_and_expiry(self) -> None:
        # 芯片发票导致 OPS：8800(医疗重开) + 52000 = 60800 > 60000
        s = self.e.snapshot("MAR", parse_time("2026-04-01T00:00Z"))
        ops = center(s, "OPS")
        self.assertTrue(ops["over_budget"])
        auto = [a for a in s["approval_queue"] if a["cost_center"] == "OPS"]
        self.assertTrue(auto)
        apr = auto[0]
        self.assertEqual(apr["status"], "expired")
        self.assertGreaterEqual(apr["amount"], 800)
        # 截止时间 = 发起 +72h（OPS 中心配置）
        self.assertEqual(apr["deadline"], "2026-03-25T15:00:00+00:00")
        # 未批准：付款被拦
        with self.assertRaises(ApprovalRequired):
            self.e.handle(cmd("payment_received", "2026-04-02T10:00Z", {
                "id": "P-MED", "invoice_id": "INV-MED-2",
                "receipt_no": "R-MED", "amount": 8800}))
        # 逾期才批准 -> 审批过期
        self.e.handle(cmd("approval_decided", "2026-03-26T09:00Z", {
            "id": apr["id"], "approved": True, "approver": "CFO"}))
        with self.assertRaises(ApprovalExpired):
            self.e.handle(cmd("payment_received", "2026-04-02T10:00Z", {
                "id": "P-MED", "invoice_id": "INV-MED-2",
                "receipt_no": "R-MED", "amount": 8800}))
        # 重新发起并在窗口内批准
        self.e.handle(cmd("approval_requested", "2026-04-02T08:00Z", {
            "id": "APR-MANUAL", "event_code": "MAR", "cost_center": "OPS",
            "commitment_id": "C-CHIP", "amount": 1000,
            "reason": "补开发票超支补批"}))
        self.e.handle(cmd("approval_decided", "2026-04-02T09:00Z", {
            "id": "APR-MANUAL", "approved": True, "approver": "CFO"}))
        # 放行：医疗与芯片均可支付
        self.e.handle(cmd("payment_received", "2026-04-02T10:00Z", {
            "id": "P-MED", "invoice_id": "INV-MED-2",
            "receipt_no": "R-MED", "amount": 8800}))
        self.e.handle(cmd("payment_received", "2026-04-03T10:00Z", {
            "id": "P-CHIP", "invoice_id": "INV-CHIP",
            "receipt_no": "R-CHIP", "amount": 52000}))
        s2 = self.e.snapshot("MAR", parse_time("2026-04-04T00:00Z"))
        self.assertEqual(commitment(s2, "C-MED")["paid"], 8800)
        self.assertEqual(commitment(s2, "C-CHIP")["paid"], 52000)

    def test_sweep_expired(self) -> None:
        swept = self.e.sweep_expired(parse_time("2026-04-01T00:00Z"))
        ids = {x.payload["id"] for x in swept}
        snap = self.e.snapshot("MAR", parse_time("2026-04-01T00:00Z"))
        auto = [a for a in snap["approval_queue"] if a["status"] == "expired"]
        self.assertTrue(auto)
        self.assertIn(auto[0]["id"], ids)

    def test_center_snapshot_totals(self) -> None:
        s = self.e.snapshot("MAR", parse_time("2026-04-01T00:00Z"))
        ven = center(s, "VEN")
        self.assertEqual(ven["committed"], 90000)  # 场地 80000 + 巴士冻结 10000
        vol = center(s, "VOL")
        self.assertEqual(vol["committed"], 10000)
        swag = center(s, "SWAG")
        self.assertEqual(swag["committed"], 0)
        self.assertEqual(swag["released"], 30000)

    def test_unknown_entities(self) -> None:
        with self.assertRaises(NotFound):
            self.e.snapshot("NOPE")
        with self.assertRaises(EngineError):
            self.e.handle({"type": "bogus", "payload": {}})


class PersistenceTest(unittest.TestCase):
    def test_replay_matches(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = EventStore(Path(d) / "events.jsonl")
            # 只喂用户命令；审批入队/过期是副作用，重放会重新生成
            skip = {"approval_expired", "approval_requested"}
            for ev in build_scenario().state.events:
                if ev.type in skip:
                    continue
                rec = {
                    "type": ev.type,
                    "occurred_at": ev.occurred_at.isoformat(),
                    "received_at": ev.received_at.isoformat(),
                    "payload": ev.payload,
                }
                store.handle(rec)
            a = store.engine.snapshot("MAR", parse_time("2026-04-01T00:00Z"))
            store2 = EventStore(Path(d) / "events.jsonl")
            b = store2.engine.snapshot("MAR", parse_time("2026-04-01T00:00Z"))
            self.assertEqual(a["cost_centers"], b["cost_centers"])
            self.assertEqual(a["commitments"], b["commitments"])


if __name__ == "__main__":
    unittest.main()
