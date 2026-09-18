"""结算引擎：命令校验 + 事件追加 + 纯函数投影。

所有状态变化都先校验再追加为不可变事件（业务时间 occurred_at 与
接收时间 received_at 分离），查询均为"截至某业务时间"的投影，
因此发票/回执乱序到达不影响账面，重复回执永不付款。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from .errors import (
    ApprovalExpired,
    ApprovalRequired,
    Conflict,
    DuplicateReceipt,
    EngineError,
    NotFound,
)
from .money import D, iso, money, now_iso, parse_time, qty, ZERO
from .models import (
    Approval,
    Commitment,
    ContractVersion,
    CostCenter,
    Event,
    Invoice,
    InvoiceLine,
    Payment,
    QtyRecord,
    ScopeAdjust,
    StoredEvent,
)

VALID_EVENTS = {
    "event_created",
    "cost_center_created",
    "commitment_created",
    "registration",
    "withdrawal",
    "contract_versioned",
    "scope_adjusted",
    "cancellation",
    "commitment_closed",
    "invoice_received",
    "payment_received",
    "approval_requested",
    "approval_decided",
    "approval_expired",
}


class State:
    def __init__(self) -> None:
        self.seq = 0
        self.events: list[StoredEvent] = []
        self.events_by_code: dict[str, Event] = {}
        self.centers: dict[str, CostCenter] = {}
        self.commitments: dict[str, Commitment] = {}
        self.invoices: dict[str, Invoice] = {}
        self.payments: dict[str, Payment] = {}
        self.approvals: dict[str, Approval] = {}
        self.receipts: set[str] = set()
        self.invoice_keys: set[tuple[str, str]] = set()


class SettlementEngine:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.state = State()
        self._lock = threading.RLock()
        self._clock = clock or (lambda: parse_time(now_iso()))
        # 重放期间为 True：审批入队等副作用已在事件日志中，不再重复生成
        self._loading = False

    @classmethod
    def replay(cls, records: list[dict[str, Any]]) -> "SettlementEngine":
        """从序列化事件重建引擎（不做命令校验，直接应用）。"""
        eng = cls()
        eng._loading = True
        try:
            for rec in records:
                se = StoredEvent(
                    seq=rec["seq"],
                    type=rec["type"],
                    occurred_at=parse_time(rec["occurred_at"]),
                    received_at=parse_time(rec["received_at"]),
                    payload=rec["payload"],
                )
                if se.seq > eng.state.seq:
                    eng.state.seq = se.seq
                eng.state.events.append(se)
                eng._apply(se)
        finally:
            eng._loading = False
        return eng

    def export_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "seq": e.seq,
                    "type": e.type,
                    "occurred_at": iso(e.occurred_at),
                    "received_at": iso(e.received_at),
                    "payload": e.payload,
                }
                for e in self.state.events
            ]


    # ------------------------------------------------------------------ 命令

    def handle(self, command: dict[str, Any]) -> list[StoredEvent]:
        """处理一条命令，返回追加的事件（可能附带系统事件）。"""
        with self._lock:
            received = parse_time(command.get("received_at") or self._clock())
            new_events = self._validate(command, received)
            stored: list[StoredEvent] = []
            for occurred_at, etype, payload in new_events:
                before = len(self.state.events)
                self.state.seq += 1
                se = StoredEvent(self.state.seq, etype, occurred_at, received, payload)
                self.state.events.append(se)
                self._apply(se)
                # _apply 可能附带系统事件（如超支自动入审批队列）
                stored.extend(self.state.events[before:])
            return stored

    def _validate(
        self, command: dict[str, Any], received: datetime
    ) -> list[tuple[datetime, str, dict[str, Any]]]:
        etype = command.get("type")
        if etype not in VALID_EVENTS:
            raise EngineError(f"未知事件类型: {etype!r}")
        p = command.get("payload") or {}
        occurred = parse_time(
            command.get("occurred_at") or p.get("at") or received
        )
        handler = getattr(self, f"_cmd_{etype}")
        result = handler(p, occurred, received)
        if result is None:
            result = [(occurred, etype, p)]
        return result

    # ------------------------------------------------------------- 基础档案

    def _cmd_event_created(self, p: dict, at: datetime, rcv: datetime):
        code = p["code"]
        if code in self.state.events_by_code:
            raise Conflict(f"赛事已存在: {code}")
        return None

    def _cmd_cost_center_created(self, p: dict, at: datetime, rcv: datetime):
        code = p["code"]
        if code in self.state.centers:
            raise Conflict(f"成本中心已存在: {code}")
        return None

    def _cmd_commitment_created(self, p: dict, at: datetime, rcv: datetime):
        cid = p["id"]
        if cid in self.state.commitments:
            raise Conflict(f"承诺已存在: {cid}")
        ev = self._event(p["event_code"])
        if p["cost_center"] not in self.state.centers:
            raise NotFound(f"成本中心不存在: {p['cost_center']}")
        price = money(p.get("unit_price", 0))
        fixed = money(p.get("fixed", 0))
        if price < 0 or fixed < 0:
            raise EngineError("单价与固定金额不能为负")
        payload = dict(p)
        payload.setdefault("kind", "contract")
        payload.setdefault("title", cid)
        payload["unit_price"] = str(price)
        payload["fixed"] = str(fixed)
        payload["planned_qty"] = str(qty(p.get("planned_qty", 0)))
        if p.get("service_at"):
            payload["service_at"] = iso(parse_time(p["service_at"]))
        else:
            payload["service_at"] = iso(ev.race_date)
        return [(at, "commitment_created", payload)]

    def _commitment(self, cid: str) -> Commitment:
        c = self.state.commitments.get(cid)
        if c is None:
            raise NotFound(f"承诺不存在: {cid}")
        return c

    def _event(self, code: str) -> Event:
        ev = self.state.events_by_code.get(code)
        if ev is None:
            raise NotFound(f"赛事不存在: {code}")
        return ev

    def _cmd_registration(self, p: dict, at: datetime, rcv: datetime):
        return self._qty(p, at, D(p.get("delta", 1)), "registration")

    def _cmd_withdrawal(self, p: dict, at: datetime, rcv: datetime):
        return self._qty(p, at, -D(p.get("delta", 1)), "withdrawal")

    def _qty(self, p: dict, at: datetime, delta: Decimal, etype: str):
        c = self._commitment(p["commitment_id"])
        if c.canceled_at is not None or c.closed_at is not None:
            raise Conflict("承诺已取消/关闭，数量不再变动（只影响未结算项目）")
        if p["id"] in {r.id for r in c.qty_records}:
            raise Conflict(f"数量流水重复: {p['id']}")
        new_total = sum((r.delta for r in c.qty_records), ZERO) + delta
        if new_total < 0:
            raise Conflict("累计数量不能为负（退赛超过报名）")
        payload = {
            "id": p["id"],
            "commitment_id": c.id,
            "delta": str(qty(delta)),
        }
        return [(at, etype, payload)]

    def _cmd_contract_versioned(self, p: dict, at: datetime, rcv: datetime):
        c = self._commitment(p["commitment_id"])
        if c.canceled_at is not None or c.closed_at is not None:
            raise Conflict("承诺已取消/关闭，不能再变更合同")
        price = money(p["unit_price"])
        fixed = money(p.get("fixed", c.version_at(at).fixed))
        if price < 0 or fixed < 0:
            raise EngineError("单价与固定金额不能为负")
        payload = {
            "commitment_id": c.id,
            "version": len(c.versions) + 1,
            "unit_price": str(price),
            "fixed": str(fixed),
            "note": p.get("note", ""),
        }
        return [(at, "contract_versioned", payload)]

    def _cmd_scope_adjusted(self, p: dict, at: datetime, rcv: datetime):
        c = self._commitment(p["commitment_id"])
        if c.canceled_at is not None or c.closed_at is not None:
            raise Conflict("承诺已取消/关闭，不能再调整范围")
        payload = {
            "commitment_id": c.id,
            "delta_qty": str(qty(p.get("delta_qty", 0))),
            "delta_fixed": str(money(p.get("delta_fixed", 0))),
            "reason": p.get("reason", ""),
        }
        return [(at, "scope_adjusted", payload)]

    def _cmd_cancellation(self, p: dict, at: datetime, rcv: datetime):
        c = self._commitment(p["commitment_id"])
        if c.canceled_at is not None:
            raise Conflict("承诺已取消")
        # 冻结取消时点的已完成与已结算价值：已挣得/已开票/已付（已结算）
        # 均不受影响，未结算部分随取消释放。
        earned = commitment_earned(self.state, c, at)
        billed = commitment_invoiced(self.state, c, at)
        paid = commitment_paid(self.state, c, at)
        frozen = max(earned, billed, paid)
        payload = {
            "commitment_id": c.id,
            "reason": p.get("reason", ""),
            "frozen_value": str(money(frozen)),
        }
        return [(at, "cancellation", payload)]

    def _cmd_commitment_closed(self, p: dict, at: datetime, rcv: datetime):
        c = self._commitment(p["commitment_id"])
        if c.closed_at is not None:
            raise Conflict("承诺已关闭")
        earned = commitment_earned(self.state, c, at)
        billed = commitment_invoiced(self.state, c, at)
        paid = commitment_paid(self.state, c, at)
        frozen = max(earned, billed, paid)
        payload = {
            "commitment_id": c.id,
            "reason": p.get("reason", ""),
            "frozen_value": str(money(frozen)),
        }
        return [(at, "commitment_closed", payload)]

    # --------------------------------------------------------------- 发票族

    def _cmd_invoice_received(self, p: dict, at: datetime, rcv: datetime):
        iid = p["id"]
        if iid in self.state.invoices:
            raise Conflict(f"发票已存在: {iid}")
        ev = self._event(p["event_code"])
        vendor = p["vendor"]
        invoice_no = p["invoice_no"]
        key = (vendor, invoice_no)
        if key in self.state.invoice_keys:
            raise Conflict(f"发票号重复: {vendor}/{invoice_no}")
        kind = p.get("kind", "normal")
        if kind not in ("normal", "red", "reissue"):
            raise EngineError("发票种类须为 normal/red/reissue")
        original_id = p.get("original_id")
        replaces_id = p.get("replaces_id")
        if kind in ("red", "reissue") and not original_id:
            raise EngineError("红字票/重开票必须通过 original_id 关联原单")
        if kind == "reissue" and not replaces_id:
            raise EngineError("重开票必须通过 replaces_id 指定被取代的发票")
        parent: Invoice | None = None
        if original_id:
            parent = self.state.invoices.get(original_id)
            if parent is None:
                raise NotFound(f"原票不存在: {original_id}")
            if parent.vendor != vendor:
                raise EngineError("红字/拆票供应商须与原票一致")
        if replaces_id:
            rep = self.state.invoices.get(replaces_id)
            if rep is None:
                raise NotFound(f"被替换发票不存在: {replaces_id}")
            issued_at = parse_time(p["issued_at"])
            if rep.issued_at > issued_at:
                raise EngineError("重开票不能早于被替换发票")
            if kind == "reissue" and rep.id != original_id:
                raise EngineError("重开票应取代原蓝字发票（先红冲再重开）")
        raw_lines = p.get("lines") or []
        if not raw_lines:
            raise EngineError("发票至少一行")
        centers: set[str] = set()
        lines: list[InvoiceLine] = []
        total = ZERO
        for ln in raw_lines:
            c = self._commitment(ln["commitment_id"])
            if c.event_code != ev.code:
                raise EngineError("发票行与发票不属于同一赛事")
            centers.add(c.cost_center)
            if len(centers) > 1:
                raise EngineError("一张发票只能属于一个成本中心")
            amount = money(ln["amount"])
            total += amount
            lines.append(
                InvoiceLine(
                    commitment_id=c.id,
                    amount=amount,
                    qty=qty(ln["qty"]) if ln.get("qty") is not None else None,
                    description=ln.get("description", ""),
                )
            )
        if kind == "red" and total >= 0:
            raise EngineError("红字发票金额必须为负")
        if kind in ("normal", "reissue") and total <= 0:
            raise EngineError("正数发票金额必须为正")
        if parent is not None and kind == "normal":
            # 拆票：累计拆票金额不得超过原票
            children = split_children(self.state, parent.id)
            have = sum((i.amount for i in children), ZERO)
            if have + total > parent.amount:
                raise EngineError("拆票累计金额超过原票")
        # 取消/关闭后，新发票只能覆盖冻结额度内的未结算部分
        for ln in lines:
            c = self.state.commitments[ln.commitment_id]
            freeze_at = c.canceled_at or c.closed_at
            issued_at = parse_time(p["issued_at"])
            if freeze_at is not None and issued_at > freeze_at:
                billed = commitment_invoiced(self.state, c, issued_at)
                cap = (c.frozen_earned or ZERO) - billed
                if ln.amount > cap + ZERO:
                    raise Conflict(
                        f"承诺已冻结，新增发票 {money(ln.amount)} 超过可结算额度 "
                        f"{money(cap)}（赛事取消只影响未结算项目）"
                    )
        payload = dict(p)
        payload["kind"] = kind
        payload["lines"] = [
            {
                "commitment_id": ln.commitment_id,
                "amount": str(ln.amount),
                **({"qty": str(ln.qty)} if ln.qty is not None else {}),
                "description": ln.description,
            }
            for ln in lines
        ]
        return [(parse_time(p["issued_at"]), "invoice_received", payload)]

    # --------------------------------------------------------------- 支付

    def _cmd_payment_received(self, p: dict, at: datetime, rcv: datetime):
        pid = p["id"]
        if pid in self.state.payments:
            raise Conflict(f"支付已存在: {pid}")
        receipt = p["receipt_no"]
        # 回执编号全局唯一：乱序/重放到达都不会重复付款
        if receipt in self.state.receipts:
            raise DuplicateReceipt(f"支付回执重复: {receipt}")
        inv = self.state.invoices.get(p["invoice_id"])
        if inv is None:
            raise NotFound(f"发票不存在: {p['invoice_id']}")
        amount = money(p["amount"])
        if amount <= 0:
            raise EngineError("支付金额必须为正")
        paid_at = parse_time(p.get("paid_at") or at)
        if paid_at < inv.issued_at:
            raise EngineError("支付时间早于开票时间")
        if invoice_is_replaced(self.state, inv, paid_at):
            raise Conflict("发票已被重开/红冲替代，不能对其付款")
        # 拆票后原票仅剩余部分可付；可付额度按截至支付时点的有效系数
        effective = money(inv.amount * invoice_factor(self.state, inv, paid_at))
        prior = invoice_paid(self.state, inv, paid_at)
        if prior + amount > effective:
            raise Conflict(
                f"支付超额：已付 {money(prior)} + {money(amount)} > "
                f"可付额度 {money(effective)}（发票额 {money(inv.amount)}）"
            )
        # 超支审批：成本中心预测支出超预算时，须有窗口内的有效批准
        center_code = inv.lines[0].commitment_id and self.state.commitments[
            inv.lines[0].commitment_id
        ].cost_center
        excess = center_excess(self.state, inv.event_code, center_code, paid_at)
        if excess > ZERO:
            ok = approval_cover(self.state, inv.event_code, center_code, paid_at)
            if ok < excess:
                which = nearest_approval_gap(
                    self.state, inv.event_code, center_code, paid_at
                )
                if which == "expired":
                    raise ApprovalExpired("超支审批已过截止时间，请重新发起")
                raise ApprovalRequired(
                    f"成本中心 {center_code} 超支 {money(excess)}，"
                    "需在审批截止前取得批准"
                )
        payload = dict(p)
        payload["event_code"] = inv.event_code
        payload["amount"] = str(amount)
        return [(paid_at, "payment_received", payload)]

    # --------------------------------------------------------------- 审批

    def _cmd_approval_requested(self, p: dict, at: datetime, rcv: datetime):
        aid = p["id"]
        if aid in self.state.approvals:
            raise Conflict(f"审批已存在: {aid}")
        self._event(p["event_code"])
        center = self.state.centers.get(p["cost_center"])
        if center is None:
            raise NotFound(f"成本中心不存在: {p['cost_center']}")
        if center.direction != 1:
            raise EngineError("收入中心无需超支审批")
        amount = money(p["amount"])
        if amount <= 0:
            raise EngineError("审批额度必须为正")
        payload = dict(p)
        payload["amount"] = str(amount)
        return [(at, "approval_requested", payload)]

    def _cmd_approval_decided(self, p: dict, at: datetime, rcv: datetime):
        a = self.state.approvals.get(p["id"])
        if a is None:
            raise NotFound(f"审批不存在: {p['id']}")
        if a.status != "pending":
            raise Conflict(f"审批已结束: {a.status}")
        approved = bool(p.get("approved", True))
        deadline = a.requested_at + timedelta(
            hours=self.state.centers[a.cost_center].approval_hours
        )
        if at > deadline:
            # 逾期决定无效：登记为拒绝，并在截止时点补记系统过期事件
            return [
                (
                    at,
                    "approval_decided",
                    {
                        "id": a.id,
                        "approver": p.get("approver", ""),
                        "approved": False,
                        "late": True,
                        "note": "逾期决定，审批已截止",
                    },
                ),
                (deadline, "approval_expired", {"id": a.id}),
            ]
        payload = {
            "id": a.id,
            "approver": p.get("approver", ""),
            "approved": approved,
            "note": p.get("note", ""),
        }
        return [(at, "approval_decided", payload)]

    # ------------------------------------------------------------------ 应用

    def _apply(self, se: StoredEvent) -> None:
        p = se.payload
        s = self.state
        if se.type == "event_created":
            s.events_by_code[p["code"]] = Event(
                code=p["code"],
                name=p.get("name", p["code"]),
                race_date=parse_time(p["race_date"]),
                currency=p.get("currency", "CNY"),
            )
        elif se.type == "cost_center_created":
            s.centers[p["code"]] = CostCenter(
                code=p["code"],
                name=p.get("name", p["code"]),
                direction=int(p.get("direction", 1)),
                approval_hours=int(p.get("approval_hours", 48)),
                budget=money(p.get("budget", 0)),
            )
        elif se.type == "commitment_created":
            c = Commitment(
                id=p["id"],
                event_code=p["event_code"],
                cost_center=p["cost_center"],
                kind=p.get("kind", "contract"),
                title=p.get("title", p["id"]),
                vendor=p.get("vendor"),
                planned_qty=qty(p["planned_qty"]),
                service_at=parse_time(p["service_at"]),
            )
            c.versions.append(
                ContractVersion(
                    version=1,
                    effective_at=se.occurred_at,
                    unit_price=D(p["unit_price"]),
                    fixed=D(p["fixed"]),
                    note="初始合同",
                )
            )
            s.commitments[c.id] = c
        elif se.type in ("registration", "withdrawal"):
            c = s.commitments[p["commitment_id"]]
            c.qty_records.append(
                QtyRecord(
                    id=p["id"], at=se.occurred_at, commitment_id=c.id, delta=D(p["delta"])
                )
            )
        elif se.type == "contract_versioned":
            c = s.commitments[p["commitment_id"]]
            c.versions.append(
                ContractVersion(
                    version=p["version"],
                    effective_at=se.occurred_at,
                    unit_price=D(p["unit_price"]),
                    fixed=D(p.get("fixed", c.versions[-1].fixed)),
                    note=p.get("note", ""),
                )
            )
        elif se.type == "scope_adjusted":
            c = s.commitments[p["commitment_id"]]
            c.adjusts.append(
                ScopeAdjust(
                    at=se.occurred_at,
                    delta_qty=D(p["delta_qty"]),
                    delta_fixed=D(p["delta_fixed"]),
                    reason=p.get("reason", ""),
                )
            )
        elif se.type in ("cancellation", "commitment_closed"):
            c = s.commitments[p["commitment_id"]]
            c.cancel_reason = p.get("reason", "")
            c.frozen_earned = D(p["frozen_value"])
            if se.type == "cancellation":
                c.canceled_at = se.occurred_at
            else:
                c.closed_at = se.occurred_at
        elif se.type == "invoice_received":
            inv = Invoice(
                id=p["id"],
                event_code=p["event_code"],
                vendor=p["vendor"],
                invoice_no=p["invoice_no"],
                issued_at=se.occurred_at,
                received_at=se.received_at,
                lines=[
                    InvoiceLine(
                        commitment_id=ln["commitment_id"],
                        amount=D(ln["amount"]),
                        qty=D(ln["qty"]) if ln.get("qty") else None,
                        description=ln.get("description", ""),
                    )
                    for ln in p["lines"]
                ],
                kind=p.get("kind", "normal"),
                original_id=p.get("original_id"),
                replaces_id=p.get("replaces_id"),
                note=p.get("note", ""),
            )
            s.invoices[inv.id] = inv
            s.invoice_keys.add((inv.vendor, inv.invoice_no))
            # 入账后若造成成本中心超预算，自动进入带截止时间的审批队列
            self._maybe_enqueue_approval(inv)
        elif se.type == "payment_received":
            inv = s.invoices[p["invoice_id"]]
            pay = Payment(
                id=p["id"],
                event_code=inv.event_code,
                vendor=inv.vendor,
                receipt_no=p["receipt_no"],
                invoice_id=inv.id,
                amount=D(p["amount"]),
                paid_at=se.occurred_at,
                received_at=se.received_at,
            )
            s.payments[pay.id] = pay
            s.receipts.add(pay.receipt_no)
        elif se.type == "approval_requested":
            a = Approval(
                id=p["id"],
                event_code=p["event_code"],
                cost_center=p["cost_center"],
                commitment_id=p.get("commitment_id", ""),
                amount=D(p["amount"]),
                requested_at=se.occurred_at,
                reason=p.get("reason", ""),
            )
            s.approvals[a.id] = a
        elif se.type == "approval_decided":
            a = s.approvals[p["id"]]
            if p.get("approved", True):
                a.status = "approved"
                a.decided_at = se.occurred_at
            elif p.get("late"):
                a.status = "expired"
                a.decided_at = se.occurred_at
            else:
                a.status = "rejected"
                a.decided_at = se.occurred_at
            a.approver = p.get("approver", "")
            a.note = p.get("note", "")
        elif se.type == "approval_expired":
            a = s.approvals[p["id"]]
            if a.status == "pending":
                a.status = "expired"

    def _maybe_enqueue_approval(self, inv: Invoice) -> None:
        if self._loading:
            return
        center_code = self.state.commitments[inv.lines[0].commitment_id].cost_center
        at = inv.issued_at
        excess = center_excess(self.state, inv.event_code, center_code, at)
        if excess <= ZERO:
            return
        # 已被窗口内批准覆盖则不入队
        if approval_cover(self.state, inv.event_code, center_code, at) >= excess:
            return
        # 同批次未决申请合并：沿用最早一条 pending（避免拆票刷队列）
        for a in self.state.approvals.values():
            if (
                a.event_code == inv.event_code
                and a.cost_center == center_code
                and a.status == "pending"
            ):
                if excess > a.amount:
                    a.amount = excess
                return
        self.state.seq += 1
        aid = f"APR-{self.state.seq:06d}"
        se = StoredEvent(
            self.state.seq,
            "approval_requested",
            at,
            inv.received_at,
            {
                "id": aid,
                "event_code": inv.event_code,
                "cost_center": center_code,
                "commitment_id": inv.lines[0].commitment_id,
                "amount": str(money(excess)),
                "reason": f"发票 {inv.invoice_no} 导致超预算",
            },
        )
        self.state.events.append(se)
        self._apply(se)

    # ------------------------------------------------------------------ 查询

    def sweep_expired(self, at: datetime | None = None) -> list[StoredEvent]:
        """把超过审批时限仍未决定的申请标记过期。"""
        at = at or self._clock()
        out: list[StoredEvent] = []
        with self._lock:
            for a in list(self.state.approvals.values()):
                if a.status != "pending":
                    continue
                deadline = a.requested_at + timedelta(
                    hours=self.state.centers[a.cost_center].approval_hours
                )
                if at >= deadline:
                    self.state.seq += 1
                    se = StoredEvent(
                        self.state.seq,
                        "approval_expired",
                        deadline,
                        at,
                        {"id": a.id},
                    )
                    self.state.events.append(se)
                    self._apply(se)
                    out.append(se)
        return out

    def snapshot(self, event_code: str, at: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            ev = self._event(event_code)
            at = at or self._clock()
            rows = []
            for c in self.state.commitments.values():
                if c.event_code != event_code:
                    continue
                # 重建业务时点 as_of 当时的认知：该时点之后才收到的
                # 发票/回执不出现在快照中（仍表现为预提/未付）。
                rows.append(commitment_view(self.state, c, at, known_at=at))
            centers: dict[str, dict[str, Any]] = {}
            for r in rows:
                cc = r["cost_center"]
                bucket = centers.setdefault(
                    cc,
                    {
                        "cost_center": cc,
                        "direction": self.state.centers[cc].direction,
                        "budget": ZERO,
                        "committed": ZERO,
                        "earned": ZERO,
                        "invoiced": ZERO,
                        "accrued": ZERO,
                        "paid": ZERO,
                        "released": ZERO,
                    },
                )
                for k in ("committed", "earned", "invoiced", "accrued", "paid", "released"):
                    bucket[k] += D(r[k])
            for code, bucket in centers.items():
                center = self.state.centers[code]
                bucket["budget"] = center.budget
                projected = max(bucket["committed"], bucket["invoiced"])
                if center.direction == 1:
                    bucket["variance_vs_budget"] = projected - center.budget
                    bucket["over_budget"] = bucket["variance_vs_budget"] > ZERO
                    bucket["cash_remaining"] = center.budget - bucket["paid"]
                else:
                    bucket["variance_vs_budget"] = center.budget - projected
                    bucket["over_budget"] = False
                    bucket["cash_remaining"] = bucket["paid"] - ZERO
            queue = approval_queue(self.state, event_code, at)
            return {
                "event": event_code,
                "event_name": ev.name,
                "as_of": iso(at),
                "currency": ev.currency,
                "cost_centers": [
                    {k: (float(v) if isinstance(v, Decimal) else v) for k, v in b.items()}
                    for b in sorted(centers.values(), key=lambda x: x["cost_center"])
                ],
                "commitments": rows,
                "approval_queue": queue,
            }


# ============================================================== 纯函数投影


def _version(c: Commitment, at: datetime) -> ContractVersion:
    return c.version_at(at)


def actual_qty(s: State, c: Commitment, at: datetime) -> Decimal:
    return sum((r.delta for r in c.qty_records if r.at <= at), ZERO)


def scope_qty(c: Commitment, at: datetime) -> Decimal:
    return sum((a.delta_qty for a in c.adjusts if a.at <= at), ZERO)


def scope_fixed(c: Commitment, at: datetime) -> Decimal:
    return sum((a.delta_fixed for a in c.adjusts if a.at <= at), ZERO)


def is_frozen(c: Commitment, at: datetime) -> bool:
    return c.frozen_earned is not None and (
        (c.canceled_at is not None and c.canceled_at <= at)
        or (c.closed_at is not None and c.closed_at <= at)
    )


def committed_raw(s: State, c: Commitment, at: datetime) -> Decimal:
    """当前有效承诺（版本单价 × (计划量+范围量) + 固定 + 范围固定）。"""
    v = _version(c, at)
    n = c.planned_qty + scope_qty(c, at)
    return money(v.fixed + scope_fixed(c, at) + v.unit_price * n)


def committed(s: State, c: Commitment, at: datetime) -> Decimal:
    if is_frozen(c, at):
        return money(c.frozen_earned or ZERO)
    return committed_raw(s, c, at)


def earned(s: State, c: Commitment, at: datetime) -> Decimal:
    if is_frozen(c, at):
        return money(c.frozen_earned or ZERO)
    v = _version(c, at)
    direction = s.centers[c.cost_center].direction
    # 支出类供应商在服务日（赛事日）交付，此前只有承诺；
    # 收入类（报名费/赞助）随数量流水即时确认。
    delivered = direction != 1 or (c.service_at is not None and c.service_at <= at)
    if not delivered:
        return ZERO
    variable = v.unit_price * actual_qty(s, c, at)
    fixed = v.fixed + scope_fixed(c, at)
    return money(variable + fixed)


commitment_earned = earned


def invoice_factor(
    s: State, inv: Invoice, at: datetime, known_at: datetime | None = None
) -> Decimal:
    """发票在截至 at 的有效系数。

    known_at 为认知截止（快照重建时点）：接收时间晚于 known_at 的关联
    发票视为尚不知道；命令校验传 None 表示全知。
    - 被重开票取代的原票：0
    - 已被重开承接的红字票：0（红冲+重开成对，净额只留重开票）
    - 未重开的红字票（单独红冲/冲减）：1，以负金额冲减
    - 拆票父票：按未拆出比例有效；拆票/重开票本身：1
    """
    if inv.issued_at > at:
        return ZERO
    if known_at is not None and inv.received_at > known_at:
        return ZERO
    if inv.replaces_id:
        return Decimal("1")
    for other in s.invoices.values():
        if other.issued_at > at:
            continue
        if known_at is not None and other.received_at > known_at:
            continue
        if other.replaces_id == inv.id:
            return ZERO
        # 红字票已被同原单的重开票承接
        if (
            inv.kind == "red"
            and other.kind == "reissue"
            and other.original_id == inv.original_id
        ):
            return ZERO
    # 拆票：父票按剩余比例有效（仅统计认知时点已知的拆票）
    children = split_children(s, inv.id)
    kids = [
        i
        for i in children
        if i.issued_at <= at and (known_at is None or i.received_at <= known_at)
    ]
    if kids:
        split_sum = sum((i.amount for i in kids), ZERO)
        remain = max(ZERO, inv.amount - split_sum)
        return remain / inv.amount if inv.amount else ZERO
    return Decimal("1")


def split_children(s: State, parent_id: str) -> list[Invoice]:
    return [
        i
        for i in s.invoices.values()
        if i.original_id == parent_id and i.kind == "normal" and i.replaces_id is None
    ]


def invoice_is_replaced(s: State, inv: Invoice, at: datetime) -> bool:
    return invoice_factor(s, inv, at) == ZERO


def invoiced_for(
    s: State,
    c: Commitment,
    at: datetime,
    known_at: datetime | None = None,
) -> Decimal:
    total = ZERO
    for inv in s.invoices.values():
        if inv.event_code != c.event_code or inv.issued_at > at:
            continue
        if known_at is not None and inv.received_at > known_at:
            continue
        factor = invoice_factor(s, inv, at, known_at)
        for ln in inv.lines:
            if ln.commitment_id == c.id:
                total += ln.amount * factor
    return money(total)


commitment_invoiced = invoiced_for


def payments_for_invoice(
    s: State,
    inv: Invoice,
    at: datetime,
    known_at: datetime | None = None,
) -> list[Payment]:
    return [
        p
        for p in s.payments.values()
        if p.invoice_id == inv.id
        and p.paid_at <= at
        and (known_at is None or p.received_at <= known_at)
    ]


def invoice_paid(
    s: State,
    inv: Invoice,
    at: datetime,
    known_at: datetime | None = None,
) -> Decimal:
    return money(
        sum((p.amount for p in payments_for_invoice(s, inv, at, known_at)), ZERO)
    )


def paid_for(
    s: State,
    c: Commitment,
    at: datetime,
    known_at: datetime | None = None,
) -> Decimal:
    total = ZERO
    for inv in s.invoices.values():
        if inv.event_code != c.event_code or inv.issued_at > at:
            continue
        if known_at is not None and inv.received_at > known_at:
            continue
        lines = [ln for ln in inv.lines if ln.commitment_id == c.id]
        if not lines or inv.amount == 0:
            continue
        share = sum((ln.amount for ln in lines), ZERO) / inv.amount
        total += invoice_paid(s, inv, at, known_at) * share
    return money(total)


commitment_paid = paid_for


def released(s: State, c: Commitment, at: datetime) -> Decimal:
    """累计释放：范围调减按当时单价计价 + 取消/关闭时未结算部分。"""
    total = ZERO
    for adj in c.adjusts:
        if adj.at > at:
            continue
        v = c.version_at(adj.at)
        cut = -(adj.delta_qty * v.unit_price + adj.delta_fixed)
        if cut > 0:
            total += cut
    freeze_at = c.canceled_at or c.closed_at
    if freeze_at is not None and freeze_at <= at:
        before = committed_raw(s, c, freeze_at)
        total += max(ZERO, before - (c.frozen_earned or ZERO))
    return money(total)


def accrued(
    s: State,
    c: Commitment,
    at: datetime,
    known_at: datetime | None = None,
) -> Decimal:
    """预提 = 已完成尚未开票部分（认知时点未知的发票仍表现为预提）。"""
    return money(max(ZERO, earned(s, c, at) - invoiced_for(s, c, at, known_at)))


def forecast_qty(s: State, c: Commitment, at: datetime) -> Decimal:
    """当前预测数量：已有实际数量时用实际量，赛前用计划量。"""
    actual = actual_qty(s, c, at)
    if actual != ZERO:
        return actual
    return c.planned_qty + scope_qty(c, at)


def variance_bridge(s: State, c: Commitment, at: datetime) -> dict[str, Decimal]:
    """初始预算 vs 当前预测的差异桥：人数 / 单价 / 合同(固定与范围)。

    恒等式：baseline + qty_variance + price_variance + contract_variance
            = current（冻结后为冻结结算额）。
    """
    v0 = c.versions[0]
    p0, n0, f0 = v0.unit_price, c.planned_qty, v0.fixed
    v = _version(c, at)
    q = forecast_qty(s, c, at)
    if is_frozen(c, at):
        current = money(c.frozen_earned or ZERO)
    else:
        current = money(v.fixed + scope_fixed(c, at) + v.unit_price * q)
    qty_var = money((q - n0) * p0)
    price_var = money(q * (v.unit_price - p0))
    contract_var = money(current - (f0 + p0 * n0 + qty_var + price_var))
    return {
        "baseline": money(f0 + p0 * n0),
        "current": current,
        "qty_variance": qty_var,
        "price_variance": price_var,
        "contract_variance": contract_var,
    }


def commitment_view(
    s: State, c: Commitment, at: datetime, known_at: datetime | None = None
) -> dict[str, Any]:
    bridge = variance_bridge(s, c, at)
    v = _version(c, at)
    return {
        "id": c.id,
        "title": c.title,
        "kind": c.kind,
        "vendor": c.vendor,
        "cost_center": c.cost_center,
        "status": (
            "canceled"
            if c.canceled_at and c.canceled_at <= at
            else "closed"
            if c.closed_at and c.closed_at <= at
            else "active"
        ),
        "unit_price": float(v.unit_price),
        "planned_qty": float(c.planned_qty),
        "actual_qty": float(actual_qty(s, c, at)),
        "committed": float(committed(s, c, at)),
        "earned": float(earned(s, c, at)),
        "invoiced": float(invoiced_for(s, c, at, known_at)),
        "accrued": float(accrued(s, c, at, known_at)),
        "paid": float(paid_for(s, c, at, known_at)),
        "released": float(released(s, c, at)),
        "variance": {k: float(val) for k, val in bridge.items()},
    }


# ------------------------------------------------------------ 成本中心/审批


def center_totals(
    s: State, event_code: str, center_code: str, at: datetime
) -> dict[str, Decimal]:
    totals = {"committed": ZERO, "invoiced": ZERO, "earned": ZERO, "paid": ZERO}
    for c in s.commitments.values():
        if c.event_code != event_code or c.cost_center != center_code:
            continue
        totals["committed"] += committed(s, c, at)
        totals["invoiced"] += invoiced_for(s, c, at)
        totals["earned"] += earned(s, c, at)
        totals["paid"] += paid_for(s, c, at)
    return totals


def center_excess(
    s: State, event_code: str, center_code: str, at: datetime
) -> Decimal:
    center = s.centers.get(center_code)
    if center is None or center.direction != 1:
        return ZERO
    t = center_totals(s, event_code, center_code, at)
    return money(max(ZERO, max(t["committed"], t["invoiced"]) - center.budget))


def approval_cover(
    s: State, event_code: str, center_code: str, at: datetime
) -> Decimal:
    """窗口内作出的批准在决定时点之后持续有效（累计额度）。"""
    total = ZERO
    for a in s.approvals.values():
        if a.event_code != event_code or a.cost_center != center_code:
            continue
        if a.status == "approved" and a.decided_at is not None and a.decided_at <= at:
            total += a.amount
    return money(total)


def nearest_approval_gap(
    s: State, event_code: str, center_code: str, at: datetime
) -> str:
    for a in s.approvals.values():
        if a.event_code != event_code or a.cost_center != center_code:
            continue
        deadline = a.requested_at + timedelta(
            hours=s.centers[a.cost_center].approval_hours
        )
        # 只有已被标记过期（系统截止/逾期决定）才报"过期"；
        # 仍未决的申请按"缺少有效批准"处理，促使重新发起。
        if a.status == "expired":
            return "expired"
    return "missing"


def approval_queue(s: State, event_code: str, at: datetime) -> list[dict[str, Any]]:
    out = []
    for a in s.approvals.values():
        if a.event_code != event_code:
            continue
        if a.requested_at > at:
            continue
        center = s.centers[a.cost_center]
        deadline = a.requested_at + timedelta(hours=center.approval_hours)
        if a.status == "pending" and at >= deadline:
            status = "expired"
        else:
            status = a.status
        out.append(
            {
                "id": a.id,
                "cost_center": a.cost_center,
                "amount": float(a.amount),
                "status": status,
                "requested_at": iso(a.requested_at),
                "deadline": iso(deadline),
                "decided_at": iso(a.decided_at) if a.decided_at else None,
                "expires_at": iso(a.expires_at) if a.expires_at else None,
                "approver": a.approver,
                "reason": a.reason,
            }
        )
    return sorted(out, key=lambda x: x["requested_at"])


# -------------------------------------------------------------- 发票/事件台账


def invoice_register(s: State, event_code: str | None = None) -> list[dict[str, Any]]:
    """发票族台账：展示有效系数、关联原单与替代关系、已付情况。"""
    far = parse_time("9999-12-31T00:00:00+00:00")
    out = []
    for inv in s.invoices.values():
        if event_code and inv.event_code != event_code:
            continue
        eff_now = invoice_factor(s, inv, far)
        paid = invoice_paid(s, inv, far)
        out.append(
            {
                "id": inv.id,
                "event_code": inv.event_code,
                "vendor": inv.vendor,
                "invoice_no": inv.invoice_no,
                "kind": inv.kind,
                "issued_at": iso(inv.issued_at),
                "received_at": iso(inv.received_at),
                "payment_status": (
                    "paid"
                    if paid >= abs(inv.amount) * eff_now and eff_now > 0
                    else "partial"
                    if paid > 0
                    else "unpaid"
                ),
                "effective_ratio": float(eff_now),
                "amount": float(inv.amount),
                "effective_amount": float(money(inv.amount * eff_now)),
                "paid": float(paid),
                "original_id": inv.original_id,
                "replaces_id": inv.replaces_id,
                "lines": [
                    {
                        "commitment_id": ln.commitment_id,
                        "amount": float(ln.amount),
                        "qty": float(ln.qty) if ln.qty is not None else None,
                        "description": ln.description,
                    }
                    for ln in inv.lines
                ],
            }
        )
    return sorted(out, key=lambda x: (x["issued_at"], x["id"]))


def event_log(s: State, event_code: str | None = None) -> list[dict[str, Any]]:
    out = []
    for e in s.events:
        if event_code and _event_code_of(s, e) != event_code:
            continue
        out.append(
            {
                "seq": e.seq,
                "type": e.type,
                "occurred_at": iso(e.occurred_at),
                "received_at": iso(e.received_at),
                "payload": to_json(e.payload),
            }
        )
    return out


def _event_code_of(s: State, e: StoredEvent) -> str:
    p = e.payload
    if "event_code" in p:
        return p["event_code"]
    if e.type == "event_created":
        return p.get("code", "")
    cid = p.get("commitment_id")
    if cid and cid in s.commitments:
        return s.commitments[cid].event_code
    if e.type == "approval_expired":
        a = s.approvals.get(p.get("id", ""))
        return a.event_code if a else ""
    return ""
