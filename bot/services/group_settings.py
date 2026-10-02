from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Group

#: 群内 /av 开关在 ``groups.settings`` 里的键名（与全局 ``config.av_enabled`` 无关）。
GROUP_AV_ENABLE_KEY = "av_enabled"


def is_group_av_enabled(group_settings: dict | None) -> bool:
    """这个群自己有没有开 /av（``groups.settings.av_enabled``）。

    唯一权威判据是**该群自己的开关**，不是全局 ``config.av_enabled``；缺失一律
    视为关闭，字符串 ``"1"`` / ``"true"`` / ``"on"`` 等也算开启。
    """

    settings_dict = group_settings if isinstance(group_settings, dict) else {}
    value = settings_dict.get(GROUP_AV_ENABLE_KEY)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return bool(value)


async def acquire_group_settings_write_intent(
    session: AsyncSession,
    group_id: int,
) -> None:
    """Serialize a following ``Group.settings`` read/modify/write transaction.

    The settings document is stored in one JSON column.  Taking the write
    transaction before reading prevents a later whole-document ORM UPDATE from
    overwriting an intervening mutation made by another handler.
    """

    await session.execute(
        update(Group)
        .where(Group.id == int(group_id))
        .values(title=Group.title)
        .execution_options(synchronize_session=False)
    )
