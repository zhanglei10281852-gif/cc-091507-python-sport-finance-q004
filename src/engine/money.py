"""金额与数量的定点数处理。

所有金额使用 Decimal，序列化为字符串，避免浮点误差进入账本。
数量精度取自 reference/domain.json 的 quantity_precision（6 位）。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CENT = Decimal("0.01")
QUANTUM_QTY = Decimal("0.000001")
ZERO = Decimal("0")


class MoneyError(ValueError):
    """金额或数量无法解析。"""


def _to_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise MoneyError(f"{field} 不能是布尔值")
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise MoneyError(f"{field} 不是合法数值: {value!r}") from exc
    raise MoneyError(f"{field} 类型不支持: {type(value).__name__}")


def to_money(value: Any, field: str = "amount") -> Decimal:
    """解析并量化为分（0.01）。"""
    return _to_decimal(value, field).quantize(CENT, rounding=ROUND_HALF_UP)


def to_qty(value: Any, field: str = "quantity") -> Decimal:
    """解析并量化为数量精度（0.000001）。"""
    return _to_decimal(value, field).quantize(QUANTUM_QTY, rounding=ROUND_HALF_UP)


def money_str(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def qty_str(value: Decimal) -> str:
    quantized = value.quantize(QUANTUM_QTY, rounding=ROUND_HALF_UP)
    # 去掉多余的尾零，保持输出紧凑但仍是定点表示
    text = format(quantized.normalize(), "f")
    return text


def mul_money_qty(price: Decimal, qty: Decimal) -> Decimal:
    """单价 × 数量 → 金额（量化到分）。"""
    return (price * qty).quantize(CENT, rounding=ROUND_HALF_UP)
