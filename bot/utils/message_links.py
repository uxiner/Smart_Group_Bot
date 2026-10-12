"""Telegram 消息回链构造：审核证据线与 ``/find``、``/memory`` 共用同一形状。

规则与历史上 ``bot/handlers/group.py::_message_evidence_link`` 完全一致，避免
命令层直接 import 审核 handler（那会形成 commands ↔ group 的循环依赖）：

* 有公开用户名 → ``https://t.me/<username>/<message_id>``；
* 私有超级群 → ``https://t.me/c/<内部 id>/<message_id>``，**仅群成员可用**；
* 其余（基础群、缺 id／id 非法）→ 返回空串，由调用方降级说明"没有链接"。

论坛（Topics）深链：官方 message-links 同时支持 query 与 path 两种写法
（``t.me/<username>/<thread_id>/<id>`` 与 ``t.me/c/<channel>/<thread_id>/<id>``），
本模块统一用 path 形状。``thread_id`` 是可选的**可信**入参：

* 只有调用方从一致归档行（``GroupMessageArchive.message_thread_id``）读到的
  thread 才允许传进来；**不从正文/模型输出/用户输入推测**，本模块也不猜；
* ``thread_id`` 必须是严格正整数（``bool``/``float``/字符串一律当作没有）；
* ``General`` 主题（thread id = 1）用不带 thread 的普通形状——官方两种形状都合法，
  这里选与 Telegram 客户端一致的那一种，并且这是显式规则 + 有回归。
* 没有可信 thread（或旧行为 NULL）→ 保持原来的非 Topics 形状，不伪造。

这里只管形状，不判断"这条消息现在是否还能看到"。调用方必须另外用真实删除
证据决定是否给链接（链接不是删除过滤的豁免）。
"""

from __future__ import annotations

#: 任何人都能打开的公开群链接
PUBLIC_LINK_KIND = "public"
#: 私有超级群的 ``t.me/c/…`` 链接，只有群成员可用
MEMBER_ONLY_LINK_KIND = "member_only"
#: 论坛「General」主题的 thread id：回链退回普通（非 Topics）形状
GENERAL_THREAD_ID = 1


def trusted_thread_id(value: object) -> int | None:
    """把候选 thread id 归一成"可信的严格正整数"或 ``None``（不静默截断、不猜）。"""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def message_link(
    chat: object,
    message_id: object,
    message_thread_id: object = None,
) -> tuple[str, str]:
    """返回 ``(url, kind)``；给不出链接时返回 ``("", "")``。

    ``kind`` 只描述这条链接的可达范围，调用方据此决定是否提示"仅群成员可用"。
    ``message_thread_id`` 是可选的**可信** thread（见模块 docstring）。
    """

    try:
        mid = int(message_id or 0)
        chat_id = int(getattr(chat, "id", 0) or 0)
    except (TypeError, ValueError):
        return "", ""
    if mid <= 0 or chat_id == 0:
        return "", ""
    thread = trusted_thread_id(message_thread_id)
    if thread == GENERAL_THREAD_ID:
        # General 主题：与客户端一致，用普通消息回链（官方两种形状都合法）。
        thread = None
    username = str(getattr(chat, "username", "") or "").strip().lstrip("@")
    if username:
        if thread is not None:
            return f"https://t.me/{username}/{thread}/{mid}", PUBLIC_LINK_KIND
        return f"https://t.me/{username}/{mid}", PUBLIC_LINK_KIND
    # 超级群内部链接：-100XXXXXXXXXX → t.me/c/XXXXXXXXXX[/<thread_id>]/<message_id>
    raw = str(chat_id)
    if raw.startswith("-100") and len(raw) > 4:
        if thread is not None:
            return f"https://t.me/c/{raw[4:]}/{thread}/{mid}", MEMBER_ONLY_LINK_KIND
        return f"https://t.me/c/{raw[4:]}/{mid}", MEMBER_ONLY_LINK_KIND
    return "", ""


def message_evidence_link(
    chat: object,
    message_id: object,
    message_thread_id: object = None,
) -> str:
    """能给就给一条 t.me 消息回链，拿不到返回空串。"""

    return message_link(chat, message_id, message_thread_id)[0]
