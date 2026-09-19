"""结算引擎领域服务。

职责：
  - 合同与合同版本（承诺的来源）
  - 报名 / 退赛 / 赞助变更 / 路线变更 / 赛事取消 / 完成量申报 事件摄入
  - 发票接收、拆票、红字、重新开票（血缘关联原单）
  - 支付回执幂等应用（乱序到达不重复付款）
  - 按赛事日 + 成本中心重建预算快照（业务时间 / 接收时间 双时间轴）
  - 偏差分解：人数(数量) / 单价 / 合同变更
  - 超支审批队列（带截止时间）

所有状态经 Store 持久化，账层分录 append-only。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from . import ledger
from .money import ZERO, money_str, mul_money_qty, qty_str, to_money, to_qty
from .store import Store

EVENT_TYPES = (
    "registration",
    "withdrawal",
    "sponsor_change",
    "route_change",
    "cancellation",
    "fulfillment",
)

CONTRACT_KINDS = ("vendor", "sponsor", "venue", "volunteer")

DEFAULT_CURRENCIES = ("CNY", "HKD", "USD")

# 超支审批默认时限（小时），可用环境变量覆盖
APPROVAL_DEADLINE_HOURS = int(os.getenv("APPROVAL_DEADLINE_HOURS", "72"))


class ServiceError(Exception):
    """业务校验或状态冲突。"""

    def __init__(self, code: str, message: str, status: int = 400, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def _check_date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ServiceError("validation", f"{field} 必须是 YYYY-MM-DD 字符串")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ServiceError("validation", f"{field} 不是合法日期: {value!r}") from exc
    return value


def _require(payload: dict[str, Any], *fields: str) -> None:
    missing = [f for f in fields if payload.get(f) is None]
    if missing:
        raise ServiceError("validation", f"缺少必填字段: {', '.join(missing)}")


def _prorate_amounts(lines: list[dict[str, Any]], amount: Decimal) -> list[Decimal]:
    """把 amount 按行金额比例分摊到各行，尾差落在最后一行，保证合计精确相等。"""
    total = sum((Decimal(line["amount"]) for line in lines), ZERO)
    shares: list[Decimal] = []
    distributed = ZERO
    for idx, line in enumerate(lines):
        if idx == len(lines) - 1:
            share = amount - distributed
        elif total != ZERO:
            share = to_money(Decimal(line["amount"]) * amount / total)
        else:
            share = ZERO
        shares.append(share)
        distributed += share
    return shares


def _to_count(value: Any) -> int:
    """报名/退赛人数：必须是正整数。"""
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ServiceError("validation", f"人数必须是整数: {value!r}") from exc
    if isinstance(value, float) and value != count:
        raise ServiceError("validation", f"人数必须是整数: {value!r}")
    if count <= 0:
        raise ServiceError("validation", "人数必须为正整数")
    return count


class SettlementService:
    def __init__(self, store: Store, currencies: Iterable[str] = DEFAULT_CURRENCIES):
        self.store = store
        self.currencies = tuple(currencies)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _post_entry(
        self,
        *,
        kind: str,
        bucket: str,
        side: str,
        amount: Decimal,
        race_day: str,
        cost_center: str,
        item: str,
        currency: str,
        business_date: str,
        source_type: str,
        source_id: str,
        contract_id: str | None = None,
        line_id: str | None = None,
        quantity: Decimal | None = None,
        unit_price: Decimal | None = None,
        note: str | None = None,
        received_at: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": self.store.next_id("le"),
            "kind": kind,
            "bucket": bucket,
            "side": side,
            "amount": money_str(amount),
            "quantity": qty_str(quantity) if quantity is not None else None,
            "unit_price": money_str(unit_price) if unit_price is not None else None,
            "race_day": race_day,
            "cost_center": cost_center,
            "item": item,
            "contract_id": contract_id,
            "line_id": line_id,
            "currency": currency,
            "business_date": business_date,
            "received_at": received_at or utc_now(),
            "source_type": source_type,
            "source_id": source_id,
            "note": note,
        }
        return self.store.append("entries", entry)

    def _entries(
        self,
        *,
        race_day: str | None = None,
        cost_center: str | None = None,
        item: str | None = None,
        as_of: str | None = None,
        received_before: str | None = None,
    ) -> list[dict[str, Any]]:
        return ledger.select(
            self.store.collection("entries"),
            race_day=race_day,
            cost_center=cost_center,
            item=item,
            as_of=as_of,
            received_before=received_before,
        )

    def _find(self, collection: str, record_id: str, label: str) -> dict[str, Any]:
        for record in self.store.collection(collection):
            if record["id"] == record_id:
                return record
        raise ServiceError("not_found", f"{label}不存在: {record_id}", status=404)

    def _check_currency(self, currency: str) -> str:
        if currency not in self.currencies:
            raise ServiceError(
                "validation",
                f"不支持的币种: {currency}",
                details={"supported": list(self.currencies)},
            )
        return currency

    # ------------------------------------------------------------------
    # 报名人数（按赛事日）
    # ------------------------------------------------------------------

    def headcount(
        self,
        race_day: str,
        as_of: str | None = None,
        received_before: str | None = None,
    ) -> int:
        """净报名人数 = 报名 - 退赛，可按业务时间/接收时间回放。"""
        total = 0
        for event in self.store.collection("events"):
            if event["type"] not in ("registration", "withdrawal"):
                continue
            if event["race_day"] != race_day:
                continue
            if as_of is not None and event["business_date"] > as_of:
                continue
            if received_before is not None and event["received_at"] > received_before:
                continue
            count = int(event["payload"].get("count", 1))
            total += count if event["type"] == "registration" else -count
        return total

    # ------------------------------------------------------------------
    # 合同
    # ------------------------------------------------------------------

    @staticmethod
    def _version_for(contract: dict[str, Any], on_date: str) -> dict[str, Any] | None:
        """生效时间 <= on_date 的最新版本。"""
        candidates = [v for v in contract["versions"] if v["effective_date"] <= on_date]
        if not candidates:
            return None
        return max(candidates, key=lambda v: (v["effective_date"], v["version"]))

    @staticmethod
    def _line_planned_qty(line: dict[str, Any], headcount: int) -> Decimal:
        if line["quantity_basis"] == "per_head":
            return to_qty(Decimal(line["per_head_factor"]) * headcount)
        return to_qty(line["fixed_quantity"])

    def create_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "vendor", "kind", "race_day", "cost_center", "currency", "lines")
        kind = payload["kind"]
        if kind not in CONTRACT_KINDS:
            raise ServiceError("validation", f"未知合同类型: {kind}",
                               details={"allowed": list(CONTRACT_KINDS)})
        race_day = _check_date(payload["race_day"], "race_day")
        effective = _check_date(payload.get("effective_date", today()), "effective_date")
        currency = self._check_currency(payload["currency"])
        lines = self._normalize_lines(payload["lines"])
        side = "income" if kind == "sponsor" else "expense"

        contract = {
            "id": self.store.next_id("ctr"),
            "vendor": payload["vendor"],
            "kind": kind,
            "side": side,
            "race_day": race_day,
            "cost_center": payload["cost_center"],
            "currency": currency,
            "created_at": utc_now(),
            "versions": [],
        }
        version = {
            "version": 1,
            "effective_date": effective,
            "received_at": utc_now(),
            "reason": payload.get("reason", "initial"),
            "lines": lines,
        }
        contract["versions"].append(version)
        self.store.append("contracts", contract)

        head = self.headcount(race_day, as_of=effective)
        posted = []
        for line in lines:
            qty = self._line_planned_qty(line, head)
            amount = mul_money_qty(Decimal(line["unit_price"]), qty)
            if amount == ZERO:
                continue
            kind_name = "income_commitment" if side == "income" else "commitment"
            bucket = "income_committed" if side == "income" else "committed"
            posted.append(self._post_entry(
                kind=kind_name, bucket=bucket, side=side, amount=amount,
                race_day=race_day, cost_center=contract["cost_center"],
                item=line["item"], currency=currency,
                business_date=effective,
                source_type="contract_version", source_id=f"{contract['id']}@1",
                contract_id=contract["id"], line_id=line["line_id"],
                quantity=qty, unit_price=Decimal(line["unit_price"]),
                note="合同初始承诺",
            ))
            if side == "expense":
                self._check_overspend(race_day, contract["cost_center"], line["item"], currency)
        self.store.save()
        return {"contract": contract, "posted_entries": posted}

    def add_contract_version(self, contract_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """合同换版：只调整未结算部分，已结算部分冻结并报告冲突。"""
        _require(payload, "effective_date", "lines")
        contract = self._find("contracts", contract_id, "合同")
        effective = _check_date(payload["effective_date"], "effective_date")
        new_lines = self._normalize_lines(payload["lines"])
        version_no = len(contract["versions"]) + 1
        version = {
            "version": version_no,
            "effective_date": effective,
            "received_at": utc_now(),
            "reason": payload.get("reason", "amendment"),
            "lines": new_lines,
        }
        contract["versions"].append(version)

        head = self.headcount(contract["race_day"], as_of=effective)
        # 旧版 = 新版之前最后一个生效版本
        prior = [v for v in contract["versions"][:-1] if v["effective_date"] <= effective]
        old_version = max(prior, key=lambda v: (v["effective_date"], v["version"])) if prior else None

        adjustments: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        all_items = {l["item"] for l in new_lines}
        if old_version:
            all_items |= {l["item"] for l in old_version["lines"]}

        for item in sorted(all_items):
            new_line = next((l for l in new_lines if l["item"] == item), None)
            old_line = (
                next((l for l in old_version["lines"] if l["item"] == item), None)
                if old_version else None
            )
            new_amount = self._line_amount(new_line, head) if new_line else ZERO
            old_amount = self._line_amount(old_line, head) if old_line else ZERO
            delta = new_amount - old_amount
            if delta == ZERO:
                continue
            line_id = (new_line or old_line)["line_id"]
            if contract["side"] == "income":
                adj, conflict = self._adjust_income_commitment(
                    contract, item, line_id, delta, effective,
                    source_id=f"{contract_id}@{version_no}",
                    note=f"合同换版 v{version_no}: {version['reason']}",
                )
            else:
                adj, conflict = self._adjust_commitment(
                    contract, item, line_id, delta, effective,
                    source_id=f"{contract_id}@{version_no}",
                    note=f"合同换版 v{version_no}: {version['reason']}",
                )
            adjustments.extend(adj)
            if conflict:
                conflicts.append(conflict)

        self.store.save()
        result: dict[str, Any] = {"contract": contract, "adjustments": adjustments}
        if conflicts:
            result["conflicts"] = conflicts
            result["warning"] = "部分已结算金额被冻结，未随版本调整"
        return result

    def _line_amount(self, line: dict[str, Any] | None, headcount: int) -> Decimal:
        if line is None:
            return ZERO
        qty = self._line_planned_qty(line, headcount)
        return mul_money_qty(Decimal(line["unit_price"]), qty)

    def _normalize_lines(self, lines: Any) -> list[dict[str, Any]]:
        if not isinstance(lines, list) or not lines:
            raise ServiceError("validation", "lines 必须是非空数组")
        normalized = []
        seen_items: set[str] = set()
        for i, raw in enumerate(lines, start=1):
            _require(raw, "item", "unit_price", "quantity_basis")
            item = raw["item"]
            if item in seen_items:
                raise ServiceError("validation", f"合同行项目重复: {item}")
            seen_items.add(item)
            basis = raw["quantity_basis"]
            if basis not in ("fixed", "per_head"):
                raise ServiceError("validation", f"quantity_basis 非法: {basis}")
            line = {
                "line_id": raw.get("line_id", f"L{i}"),
                "item": item,
                "unit_price": money_str(to_money(raw["unit_price"], "unit_price")),
                "quantity_basis": basis,
                "fixed_quantity": qty_str(to_qty(raw.get("fixed_quantity", 1)))
                if basis == "fixed" else None,
                "per_head_factor": qty_str(to_qty(raw.get("per_head_factor", 1)))
                if basis == "per_head" else None,
            }
            normalized.append(line)
        return normalized

    # ------------------------------------------------------------------
    # 承诺调整（费用侧 / 收入侧共用骨架）
    # ------------------------------------------------------------------

    def _line_buckets(self, contract: dict[str, Any], item: str) -> dict[str, Decimal]:
        entries = self._entries(
            race_day=contract["race_day"],
            cost_center=contract["cost_center"],
            item=item,
        )
        entries = [e for e in entries if e.get("contract_id") == contract["id"]]
        return ledger.fold_buckets(entries)

    def _adjust_commitment(
        self, contract, item, line_id, delta: Decimal, business_date: str,
        *, source_id: str, note: str, source_type: str = "contract_version",
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """费用侧承诺调整：正向补承诺，负向释放（不超过未结算余额）。"""
        posted: list[dict[str, Any]] = []
        conflict = None
        if delta > ZERO:
            posted.append(self._post_entry(
                kind="commitment_adjust", bucket="committed", side="expense",
                amount=delta, race_day=contract["race_day"],
                cost_center=contract["cost_center"], item=item,
                currency=contract["currency"], business_date=business_date,
                source_type=source_type, source_id=source_id,
                contract_id=contract["id"], line_id=line_id, note=note,
            ))
        elif delta < ZERO:
            buckets = self._line_buckets(contract, item)
            open_committed = buckets["committed"]
            release_amount = min(-delta, open_committed)
            if release_amount > ZERO:
                posted.append(self._post_entry(
                    kind="release", bucket="committed", side="expense",
                    amount=-release_amount, race_day=contract["race_day"],
                    cost_center=contract["cost_center"], item=item,
                    currency=contract["currency"], business_date=business_date,
                    source_type=source_type, source_id=source_id,
                    contract_id=contract["id"], line_id=line_id, note=note,
                ))
            frozen = -delta - release_amount
            if frozen > ZERO:
                conflict = {
                    "contract_id": contract["id"], "item": item,
                    "requested_release": money_str(-delta),
                    "released": money_str(release_amount),
                    "frozen_settled": money_str(frozen),
                    "reason": "已结算部分不受变更影响",
                }
        if contract["side"] == "expense":
            self._check_overspend(
                contract["race_day"], contract["cost_center"], item, contract["currency"])
        return posted, conflict

    def _adjust_income_commitment(
        self, contract, item, line_id, delta: Decimal, business_date: str,
        *, source_id: str, note: str, source_type: str = "contract_version",
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        posted: list[dict[str, Any]] = []
        conflict = None
        if delta > ZERO:
            posted.append(self._post_entry(
                kind="income_commitment", bucket="income_committed", side="income",
                amount=delta, race_day=contract["race_day"],
                cost_center=contract["cost_center"], item=item,
                currency=contract["currency"], business_date=business_date,
                source_type=source_type, source_id=source_id,
                contract_id=contract["id"], line_id=line_id, note=note,
            ))
        elif delta < ZERO:
            buckets = self._line_buckets(contract, item)
            open_income = buckets["income_committed"]
            release_amount = min(-delta, open_income)
            if release_amount > ZERO:
                posted.append(self._post_entry(
                    kind="income_release", bucket="income_committed", side="income",
                    amount=-release_amount, race_day=contract["race_day"],
                    cost_center=contract["cost_center"], item=item,
                    currency=contract["currency"], business_date=business_date,
                    source_type=source_type, source_id=source_id,
                    contract_id=contract["id"], line_id=line_id, note=note,
                ))
            frozen = -delta - release_amount
            if frozen > ZERO:
                conflict = {
                    "contract_id": contract["id"], "item": item,
                    "requested_release": money_str(-delta),
                    "released": money_str(release_amount),
                    "frozen_settled": money_str(frozen),
                    "reason": "已结算赞助权益不受变更影响",
                }
        return posted, conflict

    # ------------------------------------------------------------------
    # 预算版本
    # ------------------------------------------------------------------

    def create_budget(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "race_day", "cost_center", "currency", "lines")
        race_day = _check_date(payload["race_day"], "race_day")
        cost_center = payload["cost_center"]
        currency = self._check_currency(payload["currency"])
        business_date = _check_date(payload.get("business_date", today()), "business_date")
        lines = []
        for raw in payload["lines"]:
            _require(raw, "item", "planned_quantity", "unit_price")
            lines.append({
                "item": raw["item"],
                "planned_quantity": qty_str(to_qty(raw["planned_quantity"])),
                "unit_price": money_str(to_money(raw["unit_price"])),
                "quantity_basis": raw.get("quantity_basis", "fixed"),
            })
        existing = [b for b in self.store.collection("budgets")
                    if b["race_day"] == race_day and b["cost_center"] == cost_center]
        budget = {
            "id": self.store.next_id("bud"),
            "race_day": race_day,
            "cost_center": cost_center,
            "version": len(existing) + 1,
            "business_date": business_date,
            "received_at": utc_now(),
            "currency": currency,
            "lines": lines,
        }
        self.store.append("budgets", budget)
        for line in lines:
            self._check_overspend(race_day, cost_center, line["item"], currency)
        self.store.save()
        return {"budget": budget}

    def _budget_version(
        self, race_day: str, cost_center: str,
        as_of: str | None = None, received_before: str | None = None,
        baseline: bool = False,
    ) -> dict[str, Any] | None:
        versions = [
            b for b in self.store.collection("budgets")
            if b["race_day"] == race_day and b["cost_center"] == cost_center
            and (as_of is None or b["business_date"] <= as_of)
            and (received_before is None or b["received_at"] <= received_before)
        ]
        if not versions:
            return None
        key = (lambda b: b["version"])
        return min(versions, key=key) if baseline else max(versions, key=key)

    # ------------------------------------------------------------------
    # 事件摄入：报名 / 退赛 / 赞助变更 / 路线变更 / 取消 / 完成量
    # ------------------------------------------------------------------

    def ingest_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "type", "race_day")
        event_type = payload["type"]
        if event_type not in EVENT_TYPES:
            raise ServiceError("validation", f"未知事件类型: {event_type}",
                               details={"allowed": list(EVENT_TYPES)})
        idem = payload.get("idempotency_key")
        if idem:
            for existing in self.store.collection("events"):
                if existing.get("idempotency_key") == idem:
                    return {"event": existing, "deduplicated": True}

        race_day = _check_date(payload["race_day"], "race_day")
        business_date = _check_date(payload.get("business_date", today()), "business_date")
        event = {
            "id": self.store.next_id("evt"),
            "type": event_type,
            "idempotency_key": idem,
            "race_day": race_day,
            "business_date": business_date,
            "received_at": utc_now(),
            "payload": {k: v for k, v in payload.items()
                        if k not in ("type", "race_day", "business_date", "idempotency_key")},
            "result": {},
        }

        handler = {
            "registration": self._on_registration,
            "withdrawal": self._on_withdrawal,
            "sponsor_change": self._on_sponsor_change,
            "route_change": self._on_route_change,
            "cancellation": self._on_cancellation,
            "fulfillment": self._on_fulfillment,
        }[event_type]
        # 先入库再处理：人数重算等逻辑需要看到当前事件；
        # 处理失败则回滚本次产生的记录，保持账本一致。
        entries_marker = len(self.store.collection("entries"))
        approvals_marker = len(self.store.collection("approvals"))
        self.store.append("events", event)
        try:
            event["result"] = handler(event)
        except Exception:
            del self.store.collection("entries")[entries_marker:]
            del self.store.collection("approvals")[approvals_marker:]
            self.store.collection("events").remove(event)
            raise
        self.store.save()
        return {"event": event, "deduplicated": False}

    def _on_registration(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = event["payload"]
        count = _to_count(payload.get("count", 1))
        fee = payload.get("fee")
        currency = self._check_currency(payload.get("currency", "CNY"))
        cost_center = payload.get("cost_center", "CC-REG")
        if fee is not None:
            amount = mul_money_qty(to_money(fee, "fee"), Decimal(count))
            self._post_entry(
                kind="income", bucket="income", side="income", amount=amount,
                race_day=event["race_day"], cost_center=cost_center,
                item=payload.get("item", "报名费"), currency=currency,
                business_date=event["business_date"],
                source_type="event", source_id=event["id"],
                quantity=Decimal(count), note="报名费收入",
            )
        adjustments = self._recompute_per_head(event)
        return {"headcount_delta": count, "commitment_adjustments": adjustments}

    def _on_withdrawal(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = event["payload"]
        count = _to_count(payload.get("count", 1))
        refund = payload.get("refund")
        currency = self._check_currency(payload.get("currency", "CNY"))
        cost_center = payload.get("cost_center", "CC-REG")
        if refund is not None:
            amount = mul_money_qty(to_money(refund, "refund"), Decimal(count))
            self._post_entry(
                kind="refund", bucket="refund", side="income", amount=amount,
                race_day=event["race_day"], cost_center=cost_center,
                item=payload.get("item", "报名费"), currency=currency,
                business_date=event["business_date"],
                source_type="event", source_id=event["id"],
                quantity=Decimal(count), note="退赛退款",
            )
        adjustments = self._recompute_per_head(event)
        return {"headcount_delta": -count, "commitment_adjustments": adjustments}

    def _recompute_per_head(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """人数变化后，重算所有 per_head 合同行的承诺（只动未结算部分）。"""
        race_day = event["race_day"]
        business_date = event["business_date"]
        head = self.headcount(race_day, as_of=business_date)
        adjustments: list[dict[str, Any]] = []
        for contract in self.store.collection("contracts"):
            if contract["race_day"] != race_day:
                continue
            version = self._version_for(contract, business_date)
            if not version:
                continue
            for line in version["lines"]:
                if line["quantity_basis"] != "per_head":
                    continue
                target = mul_money_qty(
                    Decimal(line["unit_price"]),
                    self._line_planned_qty(line, head),
                )
                planned = self._line_planned_amount(contract, line["item"])
                delta = target - planned
                if delta == ZERO:
                    continue
                if contract["side"] == "income":
                    posted, conflict = self._adjust_income_commitment(
                        contract, line["item"], line["line_id"], delta, business_date,
                        source_id=event["id"], source_type="event",
                        note=f"人数变化重算(净报名 {head})",
                    )
                else:
                    posted, conflict = self._adjust_commitment(
                        contract, line["item"], line["line_id"], delta, business_date,
                        source_id=event["id"], source_type="event",
                        note=f"人数变化重算(净报名 {head})",
                    )
                record: dict[str, Any] = {
                    "contract_id": contract["id"], "item": line["item"],
                    "delta": money_str(delta),
                }
                if conflict:
                    record["conflict"] = conflict
                if posted:
                    adjustments.append(record)
        return adjustments

    def _line_planned_amount(self, contract: dict[str, Any], item: str) -> Decimal:
        """该合同行已计提的承诺毛额（commitment + commitment_adjust，未扣释放）。"""
        total = ZERO
        for entry in self.store.collection("entries"):
            if entry.get("contract_id") != contract["id"] or entry["item"] != item:
                continue
            if entry["kind"] in ("commitment", "commitment_adjust", "income_commitment"):
                total += Decimal(entry["amount"])
        return total

    def _on_sponsor_change(self, event: dict[str, Any]) -> dict[str, Any]:
        """赞助权益变更：delta_amount 为负表示取消部分权益（仅未结算部分）。"""
        payload = event["payload"]
        _require(payload, "contract_id", "item", "delta_amount")
        contract = self._find("contracts", payload["contract_id"], "赞助合同")
        if contract["side"] != "income":
            raise ServiceError("validation", "sponsor_change 只能用于赞助合同")
        delta = to_money(payload["delta_amount"], "delta_amount")
        line_id = self._contract_line_id(contract, payload["item"], event["business_date"])
        posted, conflict = self._adjust_income_commitment(
            contract, payload["item"], line_id, delta, event["business_date"],
            source_id=event["id"], source_type="event",
            note=payload.get("reason", "赞助权益变更"),
        )
        result: dict[str, Any] = {"adjustments": [e["id"] for e in posted]}
        if conflict:
            result["conflict"] = conflict
        return result

    def _on_route_change(self, event: dict[str, Any]) -> dict[str, Any]:
        """路线变更：按 impacts 清单释放未结算承诺，已结算部分冻结。"""
        payload = event["payload"]
        impacts = payload.get("impacts")
        if not isinstance(impacts, list) or not impacts:
            raise ServiceError("validation", "route_change 需要非空 impacts 数组")
        released: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for impact in impacts:
            _require(impact, "contract_id", "item")
            contract = self._find("contracts", impact["contract_id"], "合同")
            item = impact["item"]
            line_id = self._contract_line_id(contract, item, event["business_date"])
            buckets = self._line_buckets(contract, item)
            if contract["side"] == "income":
                open_amount = buckets["income_committed"]
            else:
                open_amount = buckets["committed"]
            if impact.get("delta_amount") is not None:
                requested = to_money(impact["delta_amount"], "delta_amount")
                want = min(requested, open_amount)
            else:
                requested = None
                want = open_amount  # cancel=true 或未指定 → 释放全部未结算
            if want <= ZERO:
                if open_amount <= ZERO:
                    conflicts.append({
                        "contract_id": contract["id"], "item": item,
                        "frozen_settled": money_str(ZERO),
                        "reason": "该项目的承诺已全部结算或释放，无未结算余额可调整",
                    })
                continue
            if requested is not None and requested > open_amount:
                conflicts.append({
                    "contract_id": contract["id"], "item": item,
                    "requested_release": money_str(requested),
                    "released": money_str(open_amount),
                    "frozen_settled": money_str(requested - open_amount),
                    "reason": "已结算部分不受路线变更影响",
                })
            if contract["side"] == "income":
                posted, _ = self._adjust_income_commitment(
                    contract, item, line_id, -want, event["business_date"],
                    source_id=event["id"], source_type="event",
                    note=payload.get("reason", "路线变更"),
                )
            else:
                posted, _ = self._adjust_commitment(
                    contract, item, line_id, -want, event["business_date"],
                    source_id=event["id"], source_type="event",
                    note=payload.get("reason", "路线变更"),
                )
            released.append({
                "contract_id": contract["id"], "item": item,
                "released": money_str(want),
                "entries": [e["id"] for e in posted],
            })
        result: dict[str, Any] = {"released": released}
        if conflicts:
            result["conflicts"] = conflicts
        return result

    def _on_cancellation(self, event: dict[str, Any]) -> dict[str, Any]:
        """赛事取消：释放该赛事日（可选成本中心）全部未结算承诺。"""
        payload = event["payload"]
        scope_center = payload.get("cost_center")
        released: list[dict[str, Any]] = []
        frozen: list[dict[str, Any]] = []
        for contract in self.store.collection("contracts"):
            if contract["race_day"] != event["race_day"]:
                continue
            if scope_center and contract["cost_center"] != scope_center:
                continue
            version = self._version_for(contract, event["business_date"])
            if not version:
                continue
            for line in version["lines"]:
                item = line["item"]
                buckets = self._line_buckets(contract, item)
                if contract["side"] == "income":
                    open_amount = buckets["income_committed"]
                    settled = self._line_settled_amount(contract, item, "income")
                else:
                    open_amount = buckets["committed"]
                    settled = self._line_settled_amount(contract, item, "expense")
                if open_amount > ZERO:
                    if contract["side"] == "income":
                        posted, _ = self._adjust_income_commitment(
                            contract, item, line["line_id"], -open_amount,
                            event["business_date"], source_id=event["id"],
                            source_type="event",
                            note=payload.get("reason", "赛事取消"),
                        )
                    else:
                        posted, _ = self._adjust_commitment(
                            contract, item, line["line_id"], -open_amount,
                            event["business_date"], source_id=event["id"],
                            source_type="event",
                            note=payload.get("reason", "赛事取消"),
                        )
                    released.append({
                        "contract_id": contract["id"], "item": item,
                        "released": money_str(open_amount),
                    })
                if settled > ZERO:
                    frozen.append({
                        "contract_id": contract["id"], "item": item,
                        "frozen_settled": money_str(settled),
                        "reason": "已结算部分不随取消改变",
                    })
        return {"released": released, "frozen": frozen}

    def _line_settled_amount(self, contract: dict[str, Any], item: str, side: str) -> Decimal:
        """已结算金额：费用侧 = 预提+应付净额+已付；收入侧 = 已到账。"""
        entries = [
            e for e in self._entries(
                race_day=contract["race_day"],
                cost_center=contract["cost_center"], item=item)
            if e.get("contract_id") == contract["id"]
        ]
        buckets = ledger.fold_buckets(entries)
        if side == "income":
            return buckets["income_received"]
        return buckets["accrued"] + buckets["payable"] + buckets["paid"]

    def _on_fulfillment(self, event: dict[str, Any]) -> dict[str, Any]:
        """完成量申报：按合同单价 × 实际完成量形成预提，消耗未结算承诺。"""
        payload = event["payload"]
        _require(payload, "contract_id", "item", "quantity")
        contract = self._find("contracts", payload["contract_id"], "合同")
        if contract["side"] != "expense":
            raise ServiceError("validation", "fulfillment 只能用于费用类合同")
        qty = to_qty(payload["quantity"])
        if qty <= ZERO:
            raise ServiceError("validation", "完成量必须为正")
        version = self._version_for(contract, event["business_date"])
        if not version:
            raise ServiceError("validation", "合同在完成量业务日期前未生效")
        line = next((l for l in version["lines"] if l["item"] == payload["item"]), None)
        if line is None:
            raise ServiceError("validation", f"合同行不存在: {payload['item']}")
        price = to_money(payload.get("unit_price", line["unit_price"]), "unit_price")
        amount = mul_money_qty(price, qty)

        buckets = self._line_buckets(contract, line["item"])
        consume = min(amount, max(buckets["committed"], ZERO))
        posted = []
        if consume > ZERO:
            posted.append(self._post_entry(
                kind="accrual", bucket="committed", side="expense", amount=-consume,
                race_day=contract["race_day"], cost_center=contract["cost_center"],
                item=line["item"], currency=contract["currency"],
                business_date=event["business_date"],
                source_type="event", source_id=event["id"],
                contract_id=contract["id"], line_id=line["line_id"],
                note="预提消耗承诺",
            ))
        posted.append(self._post_entry(
            kind="accrual", bucket="accrued", side="expense", amount=amount,
            race_day=contract["race_day"], cost_center=contract["cost_center"],
            item=line["item"], currency=contract["currency"],
            business_date=event["business_date"],
            source_type="event", source_id=event["id"],
            contract_id=contract["id"], line_id=line["line_id"],
            quantity=qty, unit_price=price,
            note=f"按实际完成量预提(数量 {qty_str(qty)})",
        ))
        self._check_overspend(
            contract["race_day"], contract["cost_center"], line["item"], contract["currency"])
        return {
            "accrued": money_str(amount),
            "commitment_consumed": money_str(consume),
            "entries": [e["id"] for e in posted],
        }

    def _contract_line_id(self, contract: dict[str, Any], item: str, on_date: str) -> str | None:
        version = self._version_for(contract, on_date)
        if version:
            for line in version["lines"]:
                if line["item"] == item:
                    return line["line_id"]
        return None

    # ------------------------------------------------------------------
    # 发票
    # ------------------------------------------------------------------

    def _normalize_invoice_lines(
        self, raw_lines: Any
    ) -> tuple[list[dict[str, Any]], Decimal]:
        """解析并校验发票行，返回 (行列表, 合计金额)。"""
        if not isinstance(raw_lines, list) or not raw_lines:
            raise ServiceError("validation", "发票行不能为空")
        lines: list[dict[str, Any]] = []
        total = ZERO
        for raw in raw_lines:
            _require(raw, "item", "quantity", "unit_price")
            qty = to_qty(raw["quantity"])
            if qty <= ZERO:
                raise ServiceError("validation", f"发票行数量必须为正: {raw['item']}")
            price = to_money(raw["unit_price"], "unit_price")
            if price < ZERO:
                raise ServiceError("validation", f"发票行单价不能为负: {raw['item']}")
            amount = mul_money_qty(price, qty)
            total += amount
            lines.append({
                "item": raw["item"],
                "quantity": qty_str(qty),
                "unit_price": money_str(price),
                "amount": money_str(amount),
            })
        return lines, total

    def receive_invoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "invoice_no", "vendor", "lines", "currency")
        idem = payload.get("idempotency_key")
        if idem:
            for inv in self.store.collection("invoices"):
                if inv.get("idempotency_key") == idem:
                    return {"invoice": inv, "deduplicated": True}
        invoice_no = payload["invoice_no"]
        vendor = payload["vendor"]
        for inv in self.store.collection("invoices"):
            if inv["invoice_no"] == invoice_no and inv["vendor"] == vendor:
                raise ServiceError(
                    "duplicate", f"发票号已存在: {vendor}/{invoice_no}", status=409)

        contract = None
        if payload.get("contract_id"):
            contract = self._find("contracts", payload["contract_id"], "合同")
        race_day = payload.get("race_day") or (contract and contract["race_day"])
        cost_center = payload.get("cost_center") or (contract and contract["cost_center"])
        if not race_day or not cost_center:
            raise ServiceError("validation", "发票需要 contract_id 或 race_day + cost_center")
        race_day = _check_date(race_day, "race_day")
        currency = self._check_currency(payload["currency"])
        business_date = _check_date(payload.get("business_date", today()), "business_date")

        lines, total = self._normalize_invoice_lines(payload["lines"])

        invoice = {
            "id": self.store.next_id("inv"),
            "invoice_no": invoice_no,
            "vendor": vendor,
            "contract_id": payload.get("contract_id"),
            "race_day": race_day,
            "cost_center": cost_center,
            "currency": currency,
            "lines": lines,
            "amount": money_str(total),
            "status": "received",
            "origin_id": None,
            "relation": None,
            "root_id": None,
            "business_date": business_date,
            "received_at": utc_now(),
            "idempotency_key": idem,
            "paid_amount": money_str(ZERO),
            "reversed_amount": money_str(ZERO),
        }
        invoice["root_id"] = invoice["id"]
        self.store.append("invoices", invoice)
        self._post_payable(invoice, consume=True)
        for line in lines:
            self._check_overspend(race_day, cost_center, line["item"], currency)
        self.store.save()
        return {"invoice": invoice, "deduplicated": False}

    def _post_payable(self, invoice: dict[str, Any], *, consume: bool) -> None:
        """发票确认应付。consume=True 时依次消耗预提、承诺（仅首次开票）。"""
        for line in invoice["lines"]:
            amount = Decimal(line["amount"])
            qty = Decimal(line["quantity"])
            price = Decimal(line["unit_price"])
            item = line["item"]
            if consume:
                entries = self._entries(
                    race_day=invoice["race_day"],
                    cost_center=invoice["cost_center"], item=item)
                buckets = ledger.fold_buckets(entries)
                accrued_open = max(buckets["accrued"], ZERO)
                from_accrual = min(amount, accrued_open)
                if from_accrual > ZERO:
                    # 按预提未结数量的比例冲减，保持数量/金额口径一致
                    accrued_qty_open = sum(
                        (Decimal(e["quantity"]) for e in entries
                         if e["bucket"] == "accrued" and e.get("quantity") is not None),
                        ZERO,
                    )
                    consumed_qty = to_qty(
                        accrued_qty_open * from_accrual / accrued_open)
                    self._post_entry(
                        kind="payable", bucket="accrued", side="expense",
                        amount=-from_accrual,
                        race_day=invoice["race_day"], cost_center=invoice["cost_center"],
                        item=item, currency=invoice["currency"],
                        business_date=invoice["business_date"],
                        source_type="invoice", source_id=invoice["id"],
                        contract_id=invoice.get("contract_id"),
                        quantity=-consumed_qty,
                        note="应付消耗预提",
                    )
                remainder = amount - from_accrual
                if remainder > ZERO:
                    committed_open = max(buckets["committed"], ZERO)
                    from_commitment = min(remainder, committed_open)
                    if from_commitment > ZERO:
                        self._post_entry(
                            kind="payable", bucket="committed", side="expense",
                            amount=-from_commitment,
                            race_day=invoice["race_day"],
                            cost_center=invoice["cost_center"],
                            item=item, currency=invoice["currency"],
                            business_date=invoice["business_date"],
                            source_type="invoice", source_id=invoice["id"],
                            contract_id=invoice.get("contract_id"),
                            note="应付消耗承诺",
                        )
            self._post_entry(
                kind="payable", bucket="payable", side="expense", amount=amount,
                race_day=invoice["race_day"], cost_center=invoice["cost_center"],
                item=item, currency=invoice["currency"],
                business_date=invoice["business_date"],
                source_type="invoice", source_id=invoice["id"],
                contract_id=invoice.get("contract_id"),
                quantity=qty, unit_price=price,
                note=f"发票 {invoice['invoice_no']} 确认应付",
            )

    def _invoice_open_amount(self, invoice: dict[str, Any]) -> Decimal:
        return (
            Decimal(invoice["amount"])
            - Decimal(invoice["reversed_amount"])
            - Decimal(invoice["paid_amount"])
        )

    def _invoice_remaining_reversible(self, invoice: dict[str, Any]) -> Decimal:
        return Decimal(invoice["amount"]) - Decimal(invoice["reversed_amount"])

    def split_invoice(self, invoice_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """拆票：原票转为 split，子票关联 origin_id，金额合计必须等于原票未冲销余额。"""
        original = self._find("invoices", invoice_id, "发票")
        if original["status"] != "received":
            raise ServiceError(
                "conflict", f"只有未支付未冲销的发票可以拆票(当前状态 {original['status']})",
                status=409)
        children_payload = payload.get("children")
        if not isinstance(children_payload, list) or len(children_payload) < 2:
            raise ServiceError("validation", "拆票至少需要两张子票")

        target = self._invoice_remaining_reversible(original)
        children: list[dict[str, Any]] = []
        total = ZERO
        for child in children_payload:
            _require(child, "invoice_no", "lines")
            child_lines, subtotal = self._normalize_invoice_lines(child["lines"])
            total += subtotal
            children.append({"invoice_no": child["invoice_no"],
                             "lines": child_lines, "amount": subtotal})
        if total != target:
            raise ServiceError(
                "validation",
                f"子票合计 {money_str(total)} 必须等于原票未冲销金额 {money_str(target)}")
        # 落账前完成全部校验（子票单号唯一性），避免部分失败
        seen_nos: set[str] = set()
        for child in children:
            no = child["invoice_no"]
            if no in seen_nos:
                raise ServiceError("duplicate", f"子票发票号重复: {no}", status=409)
            seen_nos.add(no)
            for inv in self.store.collection("invoices"):
                if inv["invoice_no"] == no and inv["vendor"] == original["vendor"]:
                    raise ServiceError(
                        "duplicate",
                        f"发票号已存在: {original['vendor']}/{no}", status=409)

        # 原票应付全额转出
        self._reverse_payable(original, target, note="拆票转出")
        original["status"] = "split"
        original["reversed_amount"] = money_str(
            Decimal(original["reversed_amount"]) + target)

        created = []
        for child in children:
            invoice = self._child_invoice(
                original, child["invoice_no"], child["lines"], relation="split",
                business_date=payload.get("business_date"))
            self._post_payable(invoice, consume=False)
            created.append(invoice)
        self.store.save()
        return {"original": original, "children": created}

    def red_letter_invoice(self, invoice_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """红字发票：负数子票关联原单，累计冲销不得超过原票金额。"""
        original = self._find("invoices", invoice_id, "发票")
        if original["status"] in ("split", "superseded", "reversed"):
            raise ServiceError(
                "conflict", f"发票状态 {original['status']} 不允许红字冲销", status=409)
        _require(payload, "invoice_no")
        remaining = self._invoice_remaining_reversible(original)
        amount = to_money(payload["amount"], "amount") if payload.get("amount") is not None else remaining
        if amount <= ZERO:
            raise ServiceError("validation", "红字金额必须为正")
        if amount > remaining:
            raise ServiceError(
                "validation",
                f"红字金额 {money_str(amount)} 超过原票可冲销余额 {money_str(remaining)}")

        # 按比例生成负数子票行（与冲销分录同源分摊，合计精确一致）
        shares = _prorate_amounts(original["lines"], amount)
        lines = []
        for line, share in zip(original["lines"], shares):
            line_amount = Decimal(line["amount"])
            ratio = share / line_amount if line_amount != ZERO else ZERO
            lines.append({
                "item": line["item"],
                "quantity": qty_str(-to_qty(Decimal(line["quantity"]) * ratio)),
                "unit_price": line["unit_price"],
                "amount": money_str(-share),
            })
        child = self._child_invoice(original, payload["invoice_no"], lines,
                                    relation="red_letter",
                                    business_date=payload.get("business_date"))
        self._reverse_payable(original, amount, note=f"红字冲销 {child['invoice_no']}",
                              child=child)
        original["reversed_amount"] = money_str(
            Decimal(original["reversed_amount"]) + amount)
        if Decimal(original["reversed_amount"]) == Decimal(original["amount"]):
            original["status"] = "reversed"
        result: dict[str, Any] = {"original": original, "red_letter": child}
        open_after = self._invoice_open_amount(original)
        if open_after < ZERO:
            result["refund_due"] = money_str(-open_after)
            result["warning"] = "原票已支付，红字金额形成应退余额"
        self.store.save()
        return result

    def reissue_invoice(self, invoice_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """重新开票：红字冲销原票剩余应付 + 新票关联原单（relation=reissue）。

        先校验全部输入再落账，避免冲红后新票校验失败留下不一致状态。
        """
        original = self._find("invoices", invoice_id, "发票")
        if original["status"] in ("split", "superseded", "reversed"):
            raise ServiceError(
                "conflict", f"发票状态 {original['status']} 不允许重新开票", status=409)
        _require(payload, "invoice_no", "lines")
        # 先解析新票行（含唯一性校验），全部合法后再冲红
        new_lines, _ = self._normalize_invoice_lines(payload["lines"])
        for inv in self.store.collection("invoices"):
            if inv["invoice_no"] == payload["invoice_no"] and inv["vendor"] == original["vendor"]:
                raise ServiceError(
                    "duplicate",
                    f"发票号已存在: {original['vendor']}/{payload['invoice_no']}",
                    status=409)
        remaining = self._invoice_remaining_reversible(original)

        # 1) 红字冲销原票剩余（与冲销分录同源分摊）
        shares = _prorate_amounts(original["lines"], remaining)
        red_lines = []
        for line, share in zip(original["lines"], shares):
            line_amount = Decimal(line["amount"])
            ratio = share / line_amount if line_amount != ZERO else ZERO
            red_lines.append({
                "item": line["item"],
                "quantity": qty_str(-to_qty(Decimal(line["quantity"]) * ratio)),
                "unit_price": line["unit_price"],
                "amount": money_str(-share),
            })
        red = self._child_invoice(
            original, self._unique_invoice_no(original["vendor"],
                                              f"{original['invoice_no']}-RED"),
            red_lines, relation="red_letter",
            business_date=payload.get("business_date"))
        self._reverse_payable(original, remaining, note="重新开票冲销原票", child=red)
        original["reversed_amount"] = money_str(
            Decimal(original["reversed_amount"]) + remaining)
        original["status"] = "superseded"

        # 2) 新票
        new_invoice = self._child_invoice(
            original, payload["invoice_no"],
            [dict(line) for line in new_lines],
            relation="reissue", business_date=payload.get("business_date"))
        self._post_payable(new_invoice, consume=False)
        for line in new_lines:
            self._check_overspend(
                new_invoice["race_day"], new_invoice["cost_center"],
                line["item"], new_invoice["currency"])
        self.store.save()
        return {"original": original, "red_letter": red, "reissued": new_invoice}

    def _unique_invoice_no(self, vendor: str, base: str) -> str:
        """生成不与现有发票冲突的单号：base、base-2、base-3……"""
        existing = {inv["invoice_no"] for inv in self.store.collection("invoices")
                    if inv["vendor"] == vendor}
        if base not in existing:
            return base
        suffix = 2
        while f"{base}-{suffix}" in existing:
            suffix += 1
        return f"{base}-{suffix}"

    def _child_invoice(
        self, original: dict[str, Any], invoice_no: str,
        lines: list[dict[str, Any]], relation: str,
        business_date: str | None = None,
    ) -> dict[str, Any]:
        for inv in self.store.collection("invoices"):
            if inv["invoice_no"] == invoice_no and inv["vendor"] == original["vendor"]:
                raise ServiceError(
                    "duplicate", f"发票号已存在: {original['vendor']}/{invoice_no}",
                    status=409)
        total = sum((Decimal(l["amount"]) for l in lines), ZERO)
        child = {
            "id": self.store.next_id("inv"),
            "invoice_no": invoice_no,
            "vendor": original["vendor"],
            "contract_id": original.get("contract_id"),
            "race_day": original["race_day"],
            "cost_center": original["cost_center"],
            "currency": original["currency"],
            "lines": lines,
            "amount": money_str(total),
            "status": "received",
            "origin_id": original["id"],
            "relation": relation,
            "root_id": original["root_id"],
            "business_date": _check_date(business_date, "business_date")
            if business_date else today(),
            "received_at": utc_now(),
            "idempotency_key": None,
            "paid_amount": money_str(ZERO),
            "reversed_amount": money_str(ZERO),
        }
        self.store.append("invoices", child)
        return child

    def _reverse_payable(
        self, invoice: dict[str, Any], amount: Decimal, *,
        note: str, child: dict[str, Any] | None = None,
    ) -> None:
        """按原票行比例冲减应付桶（红字/拆票/重开共用），分摊合计与 amount 精确相等。"""
        shares = _prorate_amounts(invoice["lines"], amount)
        source_id = child["id"] if child else invoice["id"]
        business_date = (child or invoice)["business_date"]
        for line, share in zip(invoice["lines"], shares):
            if share == ZERO:
                continue
            line_amount = Decimal(line["amount"])
            ratio = share / line_amount if line_amount != ZERO else ZERO
            self._post_entry(
                kind="payable_reversal", bucket="payable", side="expense",
                amount=-share,
                race_day=invoice["race_day"], cost_center=invoice["cost_center"],
                item=line["item"], currency=invoice["currency"],
                business_date=business_date,
                source_type="invoice", source_id=source_id,
                contract_id=invoice.get("contract_id"),
                quantity=-to_qty(Decimal(line["quantity"]) * ratio),
                unit_price=Decimal(line["unit_price"]),
                note=note,
            )

    def invoice_lineage(self, invoice_id: str) -> dict[str, Any]:
        """发票血缘：根单 + 全部拆票/红字/重开子单。"""
        invoice = self._find("invoices", invoice_id, "发票")
        root_id = invoice["root_id"]
        family = [inv for inv in self.store.collection("invoices")
                  if inv["root_id"] == root_id]
        family.sort(key=lambda inv: (inv["received_at"], inv["id"]))
        return {
            "root_id": root_id,
            "invoices": [
                {
                    "id": inv["id"], "invoice_no": inv["invoice_no"],
                    "relation": inv["relation"], "origin_id": inv["origin_id"],
                    "amount": inv["amount"], "status": inv["status"],
                    "business_date": inv["business_date"],
                }
                for inv in family
            ],
        }

    # ------------------------------------------------------------------
    # 支付
    # ------------------------------------------------------------------

    def receive_payment(self, payload: dict[str, Any]) -> dict[str, Any]:
        """支付回执：按 idempotency_key 幂等；乱序到达按确定性顺序分摊，绝不重复付款。"""
        _require(payload, "idempotency_key", "vendor", "amount", "currency")
        idem = payload["idempotency_key"]
        for existing in self.store.collection("payments"):
            if existing["idempotency_key"] == idem:
                return {"payment": existing, "deduplicated": True}

        vendor = payload["vendor"]
        amount = to_money(payload["amount"], "amount")
        if amount <= ZERO:
            raise ServiceError("validation", "支付金额必须为正")
        currency = self._check_currency(payload["currency"])
        business_date = _check_date(payload.get("business_date", today()), "business_date")
        direction = payload.get("direction", "outbound")

        if direction == "inbound":
            payment = self._receive_inbound(
                payload, amount, currency, business_date, idem)
            self.store.save()
            return {"payment": payment, "deduplicated": False}

        # 找出该供应商全部未结清发票，按 (发票日, 单号) 确定性排序
        open_invoices = [
            inv for inv in self.store.collection("invoices")
            if inv["vendor"] == vendor
            and inv["currency"] == currency
            and inv["status"] in ("received", "partially_paid")
            and self._invoice_open_amount(inv) > ZERO
        ]
        open_invoices.sort(key=lambda inv: (inv["business_date"], inv["id"]))

        remaining = amount
        allocations: list[dict[str, Any]] = []
        for inv in open_invoices:
            if remaining <= ZERO:
                break
            open_amount = self._invoice_open_amount(inv)
            take = min(remaining, open_amount)
            self._apply_payment_to_invoice(inv, take, business_date,
                                           payment_source=idem)
            allocations.append({"invoice_id": inv["id"],
                                "invoice_no": inv["invoice_no"],
                                "amount": money_str(take)})
            remaining -= take

        payment = {
            "id": self.store.next_id("pay"),
            "idempotency_key": idem,
            "direction": "outbound",
            "vendor": vendor,
            "amount": money_str(amount),
            "currency": currency,
            "business_date": business_date,
            "received_at": utc_now(),
            "allocations": allocations,
            "credit_amount": money_str(remaining),
        }
        self.store.append("payments", payment)
        self.store.save()
        result: dict[str, Any] = {"payment": payment, "deduplicated": False}
        if remaining > ZERO:
            result["warning"] = f"超出未结应付的部分 {money_str(remaining)} 记为供应商预付余额"
        return result

    def _apply_payment_to_invoice(
        self, invoice: dict[str, Any], amount: Decimal,
        business_date: str, *, payment_source: str,
    ) -> None:
        """把支付按发票行金额比例分摊到行级分录，并更新发票状态。"""
        shares = _prorate_amounts(invoice["lines"], amount)
        for line, line_amount in zip(invoice["lines"], shares):
            if line_amount == ZERO:
                continue
            for bucket, kind_amount in (("payable", -line_amount), ("paid", line_amount)):
                self._post_entry(
                    kind="payment", bucket=bucket, side="expense",
                    amount=kind_amount,
                    race_day=invoice["race_day"], cost_center=invoice["cost_center"],
                    item=line["item"], currency=invoice["currency"],
                    business_date=business_date,
                    source_type="payment", source_id=payment_source,
                    contract_id=invoice.get("contract_id"),
                    note=f"支付发票 {invoice['invoice_no']}",
                )
        invoice["paid_amount"] = money_str(Decimal(invoice["paid_amount"]) + amount)
        open_after = self._invoice_open_amount(invoice)
        if Decimal(invoice["reversed_amount"]) == Decimal(invoice["amount"]):
            invoice["status"] = "reversed"
        elif open_after <= ZERO:
            invoice["status"] = "paid"
        else:
            invoice["status"] = "partially_paid"

    def _receive_inbound(
        self, payload: dict[str, Any], amount: Decimal,
        currency: str, business_date: str, idem: str,
    ) -> dict[str, Any]:
        """赞助款到账：消耗赞助承诺，记收入到账。"""
        _require(payload, "contract_id")
        contract = self._find("contracts", payload["contract_id"], "赞助合同")
        if contract["side"] != "income":
            raise ServiceError("validation", "inbound 支付需要赞助合同")
        item = payload.get("item")
        if not item:
            version = self._version_for(contract, business_date) or contract["versions"][-1]
            item = version["lines"][0]["item"]
        buckets = self._line_buckets(contract, item)
        consume = min(amount, max(buckets["income_committed"], ZERO))
        if consume > ZERO:
            self._post_entry(
                kind="receipt", bucket="income_committed", side="income",
                amount=-consume, race_day=contract["race_day"],
                cost_center=contract["cost_center"], item=item,
                currency=currency, business_date=business_date,
                source_type="payment", source_id=idem,
                contract_id=contract["id"], note="赞助到账消耗权益承诺",
            )
        self._post_entry(
            kind="receipt", bucket="income_received", side="income", amount=amount,
            race_day=contract["race_day"], cost_center=contract["cost_center"],
            item=item, currency=currency, business_date=business_date,
            source_type="payment", source_id=idem,
            contract_id=contract["id"], note="赞助款到账",
        )
        payment = {
            "id": self.store.next_id("pay"),
            "idempotency_key": idem,
            "direction": "inbound",
            "vendor": payload["vendor"],
            "contract_id": contract["id"],
            "amount": money_str(amount),
            "currency": currency,
            "business_date": business_date,
            "received_at": utc_now(),
            "allocations": [{"contract_id": contract["id"], "item": item,
                             "amount": money_str(amount)}],
            "credit_amount": money_str(ZERO),
        }
        self.store.append("payments", payment)
        return payment

    # ------------------------------------------------------------------
    # 超支审批队列
    # ------------------------------------------------------------------

    def _check_overspend(
        self, race_day: str, cost_center: str, item: str, currency: str,
    ) -> None:
        budget = self._budget_version(race_day, cost_center)
        if not budget:
            return
        budget_line = next((l for l in budget["lines"] if l["item"] == item), None)
        if not budget_line:
            return
        budget_amount = mul_money_qty(
            Decimal(budget_line["unit_price"]), Decimal(budget_line["planned_quantity"]))
        entries = self._entries(race_day=race_day, cost_center=cost_center, item=item)
        buckets = ledger.fold_buckets(entries)
        obligated = (
            buckets["committed"] + buckets["accrued"]
            + buckets["payable"] + buckets["paid"]
        )
        over = obligated - budget_amount
        existing = next(
            (a for a in self.store.collection("approvals")
             if a["race_day"] == race_day and a["cost_center"] == cost_center
             and a["item"] == item and a["status"] == "pending"),
            None,
        )
        if over > ZERO:
            if existing:
                existing["projected_amount"] = money_str(obligated)
                existing["over_amount"] = money_str(over)
            else:
                now = datetime.fromisoformat(utc_now())
                self.store.append("approvals", {
                    "id": self.store.next_id("apr"),
                    "kind": "overspend",
                    "race_day": race_day,
                    "cost_center": cost_center,
                    "item": item,
                    "currency": currency,
                    "budget_amount": money_str(budget_amount),
                    "projected_amount": money_str(obligated),
                    "over_amount": money_str(over),
                    "status": "pending",
                    "created_at": now.isoformat(timespec="seconds"),
                    "deadline": (now + timedelta(hours=APPROVAL_DEADLINE_HOURS))
                    .isoformat(timespec="seconds"),
                    "decided_at": None,
                    "decision_note": None,
                })
        elif existing:
            existing["status"] = "resolved"
            existing["decided_at"] = utc_now()
            existing["decision_note"] = "占用回落至预算内，自动关闭"

    @staticmethod
    def _approval_view(approval: dict[str, Any]) -> dict[str, Any]:
        view = dict(approval)
        if (view["status"] == "pending"
                and view["deadline"] < utc_now()):
            view["status"] = "expired"
        return view

    def approval_queue(self, status: str | None = None) -> dict[str, Any]:
        approvals = [self._approval_view(a) for a in self.store.collection("approvals")]
        if status:
            approvals = [a for a in approvals if a["status"] == status]
        approvals.sort(key=lambda a: (a["deadline"], a["id"]))
        return {"approvals": approvals}

    def decide_approval(self, approval_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        _require(payload, "decision")
        approval = self._find("approvals", approval_id, "审批")
        view = self._approval_view(approval)
        if view["status"] != "pending":
            raise ServiceError(
                "conflict", f"审批当前状态为 {view['status']}，不能决策", status=409)
        decision = payload["decision"]
        if decision not in ("approved", "rejected"):
            raise ServiceError("validation", "decision 必须是 approved 或 rejected")
        approval["status"] = decision
        approval["decided_at"] = utc_now()
        approval["decision_note"] = payload.get("note")
        self.store.save()
        return {"approval": approval}

    # ------------------------------------------------------------------
    # 查询：账层视图 / 预算快照 / 偏差分解
    # ------------------------------------------------------------------

    def ledger_view(
        self, race_day: str | None = None, cost_center: str | None = None,
    ) -> dict[str, Any]:
        entries = self._entries(race_day=race_day, cost_center=cost_center)
        by_key: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        for entry in entries:
            key = (entry["race_day"], entry["cost_center"], entry["item"], entry["currency"])
            by_key.setdefault(key, []).append(entry)
        lines = []
        for (rd, cc, item, currency), group in sorted(by_key.items()):
            buckets = ledger.fold_buckets(group)
            lines.append({
                "race_day": rd, "cost_center": cc, "item": item, "currency": currency,
                "committed_open": money_str(buckets["committed"]),
                "accrued_open": money_str(buckets["accrued"]),
                "payable_open": money_str(buckets["payable"]),
                "paid": money_str(buckets["paid"]),
                "released": money_str(ledger.released_total(group)),
                "income_committed_open": money_str(buckets["income_committed"]),
                "income_received": money_str(buckets["income_received"]),
                "income": money_str(buckets["income"]),
                "refund": money_str(buckets["refund"]),
            })
        return {"lines": lines}

    def snapshot(
        self, race_day: str, cost_center: str,
        as_of: str | None = None, received_before: str | None = None,
    ) -> dict[str, Any]:
        """按赛事日 + 成本中心重建"当时"的预算快照。

        as_of          业务时间上限（含）
        received_before 接收时间上限（含）——只使用当时已到达的记录
        """
        as_of = as_of or today()
        received_before = received_before or utc_now()
        _check_date(as_of, "as_of")

        budget = self._budget_version(
            race_day, cost_center, as_of=as_of, received_before=received_before)
        entries = self._entries(
            race_day=race_day, cost_center=cost_center,
            as_of=as_of, received_before=received_before)

        by_item: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            by_item.setdefault(entry["item"], []).append(entry)

        items: dict[str, Any] = {}
        budget_lines = {l["item"]: l for l in (budget["lines"] if budget else [])}
        for item in sorted(set(by_item) | set(budget_lines)):
            group = by_item.get(item, [])
            buckets = ledger.fold_buckets(group)
            currency = group[0]["currency"] if group else (
                budget["currency"] if budget else "CNY")
            budget_amount = ZERO
            if item in budget_lines:
                budget_amount = mul_money_qty(
                    Decimal(budget_lines[item]["unit_price"]),
                    Decimal(budget_lines[item]["planned_quantity"]))
            obligated = (buckets["committed"] + buckets["accrued"]
                         + buckets["payable"] + buckets["paid"])
            items[item] = {
                "currency": currency,
                "budget_amount": money_str(budget_amount),
                "committed_open": money_str(buckets["committed"]),
                "accrued_open": money_str(buckets["accrued"]),
                "payable_open": money_str(buckets["payable"]),
                "paid": money_str(buckets["paid"]),
                "released": money_str(ledger.released_total(group)),
                "obligated": money_str(obligated),
                "remaining": money_str(budget_amount - obligated),
                "overspent": obligated > budget_amount,
            }

        all_buckets = ledger.fold_buckets(entries)
        cash = {
            "registration_income": money_str(all_buckets["income"]),
            "refunds": money_str(all_buckets["refund"]),
            "sponsor_received": money_str(all_buckets["income_received"]),
            "paid_out": money_str(all_buckets["paid"]),
            "net_cash": money_str(
                all_buckets["income"] - all_buckets["refund"]
                + all_buckets["income_received"] - all_buckets["paid"]),
        }
        return {
            "race_day": race_day,
            "cost_center": cost_center,
            "as_of": as_of,
            "received_before": received_before,
            "headcount": self.headcount(
                race_day, as_of=as_of, received_before=received_before),
            "budget_version": budget["version"] if budget else None,
            "items": items,
            "cash": cash,
        }

    # ------------------------------------------------------------------
    # 偏差分解：人数(数量) / 单价 / 合同变更
    # ------------------------------------------------------------------

    def variance(
        self, race_day: str, cost_center: str,
        as_of: str | None = None, received_before: str | None = None,
    ) -> dict[str, Any]:
        """把每个项目的总偏差精确分解为三个成因。

        链式分解（对每项目）：
          A0  基线预算金额          = p0 × q0(h0)
          A_h 基线条款 × 当前人数   = p0 × q0(h1)
          A_c 当前合同条款 × 当前人数
          A_q 当前合同价 × 实际完成量
          A2  实际金额（预提+应付+已付，净红字）

          人数/数量 = (A_h - A0) + (A_q - A_c)
          合同变更  = A_c - A_h
          单价      = A2 - A_q
          合计      = A2 - A0  （恒等）
        """
        as_of = as_of or today()
        received_before = received_before or utc_now()
        _check_date(as_of, "as_of")

        baseline_budget = self._budget_version(
            race_day, cost_center, baseline=True)
        current_budget = self._budget_version(
            race_day, cost_center, as_of=as_of, received_before=received_before)
        if not baseline_budget:
            raise ServiceError("not_found", "该赛事日+成本中心没有预算版本", status=404)

        h0 = self.headcount(race_day, as_of=baseline_budget["business_date"],
                            received_before=received_before)
        h1 = self.headcount(race_day, as_of=as_of, received_before=received_before)

        contracts = [c for c in self.store.collection("contracts")
                     if c["race_day"] == race_day and c["cost_center"] == cost_center
                     and c["side"] == "expense"]

        baseline_date = baseline_budget["business_date"]
        item_names: set[str] = set()
        for budget in (baseline_budget, current_budget):
            if budget:
                item_names |= {l["item"] for l in budget["lines"]}
        for contract in contracts:
            for version in contract["versions"]:
                if version["effective_date"] <= as_of:
                    item_names |= {l["item"] for l in version["lines"]}
        for entry in self._entries(race_day=race_day, cost_center=cost_center,
                                   as_of=as_of, received_before=received_before):
            if entry["side"] == "expense":
                item_names.add(entry["item"])

        items: dict[str, Any] = {}
        totals = {"baseline": ZERO, "actual": ZERO, "quantity": ZERO,
                  "price": ZERO, "contract_change": ZERO, "total": ZERO}
        for item in sorted(item_names):
            result = self._variance_for_item(
                item, contracts, baseline_budget, current_budget,
                baseline_date, as_of, h0, h1, race_day, cost_center,
                received_before)
            items[item] = result
            totals["baseline"] += Decimal(result["baseline_amount"])
            totals["actual"] += Decimal(result["actual_amount"])
            totals["quantity"] += Decimal(result["variance"]["quantity"])
            totals["price"] += Decimal(result["variance"]["price"])
            totals["contract_change"] += Decimal(result["variance"]["contract_change"])
            totals["total"] += Decimal(result["variance"]["total"])

        return {
            "race_day": race_day,
            "cost_center": cost_center,
            "as_of": as_of,
            "headcount_baseline": h0,
            "headcount_current": h1,
            "items": items,
            "totals": {k: money_str(v) for k, v in totals.items()},
        }

    def _variance_for_item(
        self, item: str, contracts: list[dict[str, Any]],
        baseline_budget: dict[str, Any], current_budget: dict[str, Any] | None,
        baseline_date: str, as_of: str, h0: int, h1: int,
        race_day: str, cost_center: str, received_before: str,
    ) -> dict[str, Any]:
        def terms_from_contracts(on_date: str) -> list[tuple[Decimal, Decimal, str]]:
            """(单价, 基准数量, 数量基准) 列表，聚合所有覆盖该项目的合同行。"""
            terms = []
            for contract in contracts:
                version = self._version_for(contract, on_date)
                if not version:
                    continue
                for line in version["lines"]:
                    if line["item"] != item:
                        continue
                    terms.append((
                        Decimal(line["unit_price"]),
                        Decimal(line["per_head_factor"] or line["fixed_quantity"] or "0"),
                        line["quantity_basis"],
                    ))
            return terms

        def eval_terms(terms: list[tuple[Decimal, Decimal, str]], head: int) -> Decimal:
            total = ZERO
            for price, qty_base, basis in terms:
                qty = qty_base * head if basis == "per_head" else qty_base
                total += mul_money_qty(price, to_qty(qty))
            return total

        def budget_line(budget: dict[str, Any] | None) -> dict[str, Any] | None:
            if not budget:
                return None
            return next((l for l in budget["lines"] if l["item"] == item), None)

        def terms_from_budget(budget: dict[str, Any] | None) -> list[tuple[Decimal, Decimal, str]]:
            """预算行转条款。per_head 行的 planned_quantity 是预算日人数下的计划量，
            需要按预算日人数换算成每人系数。"""
            line = budget_line(budget)
            if not line:
                return []
            price = Decimal(line["unit_price"])
            planned = Decimal(line["planned_quantity"])
            if line.get("quantity_basis", "fixed") == "per_head":
                h_ref = self.headcount(
                    race_day, as_of=budget["business_date"],
                    received_before=received_before)
                if h_ref > 0:
                    return [(price, planned / h_ref, "per_head")]
            return [(price, planned, "fixed")]

        # 基线条款：优先合同（基线日生效），否则基线预算行
        terms0 = terms_from_contracts(baseline_date)
        if not terms0:
            terms0 = terms_from_budget(baseline_budget)
        # 当前条款：优先合同（as_of 生效），否则当前预算行，否则基线条款
        terms1 = terms_from_contracts(as_of)
        if not terms1:
            terms1 = terms_from_budget(current_budget)
        if not terms1:
            terms1 = terms0

        a0 = eval_terms(terms0, h0)
        a_h = eval_terms(terms0, h1)
        a_c = eval_terms(terms1, h1)

        entries = [e for e in self._entries(
            race_day=race_day, cost_center=cost_center, item=item,
            as_of=as_of, received_before=received_before) if e["side"] == "expense"]
        q2, a2 = ledger.actuals(entries)

        # 当前条款单价（加权），用于实际量的价格基准
        q1 = ZERO
        for price, qty_base, basis in terms1:
            q1 += qty_base * h1 if basis == "per_head" else qty_base
        if q1 > ZERO:
            p1 = a_c / to_qty(q1)
        elif terms1:
            p1 = terms1[0][0]
        elif q2 > ZERO:
            p1 = a2 / q2
        else:
            p1 = ZERO
        a_q = mul_money_qty(p1, q2)

        headcount_driven = a_h - a0
        completion_driven = a_q - a_c
        quantity_var = headcount_driven + completion_driven
        price_var = a2 - a_q
        contract_var = a_c - a_h
        total_var = a2 - a0

        return {
            "baseline_amount": money_str(a0),
            "actual_amount": money_str(a2),
            "actual_quantity": qty_str(q2),
            "variance": {
                "quantity": money_str(quantity_var),
                "price": money_str(price_var),
                "contract_change": money_str(contract_var),
                "total": money_str(total_var),
            },
            "quantity_split": {
                "headcount_driven": money_str(headcount_driven),
                "completion_driven": money_str(completion_driven),
            },
        }
