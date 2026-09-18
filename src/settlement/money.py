"""金额、精度与时间工具。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# reference/domain.json: quantity_precision = 6
QTY_QUANT = Decimal("0.000001")
MONEY_QUANT = Decimal("0.01")
ZERO = Decimal("0")


def D(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        raise ValueError("金额不能为空")
    return Decimal(str(value))


def money(value: Any) -> Decimal:
    return D(value).quantize(MONEY_QUANT)


def qty(value: Any) -> Decimal:
    return D(value).quantize(QTY_QUANT)


def parse_time(value: Any) -> datetime:
    """解析 ISO8601；裸日期视为当天 UTC 0 点。"""
    if isinstance(value, datetime):
        dt = value
    else:
        if not isinstance(value, str) or not value:
            raise ValueError("时间必须是 ISO8601 字符串")
        text = value.strip().replace("Z", "+00:00")
        if len(text) == 10:
            text += "T00:00:00+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def to_json(value: Any) -> Any:
    if isinstance(value, Decimal):
        # 金额保留两位输出为数字，数量超过两位时保留有效小数
        if value == value.quantize(MONEY_QUANT):
            return float(value.quantize(MONEY_QUANT))
        return float(value)
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    return value
