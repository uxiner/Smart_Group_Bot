"""``/selfcheck``：最高管理员在私聊里读一条**本进程**只读自检。

为什么单独一个 router：``commands.router`` 目前在 ``private_chat.router`` 之前，
而私聊兜底处理器用的是宽泛 ``F.chat.type == PRIVATE`` 过滤。这里用**精确命令**
（``Command("selfcheck")``，天然只认 ``/selfcheck`` 与 ``/selfcheck@本bot``）注册在
``commands.router`` 之前，既不会被私聊/群聊的宽泛处理器吃掉，也不会吃掉它们。

权限（唯一依据是真实配置，不是正文措辞）：

* 只认 ``settings.super_admin_id`` 与真实 ``message.from_user.id`` 的相等关系
  （:func:`bot.services.authz.is_super_admin_user_id`）。未配置（0/None）、匿名
  （``from_user is None``）、普通用户、机器人 → 一律只回一句固定拒绝语。
* 正文里自称"我是超管"没有任何作用：本处理器从不读取正文做鉴权。
* 群聊里**不展示任何详细状态**，只回一句"仅私聊可用"，连总体健康都不说。

只读约束：不建模型请求、不消费聊天配额、不落库、不自动语音、不访问网络/搜索/TTS、
不重启服务；发送只走既有助手 :func:`bot.utils.telegram.answer_with_auto_delete`
（仅此命令显式禁用删除策略，避免删除队列写入与键盘编辑），不新造统一发送层。
既有生产中间件仍可打开 DB 会话或记录投递水位；本 handler 不新增业务写入。
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import Settings
from bot.services.authz import is_super_admin_user_id
from bot.services.self_check import (
    NOT_OBSERVED_REASON,
    render_self_check_text,
    self_check_report,
)
from bot.utils.telegram import answer_with_auto_delete

log = logging.getLogger(__name__)

router = Router(name="self_check")

#: 群聊（以及任何非私聊）口径：不暴露任何状态细节。
PRIVATE_ONLY_NOTICE = "该命令仅可在与我的私聊中使用。"

#: 无权限口径：不区分"未配置/匿名/普通用户"，避免任何人做权限枚举。
DENIED_NOTICE = "该命令仅对最高管理员开放。"

#: 渲染兜底：只说"没取到"，不回显异常内容。
UNAVAILABLE_NOTICE = f"自检暂时不可用（{NOT_OBSERVED_REASON}之外的采样未完成）。"


async def _send(message: Message, settings: Settings, text: str) -> None:
    """复用既有发送语义，但不安排删除任务、不编辑删除按钮。"""

    await answer_with_auto_delete(
        message,
        text,
        auto_delete_seconds=0,
    )


def build_self_check_message(
    *,
    settings: Settings,
    snapshot_provider=None,
) -> str:
    """生成自检正文。抽成模块级函数，便于离线断言而不喂 Telegram。"""

    report = self_check_report(snapshot_provider=snapshot_provider)
    return render_self_check_text(report)


@router.message(Command("selfcheck"))
async def cmd_selfcheck(message: Message, settings: Settings) -> None:
    chat = message.chat
    if chat is None or chat.type != ChatType.PRIVATE:
        await _send(message, settings, PRIVATE_ONLY_NOTICE)
        return

    user = message.from_user
    if user is None or getattr(user, "is_bot", False):
        await _send(message, settings, DENIED_NOTICE)
        return

    try:
        authorized = is_super_admin_user_id(int(user.id), settings)
    except Exception:  # pragma: no cover - user.id 一直是 int
        authorized = False
    if not authorized:
        await _send(message, settings, DENIED_NOTICE)
        return

    try:
        text = build_self_check_message(settings=settings)
    except Exception as exc:
        # 固定兜底；日志也不携带异常正文或 traceback，取消仍原样传播。
        log.warning("self check: report build failed (%s)", type(exc).__name__)
        text = UNAVAILABLE_NOTICE
    await _send(message, settings, text)
