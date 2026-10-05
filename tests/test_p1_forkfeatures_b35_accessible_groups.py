"""修复批 P1-2 / B-35：私聊准入的「可访问群集合」既不完整又会过期。

复现的原缺陷（``AUDIT-B`` B-35）：

1. **不完整**：``private_chat.resolve_access`` 的管理员分支一命中就 ``return``，
   ``confirmed`` 只是**已遍历过的前缀**（``list_authorized_groups`` 按
   ``created_at.desc()`` 排序），后面的群永远不打 ``getChatMember``、
   也永远不进 ``AccessVerdict.group_ids``——私聊侧读群聊公开记录 / 群作用域长期记忆
   时拿到的可访问群集合因此是残缺的。
2. **会过期**：``MemberAccessCache`` 的 TTL 是 600s，缓存里带着同一个群集合；
   被踢出群之后最长 10 分钟内，那一群的内容仍然继续注入他的私聊。

隐私方向（refs 要求明确断言）：

* 私聊正文**永不**进群聊（``bot/handlers/group.py`` 侧由 ``test_context_privacy`` /
  ``test_long_term_memory_privacy`` 钉死，本文件做结构性复核）；
* 「可访问群集合」只由 ``getChatMember`` 逐群确认得到——**他人内容不得冒充本人可读**，
  不在群里的群一个都不进集合。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.services import private_chat as dm

GROUP_A = -100111
GROUP_B = -100222
GROUP_C = -100333
USER_ID = 777


def _bot(*, statuses, absent_groups=(), failing_groups=()) -> MagicMock:
    bot = MagicMock()
    mapping = dict(statuses)
    absent = {int(g) for g in absent_groups}
    failing = {int(g) for g in failing_groups}

    async def _get_chat_member(chat_id, user_id):
        cid = int(chat_id)
        if cid in absent:
            raise RuntimeError("Telegram server says - Bad Request: member not found")
        if cid in failing:
            raise RuntimeError("telegram down")
        member = MagicMock()
        member.status = mapping.get(cid, "left")
        member.is_member = member.status != "left"
        return member

    bot.get_chat_member = AsyncMock(side_effect=_get_chat_member)
    return bot


def _settings() -> SimpleNamespace:
    return SimpleNamespace(super_admin_id=1, bot=SimpleNamespace())


def _authorized_groups(*ids: int):
    # ``list_authorized_groups`` 按 created_at.desc() 排序：模拟「A 群最新、B 群更旧」。
    return [SimpleNamespace(group_id=group_id) for group_id in ids]


class AccessibleGroupSetTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_verdict_collects_every_confirmed_group(self) -> None:
        """管理员分支不再提前 return：``group_ids`` 必须是**全集**而不是前缀。"""

        bot = _bot(
            statuses={GROUP_A: "administrator", GROUP_B: "member", GROUP_C: "creator"}
        )
        cache = dm.MemberAccessCache()
        with patch.object(
            dm, "list_authorized_groups", new=AsyncMock(return_value=_authorized_groups(GROUP_A, GROUP_B, GROUP_C))
        ):
            verdict = await dm.resolve_access(
                bot, AsyncMock(), _settings(), USER_ID, cache=cache
            )
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.tier, dm.TIER_ADMIN, "只要有一个群是管理员就是管理员档")
        self.assertEqual(
            set(verdict.group_ids),
            {GROUP_A, GROUP_B, GROUP_C},
            "可访问群集合必须遍历完整张授权群列表（原来只拿到前缀）",
        )
        self.assertEqual(bot.get_chat_member.await_count, 3, "每个授权群都要确认一次")
        # 缓存里也必须是全集（否则缓存命中时又会退回残缺集合）。
        self.assertEqual(set(cache.get_groups(USER_ID)), {GROUP_A, GROUP_B, GROUP_C})

    async def test_admin_tier_survives_a_failing_group_without_caching(self) -> None:
        """有群没查成时：放行（管理员档已知）但不缓存——缓存集合不是全集。"""

        bot = _bot(
            statuses={GROUP_A: "administrator", GROUP_B: "member"},
            failing_groups=[GROUP_C],
        )
        cache = dm.MemberAccessCache()
        with patch.object(
            dm, "list_authorized_groups", new=AsyncMock(return_value=_authorized_groups(GROUP_A, GROUP_B, GROUP_C))
        ):
            verdict = await dm.resolve_access(
                bot, AsyncMock(), _settings(), USER_ID, cache=cache
            )
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.tier, dm.TIER_ADMIN)
        self.assertEqual(len(cache), 0, "结论不完整时不该缓存")

    async def test_groups_the_user_is_not_in_never_enter_the_set(self) -> None:
        """不在群里的群**一个都不进**集合：他人/外群内容不得被当成他可读。"""

        bot = _bot(
            statuses={GROUP_A: "member", GROUP_B: "member"},
            absent_groups=[GROUP_C],
        )
        with patch.object(
            dm, "list_authorized_groups", new=AsyncMock(return_value=_authorized_groups(GROUP_A, GROUP_B, GROUP_C))
        ):
            verdict = await dm.resolve_access(
                bot, AsyncMock(), _settings(), USER_ID, cache=dm.MemberAccessCache()
            )
        self.assertTrue(verdict.allowed)
        self.assertEqual(set(verdict.group_ids), {GROUP_A, GROUP_B})
        self.assertNotIn(GROUP_C, verdict.group_ids)

    async def test_admin_in_the_last_group_keeps_the_earlier_member_group(self) -> None:
        """回归：管理员出现在**列表末尾**时，前面的普通成员群仍要进集合。"""

        bot = _bot(statuses={GROUP_A: "member", GROUP_B: "member", GROUP_C: "administrator"})
        with patch.object(
            dm, "list_authorized_groups", new=AsyncMock(return_value=_authorized_groups(GROUP_A, GROUP_B, GROUP_C))
        ):
            verdict = await dm.resolve_access(
                bot, AsyncMock(), _settings(), USER_ID, cache=dm.MemberAccessCache()
            )
        self.assertEqual(verdict.tier, dm.TIER_ADMIN)
        self.assertEqual(set(verdict.group_ids), {GROUP_A, GROUP_B, GROUP_C})


class CacheExpiryTests(unittest.TestCase):
    def test_default_ttl_is_short_enough_to_bound_the_stale_window(self) -> None:
        self.assertLessEqual(
            dm.MEMBER_ACCESS_TTL_SECONDS,
            60.0,
            "被踢出群后的越权窗口必须 <= 60s（B-35；原来 600s）",
        )
        self.assertEqual(dm.MemberAccessCache().ttl_seconds, 60.0)

    def test_kicked_user_loses_the_group_set_after_one_ttl(self) -> None:
        clock = {"now": 0.0}
        cache = dm.MemberAccessCache(clock=lambda: clock["now"])
        cache.put(USER_ID, dm.TIER_MEMBER, [GROUP_A, GROUP_B])
        self.assertEqual(set(cache.get_groups(USER_ID)), {GROUP_A, GROUP_B})
        clock["now"] += dm.MEMBER_ACCESS_TTL_SECONDS + 1
        self.assertEqual(cache.get_groups(USER_ID), (), "TTL 一到，集合必须失效")
        self.assertIsNone(cache.get(USER_ID))


class PrivacyDirectionTests(unittest.TestCase):
    """结构性复核：私聊正文不进群聊。"""

    ROOT = Path(__file__).resolve().parents[1]

    def test_group_side_modules_never_reference_the_private_history_reader(self) -> None:
        for relative in (
            "bot/handlers/group.py",
            "bot/services/casual.py",
            "bot/services/skills/service.py",
        ):
            with self.subTest(file=relative):
                source = (self.ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("load_private_history", source)
                self.assertNotIn("private_chat_messages", source)
                self.assertNotIn("PrivateChatMessage", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
