"""A 档（规则绕过 / 误伤与权益）验收中发现的缺陷回归用例。

这批缺陷都有一个共同形态：**代码在运行时引用了一个从未导入或从未赋值的名字**，
而既有用例恰好没走到那条分支。静态检查（pyflakes）能提前发现，这里再把两个
运行时真正会被用到的名字钉成用例，避免回归。

1. ``bot.handlers.group._send_review_handover`` 里用 ``SimpleNamespace(...)`` 构造
   证据链接的 chat 占位对象，但模块从未导入它 —— 审核卡生成链接时必然 NameError。
2. ``bot.handlers.admin`` 的若干签名使用 ``Any``，模块未导入（有
   ``from __future__ import annotations`` 兜底，但注解一旦被求值就会炸，
   例如任何 ``typing.get_type_hints`` / 反射式调用）。

另外两处（``global_ban`` 的 ``locally_banned``、``point_shop`` 提交失败后读余额）
由 ``tests/test_join_screening.py``、``tests/test_moderation_durable_retry.py``、
``tests/test_point_shop.py`` 里既有的用例覆盖，不在这里重复。
"""
from __future__ import annotations

import importlib
import unittest


class RuntimeNamesAreResolvableTests(unittest.TestCase):
    """运行时使用的名字必须在模块里真的存在。"""

    def test_group_handler_imports_simple_namespace(self) -> None:
        group_module = importlib.import_module("bot.handlers.group")
        self.assertTrue(
            hasattr(group_module, "SimpleNamespace"),
            "bot.handlers.group 在 _send_review_handover 里运行时使用 SimpleNamespace，"
            "必须真的导入它（否则审核卡生成证据链接时 NameError）",
        )

    def test_admin_handler_imports_any(self) -> None:
        admin_module = importlib.import_module("bot.handlers.admin")
        self.assertTrue(
            hasattr(admin_module, "Any"),
            "bot.handlers.admin 的签名使用 Any，必须真的导入它",
        )

    def test_global_ban_has_no_undefined_local_ban_reference(self) -> None:
        """``global_ban`` 里不再出现未定义的 ``locally_banned`` 名字。

        用源码文本断言，因为只有在特定封禁组合下才会走到那条分支；
        静态扫查（pyflakes）是同一意图的更强版本，这里留一条不依赖额外工具的守卫。
        """
        import inspect

        from bot.middlewares import global_ban

        source = inspect.getsource(global_ban)
        self.assertNotIn(
            "final_banned and locally_banned",
            source,
            "该分支引用未定义的 locally_banned；应使用 preserve_ban() 查持久策略",
        )


if __name__ == "__main__":
    unittest.main()
