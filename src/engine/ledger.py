"""分层账：承诺 / 预提 / 应付 / 已付 / 释放。

账本由 append-only 分录（entry）组成。每条分录是对某个账层桶位（bucket）
的一次带符号变动，同时携带：
  - business_date  业务时间（履约日、发票日、支付日、变更日）
  - received_at    接收时间（系统何时得知）
  - source_*       来源单据（事件、发票、支付、合同版本）

任意时点的账层余额 = 满足时间条件的分录按桶位求和，
因此财务可以按赛事日和成本中心重建"当时"的预算快照。

费用侧桶位流转（kind → 分录对）：
  commitment          committed += 承诺额
  commitment_adjust   committed ±= 计划调整（人数变化、合同换版）
  release             committed -= 释放额（取消/路线变更/退赛，仅未结算部分）
  accrual             committed -=, accrued +=   按实际完成量预提
  payable             accrued/committed -=, payable +=  发票确认应付
  payable_reversal    payable -=               红字发票冲销
  payment             payable -=, paid +=      支付

收入侧（side=income）：
  income_commitment   赞助权益承诺
  income_release      赞助权益取消（仅未结算部分）
  income              报名费收入
  refund              退赛退款
  receipt             赞助款到账（消耗 income_committed）
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from .money import ZERO

EXPENSE_BUCKETS = ("committed", "accrued", "payable", "paid")
INCOME_BUCKETS = ("income_committed", "income_received", "income", "refund")
ALL_BUCKETS = EXPENSE_BUCKETS + INCOME_BUCKETS

ENTRY_KINDS = (
    "commitment",
    "commitment_adjust",
    "release",
    "accrual",
    "payable",
    "payable_reversal",
    "payment",
    "income_commitment",
    "income_release",
    "income",
    "refund",
    "receipt",
)

# 这些 kind 的目标桶分录携带实际完成量与单价，用于偏差分解
ACTUAL_KINDS = ("accrual", "payable", "payable_reversal")


def fold_buckets(entries: Iterable[dict[str, Any]]) -> dict[str, Decimal]:
    """把分录折叠成桶位余额。"""
    buckets = {bucket: ZERO for bucket in ALL_BUCKETS}
    for entry in entries:
        buckets[entry["bucket"]] = buckets.get(entry["bucket"], ZERO) + Decimal(
            entry["amount"]
        )
    return buckets


def released_total(entries: Iterable[dict[str, Any]]) -> Decimal:
    """释放总额（release 分录的绝对值）。"""
    total = ZERO
    for entry in entries:
        if entry["kind"] == "release":
            total += -Decimal(entry["amount"])
    return total


def actuals(entries: Iterable[dict[str, Any]]) -> tuple[Decimal, Decimal]:
    """实际完成量与金额：(数量, 金额)，来自预提/应付/红字分录。"""
    qty = ZERO
    amount = ZERO
    for entry in entries:
        if entry["kind"] in ACTUAL_KINDS and entry["bucket"] in ("accrued", "payable"):
            amount += Decimal(entry["amount"])
            if entry.get("quantity") is not None:
                qty += Decimal(entry["quantity"])
    return qty, amount


def matches(
    entry: dict[str, Any],
    *,
    race_day: str | None = None,
    cost_center: str | None = None,
    item: str | None = None,
    as_of: str | None = None,
    received_before: str | None = None,
) -> bool:
    """快照过滤条件：维度 + 业务时间不晚于 as_of + 接收时间不晚于 received_before。"""
    if race_day is not None and entry["race_day"] != race_day:
        return False
    if cost_center is not None and entry["cost_center"] != cost_center:
        return False
    if item is not None and entry["item"] != item:
        return False
    if as_of is not None and entry["business_date"] > as_of:
        return False
    if received_before is not None and entry["received_at"] > received_before:
        return False
    return True


def select(
    entries: Iterable[dict[str, Any]],
    *,
    race_day: str | None = None,
    cost_center: str | None = None,
    item: str | None = None,
    as_of: str | None = None,
    received_before: str | None = None,
) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if matches(
            entry,
            race_day=race_day,
            cost_center=cost_center,
            item=item,
            as_of=as_of,
            received_before=received_before,
        )
    ]
