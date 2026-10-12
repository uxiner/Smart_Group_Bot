"""把「这条 update 属于哪个归属」绑到任务上下文上。

为什么放在最外层 middleware 而不是各 handler 里散着写：归属必须在**任何**
模型调用之前就确定，而且必须覆盖到所有 observer（message / edited_message /
callback_query / chat_member …）。散着写的后果是漏一个 observer，而漏掉的
那个会把用量记到 ``system`` 桶 —— 看着只是少一栏数字，实际是某个群的账单
凭空消失。

私聊按 ``private`` 记，后台作业必须自己显式声明归属（见各 service），**不**
默认落到发起它的那位管理员的私聊上。

这个 middleware **只**做上下文绑定：不查库、不发网络、不写库。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware

from bot.services import usage_scope

log = logging.getLogger(__name__)

_GROUP_CHAT_TYPES = {"group", "supergroup", "channel"}


def _chat_scope(chat: Any) -> usage_scope.UsageScope:
    chat_type = str(getattr(chat, "type", "") or "").lower()
    chat_id = getattr(chat, "id", None)
    if chat_type in _GROUP_CHAT_TYPES and isinstance(chat_id, int) and chat_id < 0:
        # 归属类型先不定用途：具体用途由**这次调用的 stage** 决定（见
        # usage_scope.default_purpose_for_stage），审核路径不会被预算误停。
        return usage_scope.build_scope(
            kind=usage_scope.SCOPE_GROUP, group_id=chat_id, purpose=None
        )
    if chat_type == "private":
        return usage_scope.build_scope(kind=usage_scope.SCOPE_PRIVATE)
    return usage_scope.SYSTEM_SCOPE


def _scope_for_update(update: Any) -> usage_scope.UsageScope:
    """从 ``Update`` 推出归属。**推不出来就 system**，不猜。

    挂在 ``dispatcher.update`` 上时中间件拿到的是 :class:`Update`，不是
    ``Message`` —— 之前直接读 ``event.chat`` 于是永远读不到，群消息的用量
    全部掉进 ``system`` 桶（那不是"少一栏数字"，是某个群的账单凭空消失）。
    ``Update.event`` 会展开当前活跃的那一种事件；仍然拿不到时再看几个
    明确带 chat 的字段。
    """

    event = getattr(update, "event", None)
    if event is not None:
        chat = getattr(event, "chat", None)
        if chat is not None:
            return _chat_scope(chat)
        # callback_query 的 chat 藏在 message 里；inline 模式没有 message。
        inner_message = getattr(event, "message", None)
        inner_chat = getattr(inner_message, "chat", None)
        if inner_chat is not None:
            return _chat_scope(inner_chat)
    for attribute in ("message", "edited_message", "channel_post", "chat_member",
                       "my_chat_member"):
        candidate = getattr(update, attribute, None)
        chat = getattr(candidate, "chat", None)
        if chat is not None:
            return _chat_scope(chat)
    callback = getattr(update, "callback_query", None)
    callback_chat = getattr(getattr(callback, "message", None), "chat", None)
    if callback_chat is not None:
        return _chat_scope(callback_chat)
    return usage_scope.SYSTEM_SCOPE


def _scope_for_event(event: Any) -> usage_scope.UsageScope:
    """兼容直接挂在子观察者上的用法（拿到的是 Message / CallbackQuery）。"""

    chat = getattr(event, "chat", None)
    if chat is not None:
        return _chat_scope(chat)
    inner_chat = getattr(getattr(event, "message", None), "chat", None)
    if inner_chat is not None:
        return _chat_scope(inner_chat)
    return _scope_for_update(event)


class UsageScopeMiddleware(BaseMiddleware):
    def __init__(
        self,
        *,
        scope_resolver: Callable[[Any], usage_scope.UsageScope] | None = None,
    ) -> None:
        # 一个解析器同时吃 Update 与各种子事件：它自己认得出拿到的是哪一种，
        # 所以注册在根上还是注册在子观察者上都能用对。
        self._resolve = scope_resolver or _scope_for_event

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        try:
            scope = self._resolve(event)
        except usage_scope.ScopeInvalid:
            # 归属推不出来不是错误：退到 system 桶继续服务，只是这一条不计入
            # 任何群。让一次畸形 update 变成 500 是更糟的结果。
            scope = usage_scope.SYSTEM_SCOPE
        token = usage_scope.set_current(scope)
        try:
            return await handler(event, data)
        finally:
            usage_scope.reset_current(token)