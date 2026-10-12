"""严格字面量解析：拒绝任何会被 ``int()`` **静默修好**的值。

为什么这条要单独立一个模块：`int(-100900000002.75)` 返回
``-100900000002`` —— 源标识被截断后，会拿着一个**并不存在**的群 id 去授权、
查库、发 Telegram。截断掉的 0.75 谁都看不见，于是这看起来像一次成功的操作，
实际上查的根本是另一个群。凡是要变成标识、revision、额度或分页参数的外部
原始值，都必须先过这里。
"""

from __future__ import annotations

import math
import unittest

from bot.services.strict_values import (
    StrictValueError,
    strict_int,
    strict_int_or_none,
)


class StrictIntTests(unittest.TestCase):
    def test_accepts_real_integers_and_full_decimal_strings(self) -> None:
        self.assertEqual(strict_int(-1001234567890), -1001234567890)
        self.assertEqual(strict_int("-1001234567890"), -1001234567890)
        self.assertEqual(strict_int(0), 0)
        self.assertEqual(strict_int("42"), 42)
        # 前后空白允许：**串**仍被整串验证后才转换。
        self.assertEqual(strict_int("  7  "), 7)

    def test_rejects_floats_even_when_whole(self) -> None:
        for bad in (-100900000002.75, 3.0, -0.0, float(7)):
            with self.subTest(bad=bad):
                with self.assertRaises(StrictValueError):
                    strict_int(bad)

    def test_rejects_float_strings_including_exponent_form(self) -> None:
        for bad in ("-100900000002.75", "3.0", "1e3", "1E3", "-0.0", "nan", "inf"):
            with self.subTest(bad=bad):
                with self.assertRaises(StrictValueError):
                    strict_int(bad)

    def test_rejects_bool(self) -> None:
        # bool 是 int 的子类：不做显式排除时 ``int(True) == 1`` 会把
        # "enabled='false'" 变成 enabled=1。
        for bad in (True, False):
            with self.subTest(bad=bad):
                with self.assertRaises(StrictValueError):
                    strict_int(bad)

    def test_rejects_non_finite_and_out_of_range(self) -> None:
        for bad in (math.inf, -math.inf, math.nan):
            with self.subTest(bad=bad):
                with self.assertRaises(StrictValueError):
                    strict_int(bad)
        with self.assertRaises(StrictValueError):
            strict_int(2**200)
        with self.assertRaises(StrictValueError):
            strict_int(-(2**200))

    def test_rejects_junk(self) -> None:
        for bad in (None, "", "  ", "abc", "0x10", "1_000", [1], object()):
            with self.subTest(bad=bad):
                with self.assertRaises(StrictValueError):
                    strict_int(bad)

    def test_or_none_distinguishes_missing_from_invalid(self) -> None:
        self.assertIsNone(strict_int_or_none(None))
        self.assertIsNone(strict_int_or_none(""))
        self.assertEqual(strict_int_or_none("5"), 5)
        # 「没填」可以退回 None；「填了但是坏的」必须报错 ——
        # 悄悄退回 None 等于把一条上限悄悄删掉。
        with self.assertRaises(StrictValueError):
            strict_int_or_none("not-an-integer")
        with self.assertRaises(StrictValueError):
            strict_int_or_none(2.5)

    def test_telegram_chat_id_range_is_enforced(self) -> None:
        # Telegram chat id 是 64 位有符号整数范围。
        self.assertEqual(strict_int(-(2**63)), -(2**63))
        self.assertEqual(strict_int(2**63 - 1), 2**63 - 1)
        with self.assertRaises(StrictValueError):
            strict_int(-(2**63) - 1)
        with self.assertRaises(StrictValueError):
            strict_int(2**63)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()