"""路由完整性：装饰器必须挂在真正的异步处理器上。

为什么专门加这个测试（教训）：往群消息处理器前面插三个辅助函数时，把
``@router.message(...)`` 留在了它上面，运行时装饰器就挂到了 ``_verdict_confidence``
（一个纯计算函数）身上，真正的 ``on_group_message`` 反而没有路由——
群消息于是全部「处理成功、0ms、什么都不做」，而当时 1879 个测试一个都没报错，
因为没有任何测试关心"路由到底指向谁"。

规则很简单：挂在 ``router.<observer>`` 上的回调必须是 async 函数，且名字不能是
下划线开头的私有/辅助函数。这两条任意一条不满足，就说明装饰器被插错了位置。
"""

from __future__ import annotations

import inspect
import unittest

from bot.handlers import admin as admin_handlers
from bot.handlers import commands as command_handlers
from bot.handlers import group as group_handlers
from bot.handlers import membership as membership_handlers

#: 四个路由模块（与 __main__ 里 include_router 的清单一致）
ROUTER_MODULES = {
    "commands": command_handlers,
    "admin": admin_handlers,
    "membership": membership_handlers,
    "group": group_handlers,
}

OBSERVERS = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
    "inline_query",
    "chat_member",
    "my_chat_member",
    "chat_join_request",
)


def _route_callbacks(module: object, observer: str) -> list[tuple[str, object]]:
    observer_obj = getattr(module.router, observer, None)
    if observer_obj is None:
        return []
    return [
        (getattr(item.callback, "__name__", repr(item.callback)), item.callback)
        for item in getattr(observer_obj, "handlers", [])
    ]


class GroupRouteIntegrityTests(unittest.TestCase):
    """群消息、群消息编辑这两条主路由必须指向真正的处理器。"""

    def test_group_message_route_is_the_real_handler(self) -> None:
        names = [name for name, _ in _route_callbacks(group_handlers, "message")]
        self.assertIn(
            "on_group_message",
            names,
            "群消息路由丢了：装饰器可能被插到了别的函数上（群会彻底不响应）",
        )

    def test_group_edited_message_route_is_the_real_handler(self) -> None:
        names = [name for name, _ in _route_callbacks(group_handlers, "edited_message")]
        self.assertIn("on_group_message_edited", names)

    def test_group_message_handler_is_async(self) -> None:
        for name, callback in _route_callbacks(group_handlers, "message"):
            self.assertTrue(
                inspect.iscoroutinefunction(callback),
                f"群消息路由指向了同步函数 {name}：装饰器挂错了对象",
            )


class EveryRouteIntegrityTests(unittest.TestCase):
    """全量检查：任何路由回调都不能是同步函数或下划线开头的辅助函数。"""

    def test_all_route_callbacks_are_real_handlers(self) -> None:
        problems: list[str] = []
        checked = 0
        for module_name, module in ROUTER_MODULES.items():
            for observer in OBSERVERS:
                for name, callback in _route_callbacks(module, observer):
                    checked += 1
                    where = f"{module_name}.router.{observer} → {name}"
                    if not inspect.iscoroutinefunction(callback):
                        problems.append(f"{where}（同步函数，装饰器挂错）")
                    elif name.startswith("_"):
                        problems.append(f"{where}（下划线开头的辅助函数，路由丢了）")
        self.assertGreater(checked, 20, "路由数量异常，检查逻辑可能没生效")
        self.assertEqual(problems, [], "路由指向了非处理器：" + "; ".join(problems))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
