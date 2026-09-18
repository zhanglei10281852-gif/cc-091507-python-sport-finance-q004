"""领域模型：全部以显式业务时间保存，金额用 Decimal。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

Layer = Literal["committed", "earned", "payable", "accrued", "released", "paid"]

# 供应商成本类目的分层：committed 合同承诺 -> earned 实际完成 ->
# payable 发票确认（拆票/红字/重开） -> paid 支付回执
# accrued 为无发票时按完成量预提；结算后释放（released）多余承诺。
# 报名收入使用同一引擎的收入方向（sign=-1 的成本中心）。


@dataclass
class CostCenter:
    code: str
    name: str
    # +1 支出（预算占用），-1 收入（报名费/赞助等）
    direction: int = 1
    # 支出中心为预算上限；收入中心为收入目标（均为正数）
    budget: Decimal = Decimal("0")
    # 超支审批时限（小时）
    approval_hours: int = 48


@dataclass
class Event:
    code: str
    name: str
    race_date: datetime
    currency: str = "CNY"


@dataclass
class ContractVersion:
    """合同的一个版本：某业务时间起生效的单价/结构。

    unit_price: 单价（每人/每单位）；fixed=固定金额部分。
    承诺额 = fixed + unit_price * 计划数量（预算时为报名人数预测）。
    """

    version: int
    effective_at: datetime
    unit_price: Decimal
    fixed: Decimal = Decimal("0")
    note: str = ""


@dataclass
class ScopeAdjust:
    """合同范围变更（路线调整导致赞助权益取消等）。

    delta_qty / delta_fixed 直接调增(+)或调减(-)承诺口径。
    """

    at: datetime
    delta_qty: Decimal = Decimal("0")
    delta_fixed: Decimal = Decimal("0")
    reason: str = ""


@dataclass
class Commitment:
    """一项预算承诺（合同/预算行），可随合同版本演进。"""

    id: str
    event_code: str
    cost_center: str
    kind: str  # contract | registration | volunteer | venue | sponsor
    title: str
    vendor: str | None
    planned_qty: Decimal = Decimal("0")
    service_at: datetime | None = None
    versions: list[ContractVersion] = field(default_factory=list)
    qty_records: list[QtyRecord] = field(default_factory=list)
    adjusts: list[ScopeAdjust] = field(default_factory=list)
    canceled_at: datetime | None = None
    cancel_reason: str = ""
    closed_at: datetime | None = None
    # 取消/关闭时冻结的已挣得金额（此后只允许针对已完成部分的发票）
    frozen_earned: Decimal | None = None

    def version_at(self, at: datetime) -> ContractVersion:
        current = self.versions[0]
        for v in self.versions:
            if v.effective_at <= at:
                current = v
            else:
                break
        return current


@dataclass
class QtyRecord:
    """报名 / 退赛 / 志愿者出勤等数量流水，决定实际完成量。"""

    id: str
    commitment_id: str
    at: datetime  # 业务时间
    delta: Decimal  # 报名 +1，退赛 -1
    unit_override: Decimal | None = None  # 退赛手续费等场景
    ref: str = ""


@dataclass
class InvoiceLine:
    commitment_id: str
    amount: Decimal
    qty: Decimal | None = None
    description: str = ""


@dataclass
class Invoice:
    """发票（含红字/重开/拆票），通过 original_id 关联原单。"""

    id: str
    event_code: str
    vendor: str
    invoice_no: str
    issued_at: datetime  # 开票日（业务时间）
    received_at: datetime  # 接收时间（乱序到达依据）
    lines: list[InvoiceLine]
    kind: Literal["normal", "red", "reissue"] = "normal"
    original_id: str | None = None  # 红字/重开关联原票
    replaces_id: str | None = None  # 重开票取代的旧票
    note: str = ""

    @property
    def amount(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), Decimal("0"))


@dataclass
class Payment:
    id: str
    event_code: str
    vendor: str
    receipt_no: str
    invoice_id: str  # 支付必须指向具体发票
    amount: Decimal
    paid_at: datetime  # 支付/回执业务时间
    received_at: datetime  # 回执到达时间


@dataclass
class Approval:
    id: str
    event_code: str
    cost_center: str
    commitment_id: str
    amount: Decimal  # 批准的超支额度上限
    requested_at: datetime
    decided_at: datetime | None = None
    approver: str = ""
    status: Literal["pending", "approved", "rejected", "expired"] = "pending"
    expires_at: datetime | None = None
    note: str = ""
    reason: str = ""


@dataclass
class StoredEvent:
    """不可变事件记录：业务时间与接收时间分离。"""

    seq: int
    type: str
    occurred_at: datetime  # 业务时间
    received_at: datetime  # 系统接收时间
    payload: dict[str, Any]
