"""严格字面量解析：拒绝任何会被 ``int()`` **静默修好**的值。

为什么这条要单独立一个模块：``int(-100900000002.75)`` 返回
``-100900000002`` —— 源标识被截断后，会拿着一个**并不存在**的群 id 去授权、
查库、发 Telegram。截断掉的 0.75 谁都看不见，于是这看起来像一次成功的操作，
实际上查的根本是另一个群。凡是要变成标识、revision、额度或分页参数的外部
原始值，都必须先过这里。
"""

from __future__ import annotations

import math
import re
from typing import Any

#: 允许的整数字面量：**完整**十进制，可带正负号。不接受小数点、指数、
#: 下划线分隔或十六进制 —— 它们都会在某些转换路径下被悄悄改值。
_INT_LITERAL = re.compile(r"^[+-]?\d+$")

#: Telegram chat/user id 的取值范围（64 位有符号整数）。
TELEGRAM_ID_MIN = -(2**63)
TELEGRAM_ID_MAX = 2**63 - 1


class StrictValueError(ValueError):
    """外部字面量无法**无损**转换成整数。"""


def strict_int(
    value: Any,
    *,
    field: str = "value",
    minimum: int | None = None,
    maximum: int | None = None,
    bounded: bool = True,
) -> int:
    """把外部值解析成整数，任何可能改值的形态都拒绝。

    只接受两种形态：真正的 Python ``int``（排除 ``bool``），以及**整串**都是
    十进制数字的字符串。浮点、指数写法、``nan``/``inf``、``"1_000"``、
    ``"0x10"`` 全部拒绝 —— 它们要么会被截断，要么是压根不同的表示。

    ``bounded=True``（默认）额外按 **64 位有符号整数** 卡上下界：落进 SQLite
    的每个整数都是 64 位，超界值在写入时会变成另一个值（或者直接写失败），
    两种都不是「用户写的东西」。
    """

    if isinstance(value, bool):
        raise StrictValueError(f"{field} 不能是布尔值。")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or not _INT_LITERAL.match(text):
            raise StrictValueError(f"{field} 必须是完整十进制整数。")
        parsed = int(text)
    else:
        # float / Decimal / None / 容器 …… 一律拒绝，哪怕它看起来是整数。
        raise StrictValueError(f"{field} 必须是整数。")

    if bounded and not (TELEGRAM_ID_MIN <= parsed <= TELEGRAM_ID_MAX):
        raise StrictValueError(f"{field} 超出 64 位整数范围。")
    if minimum is not None and parsed < minimum:
        raise StrictValueError(f"{field} 不能小于 {minimum}。")
    if maximum is not None and parsed > maximum:
        raise StrictValueError(f"{field} 不能大于 {maximum}。")
    return parsed


def strict_int_or_none(
    value: Any,
    field: str = "value",
    minimum: int | None = None,
    maximum: int | None = None,
    bounded: bool = True,
) -> int | None:
    """``None`` / 空串 = 未设置；其余一律走 :func:`strict_int`。

    区分这两者很重要：「没填」可以合法地表示不限额，「填了但是坏的」必须
    报错 —— 悄悄退回 ``None`` 等于把用户写的一条上限**静默删掉**，界面上
    还显示成「未设置」。
    """

    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return strict_int(
        value, field=field, minimum=minimum, maximum=maximum, bounded=bounded
    )


def strict_telegram_id(value: Any, field: str = "id") -> int:
    """Telegram 标识：严格整数 + 64 位范围。"""

    return strict_int(
        value, field=field, minimum=TELEGRAM_ID_MIN, maximum=TELEGRAM_ID_MAX
    )