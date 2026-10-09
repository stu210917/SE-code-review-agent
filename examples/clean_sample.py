# -*- coding: utf-8 -*-
"""订单金额计算模块（**良好实践示例**）。

这个文件用来对照 ``buggy_sample.py``：同样的业务量级，但结构清晰、
异常处理明确、依赖外部配置。审查 Agent 在它身上应该只给出少量提示
（甚至零问题）——这是检验「Agent 不会为了凑数而编造问题」的对照组。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

#: 折扣率的合法区间
MIN_DISCOUNT = Decimal("0")
MAX_DISCOUNT = Decimal("1")
CENT = Decimal("0.01")


class PricingError(ValueError):
    """定价参数非法时抛出。"""


@dataclass(frozen=True)
class OrderLine:
    """订单中的一行商品。"""

    sku: str
    unit_price: Decimal
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise PricingError(f"数量必须为正数，收到 {self.quantity}")
        if self.unit_price < 0:
            raise PricingError(f"单价不能为负，收到 {self.unit_price}")

    @property
    def subtotal(self) -> Decimal:
        """该行小计。"""
        return (self.unit_price * self.quantity).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PricingResult:
    """计价结果。"""

    subtotal: Decimal
    discount: Decimal
    total: Decimal


def normalize_discount(discount: Decimal | float | str | None) -> Decimal:
    """把外部传入的折扣率归一化到 [0, 1]。

    :raises PricingError: 折扣率非法或不是数字。
    """
    if discount is None:
        return MIN_DISCOUNT
    try:
        value = Decimal(str(discount))
    except (ArithmeticError, ValueError) as exc:
        raise PricingError(f"折扣率无法解析为数字：{discount!r}") from exc
    if not MIN_DISCOUNT <= value <= MAX_DISCOUNT:
        raise PricingError(f"折扣率必须位于 [{MIN_DISCOUNT}, {MAX_DISCOUNT}]，收到 {value}")
    return value


def calculate_total(lines: Iterable[OrderLine], discount: Decimal | float | str | None = None) -> PricingResult:
    """计算订单总额。

    :param lines: 订单行；空序列返回全 0 的结果。
    :param discount: 折扣率，``0.1`` 表示打九折。
    """
    subtotal = sum((line.subtotal for line in lines), start=Decimal("0"))
    rate = normalize_discount(discount)
    discount_amount = (subtotal * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    return PricingResult(
        subtotal=subtotal.quantize(CENT, rounding=ROUND_HALF_UP),
        discount=discount_amount,
        total=(subtotal - discount_amount).quantize(CENT, rounding=ROUND_HALF_UP),
    )


def top_skus(lines: Sequence[OrderLine], limit: int = 3) -> list[str]:
    """按小计金额返回金额最高的若干 SKU。"""
    if limit <= 0:
        return []
    ranked = sorted(lines, key=lambda line: line.subtotal, reverse=True)
    return [line.sku for line in ranked[:limit]]


def read_lines(rows: Iterable[dict[str, object]]) -> list[OrderLine]:
    """从原始字典行构造订单行，跳过无法解析的记录并记录日志。"""
    lines: list[OrderLine] = []
    for row in rows:
        try:
            lines.append(
                OrderLine(
                    sku=str(row["sku"]),
                    unit_price=Decimal(str(row["unit_price"])),
                    quantity=int(row["quantity"]),  # type: ignore[arg-type]
                )
            )
        except (KeyError, TypeError, ArithmeticError, PricingError) as exc:
            logger.warning("跳过非法订单行 %r：%s", row, exc)
    return lines
