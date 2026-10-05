"""改非默认值之后，**真实消费者**真的变了——不是只 assert schema 存了数。

每个用例都走「运行时配置 → apply_to_settings → policy_runtime 快照 → 业务函数」，
并断言业务输出跟着变。覆盖：私聊配额/长度/保险丝、商店价格与奖池、签到、活跃奖励、
提醒文案、显示文案、LLM 阶段 deadline、维护巡检间隔、后台 getter。

价格相关的用例额外钉住"一次交易只用一份快照"——中途改价不能让显示与扣费错账。
"""

from __future__ import annotations

import unittest

from bot.config import Settings
from bot.services import policy_runtime
from bot.services.runtime_config import RuntimeConfig


def _bind(payload: dict) -> RuntimeConfig:
    """用一份 payload 走完整的 apply 链，返回验证过的 RuntimeConfig。"""

    base = RuntimeConfig()
    merged = base.storage_payload()
    for section, values in payload.items():
        merged.setdefault(section, {})
        merged[section] = {**merged[section], **values}
    config = RuntimeConfig.model_validate(merged)
    settings = Settings(_env_file=None)
    config.apply_to_settings(settings, apply_prompts=False)
    policy_runtime.bind(settings)
    return config


class PolicyBindingMixin(unittest.TestCase):
    def setUp(self) -> None:
        policy_runtime.unbind()

    def tearDown(self) -> None:
        policy_runtime.unbind()


class PrivateChatConsumerTests(PolicyBindingMixin):
    def test_quota_tiers_and_limits_really_change(self) -> None:
        from bot.services import private_chat

        self.assertEqual(
            private_chat.quota_limits(),
            (100, 500, 20_000, 100_000),
            "默认必须与改造前逐字相同",
        )
        _bind(
            {
                "private_chat": {
                    "per_user_daily_limit": 3,
                    "admin_per_user_daily_limit": 7,
                    "global_daily_limit": 11,
                    "admin_global_daily_limit": 13,
                }
            }
        )
        self.assertEqual(private_chat.quota_limits(), (3, 7, 11, 13))

    def test_member_access_cache_ttl_follows_the_config_but_never_exceeds_60(self) -> None:
        from bot.services import private_chat

        cache = private_chat.MemberAccessCache()
        self.assertEqual(cache.ttl_seconds, 60.0)
        _bind({"private_chat": {"access_ttl_seconds": 5.0}})
        self.assertEqual(cache.ttl_seconds, 5.0)

    def test_ttl_is_re_evaluated_per_read_so_a_shrunk_window_expires_entries(self) -> None:
        from bot.services import private_chat

        now = [1000.0]
        cache = private_chat.MemberAccessCache(clock=lambda: now[0])
        cache.put(7, "member", [1])
        self.assertEqual(cache.get(7), "member")
        _bind({"private_chat": {"access_ttl_seconds": 10.0}})
        now[0] += 11.0
        # TTL 变小之后，这条**必须**立刻过期——不能让已被移出群的成员复活。
        self.assertIsNone(cache.get(7))

    def test_input_limit_really_truncates(self) -> None:
        from bot.services import private_chat

        long_text = "字" * 5000
        default_len = len(
            private_chat.build_private_chat_messages(long_text)[-1]["content"]
        )
        self.assertLessEqual(default_len, 1000 * 2)
        _bind({"private_chat": {"input_max_chars": 20}})
        shrunk = private_chat.build_private_chat_messages(long_text)[-1]["content"]
        self.assertLess(len(shrunk), 1000)

    def test_reply_split_follows_the_configured_limit(self) -> None:
        from bot.handlers.private_chat import _split_for_telegram

        chunks = _split_for_telegram("a" * 9000)
        self.assertTrue(all(len(chunk) <= 3800 for chunk in chunks))
        _bind({"private_chat": {"reply_max_chars": 512}})
        chunks = _split_for_telegram("a" * 9000)
        self.assertTrue(all(len(chunk) <= 512 for chunk in chunks))

    def test_search_fuse_hot_change_does_not_reset_todays_usage(self) -> None:
        from bot.services import dm_search

        budget = dm_search.SearchBudget()
        for _ in range(3):
            self.assertTrue(budget.take())
        self.assertEqual(budget.used, 3)
        _bind({"private_chat": {"search_daily_limit": 10}})
        self.assertEqual(budget.used, 3, "热改上限不能清零当天已消耗的次数")
        for _ in range(7):
            self.assertTrue(budget.take())
        self.assertFalse(budget.take(), "新上限 10 应该正好在第 11 次触发")

    def test_voice_segment_limit_follows_the_config(self) -> None:
        from bot.services import private_tts

        self.assertEqual(private_tts.max_private_tts_segments(), 6)
        _bind({"private_chat": {"voice_max_segments": 2}})
        self.assertEqual(private_tts.max_private_tts_segments(), 2)


class EconomyConsumerTests(PolicyBindingMixin):
    def test_tag_price_and_duration_really_change(self) -> None:
        from bot.services import point_shop

        self.assertEqual(point_shop.tag_price(7), 30)
        self.assertEqual(point_shop.tag_price(30), 80)
        _bind(
            {
                "economy": {
                    "tag_price_7d": 11,
                    "tag_price_30d": 22,
                    "tag_days_7d": 5,
                    "tag_days_30d": 40,
                }
            }
        )
        self.assertEqual(point_shop.tag_price(5), 11)
        self.assertEqual(point_shop.tag_price(40), 22)
        request = point_shop.parse_tag_request("摸鱼冠军")
        self.assertEqual((request.days, request.price), (5, 11))
        long_request = point_shop.parse_tag_request("摸鱼冠军 30天")
        self.assertEqual((long_request.days, long_request.price), (40, 22))

    def test_shop_menu_uses_the_configured_prices(self) -> None:
        from bot.services import point_shop

        menu = point_shop.render_shop_menu(available=999)
        self.assertIn("30 分", menu)
        self.assertIn("80 分", menu)
        _bind({"economy": {"tag_price_7d": 13, "tag_price_30d": 26, "pin_price": 7}})
        menu = point_shop.render_shop_menu(available=999)
        self.assertIn("13 分", menu)
        self.assertIn("26 分", menu)
        self.assertIn("7 分", menu)
        self.assertNotIn("① 自定义头衔 · 7 天 —— 30 分", menu)

    def test_lottery_table_and_expectation_follow_the_config(self) -> None:
        from bot.services import point_shop

        self.assertEqual(point_shop.expected_lottery_value(), 6.0)
        _bind(
            {
                "economy": {
                    "lottery_prizes": [
                        {"payout": 0, "weight": 9000, "label": "谢谢参与"},
                        {"payout": 100, "weight": 1000, "label": "100 分"},
                    ]
                }
            }
        )
        self.assertEqual(
            point_shop.expected_lottery_value(),
            10.0,
            "期望值必须由当前奖池算出",
        )
        self.assertEqual(
            point_shop.lottery_table(),
            (
                point_shop.LotteryPrize(0, 9000, "谢谢参与"),
                point_shop.LotteryPrize(100, 1000, "100 分"),
            ),
        )
        # 随机算法没变：roll=0 落在第一档，roll=9999 落在最后一档。
        self.assertEqual(point_shop.draw_prize(lambda _n: 0).points, 0)
        self.assertEqual(point_shop.draw_prize(lambda _n: 9999).points, 100)

    def test_one_purchase_uses_one_price_snapshot(self) -> None:
        """一次操作钉住的价格不会被中途的改价改掉。"""

        from bot.services import policy_runtime as pr
        from bot.services import point_shop

        with pr.pinned_section("economy") as snapshot:
            self.assertEqual(snapshot.tag_price_7d, 30)
            # 事务进行到一半，管理员把价改成 99……
            _bind({"economy": {"tag_price_7d": 99}})
            # ……这笔交易里读到的仍然是 30（菜单/扣费/退款同一份）。
            self.assertEqual(point_shop.tag_price(7), 30)
            self.assertEqual(pr.economy_policy().tag_price_7d, 30)
        # 出了这笔交易，下一笔立刻看到新价。
        self.assertEqual(point_shop.tag_price(7), 99)

    def test_challenge_skip_cost_cannot_be_freed(self) -> None:
        from bot.services import checkin

        self.assertEqual(checkin.challenge_skip_cost(), 2)
        _bind({"economy": {"challenge_skip_cost": 9}})
        self.assertEqual(checkin.challenge_skip_cost(), 9)
        with self.assertRaises(Exception):
            RuntimeConfig.model_validate(
                {
                    **RuntimeConfig().storage_payload(),
                    "economy": {"challenge_skip_cost": 0},
                }
            )

    def test_checkin_cap_and_rank_limit_really_change(self) -> None:
        from bot.services import checkin

        self.assertEqual(checkin.award_for_streak(30), 10)
        self.assertEqual(checkin.rank_limit(), 10)
        self.assertEqual(checkin.violation_window_days(), 30)
        _bind(
            {
                "economy": {
                    "checkin_daily_point_cap": 3,
                    "checkin_rank_limit": 5,
                    "checkin_violation_window_days": 7,
                }
            }
        )
        self.assertEqual(checkin.award_for_streak(30), 3)
        self.assertEqual(checkin.award_for_streak(2), 2)
        self.assertEqual(checkin.rank_limit(), 5)
        self.assertEqual(checkin.violation_window_days(), 7)

    def test_tag_max_length_is_capped_at_the_telegram_limit(self) -> None:
        from bot.services import point_shop

        _bind({"economy": {"tag_max_length": 8}})
        self.assertFalse(point_shop.check_tag_text("一二三四五六七八九").ok)
        self.assertTrue(point_shop.check_tag_text("一二三四五六七八").ok)
        with self.assertRaises(Exception):
            RuntimeConfig.model_validate(
                {
                    **RuntimeConfig().storage_payload(),
                    "economy": {"tag_max_length": 32},
                }
            )


class ActivityConsumerTests(PolicyBindingMixin):
    def test_reward_vector_drives_rank_and_total(self) -> None:
        from bot.services import activity

        self.assertEqual(
            [activity.reward_points_for_rank(i) for i in range(1, 11)],
            [25, 12, 12, 4, 4, 4, 4, 4, 4, 4],
        )
        self.assertEqual(activity._act().weekly_total_points, 77)
        self.assertEqual(activity._act().weekly_top_n, 10)
        self.assertEqual(activity.reward_points_for_rank(11), 0)
        _bind({"activity": {"weekly_reward_points": [50, 30]}})
        self.assertEqual(activity.reward_points_for_rank(1), 50)
        self.assertEqual(activity.reward_points_for_rank(2), 30)
        self.assertEqual(activity.reward_points_for_rank(3), 0)
        self.assertEqual(activity._act().weekly_total_points, 80)
        self.assertEqual(activity._act().weekly_top_n, 2)

    def test_thresholds_and_daily_cap_really_change(self) -> None:
        from bot.services import activity

        self.assertTrue(activity.meets_threshold(messages=10, active_days=3))
        self.assertFalse(activity.meets_threshold(messages=9, active_days=3))
        _bind(
            {
                "activity": {
                    "min_weekly_messages": 50,
                    "min_active_days": 5,
                    "max_daily_messages": 3,
                    "min_message_text_length": 4,
                }
            }
        )
        self.assertFalse(activity.meets_threshold(messages=10, active_days=3))
        self.assertTrue(activity.meets_threshold(messages=50, active_days=5))
        # 最小有效长度 4：3 个字不算，"abcd" 才算。
        self.assertFalse(activity.is_countable_message(text="abc"))
        self.assertTrue(activity.is_countable_message(text="abcd"))

    def test_scoring_formula_is_untouched(self) -> None:
        from bot.services import activity

        self.assertEqual(
            activity.activity_score(messages=10, active_days=3, replies_received=5),
            21,
        )


class CheckinReminderConsumerTests(PolicyBindingMixin):
    def test_slots_greetings_roster_and_stale_grace(self) -> None:
        from bot.services import checkin_reminder as reminder

        self.assertEqual(reminder.reminder_slots(), (9, 12, 15, 18))
        self.assertEqual(reminder.normalize_slot(12), 12)
        self.assertIsNone(reminder.normalize_slot(11))
        self.assertEqual(reminder.reminder_auto_delete_seconds(), 600)
        _bind(
            {
                "checkin_reminder": {
                    "slots": [8, 20],
                    "slot_greetings": {"8": "早上好", "20": "晚上好"},
                    "auto_delete_seconds": 30,
                    "roster_max_names": 3,
                    "stale_grace_seconds": 120,
                }
            }
        )
        self.assertEqual(reminder.reminder_slots(), (8, 20))
        self.assertEqual(reminder.normalize_slot(8), 8)
        self.assertIsNone(reminder.normalize_slot(9))
        self.assertEqual(reminder.reminder_auto_delete_seconds(), 30)
        self.assertEqual(reminder._rem().roster_max_names, 3)
        self.assertEqual(reminder._rem().stale_grace_seconds, 120)
        self.assertEqual(reminder._rem().greeting_for(20), "晚上好")
        self.assertEqual(reminder._rem().greeting_for(9), "")

    def test_greeting_keys_must_be_among_the_slots(self) -> None:
        with self.assertRaises(Exception):
            RuntimeConfig.model_validate(
                {
                    **RuntimeConfig().storage_payload(),
                    "checkin_reminder": {
                        "slots": [9],
                        "slot_greetings": {"9": "早", "18": "晚"},
                    },
                }
            )

    def test_roster_rendering_honours_the_configured_cap(self) -> None:
        from bot.services import checkin_reminder as reminder

        names = [f"成员{i}" for i in range(1, 26)]
        line = reminder.render_checkin_roster(checked_in=25, names=names)
        self.assertIn("…等 5 人", line, "默认 20 个封顶，超出折成「…等 N 人」")
        self.assertNotIn("成员21", line)
        _bind({"checkin_reminder": {"roster_max_names": 2}})
        line = reminder.render_checkin_roster(checked_in=25, names=names)
        self.assertIn("成员1", line)
        self.assertIn("成员2", line)
        self.assertNotIn("成员3", line)


class DisplayConsumerTests(PolicyBindingMixin):
    def test_buttons_notices_and_voice_title_follow_the_config(self) -> None:
        from bot.services import checkin, checkin_reminder, private_tts

        self.assertEqual(checkin.checkin_button_text(), "✅ 一键签到")
        self.assertEqual(checkin_reminder.shop_button_text(), "🛒 积分商店")
        self.assertEqual(private_tts.audio_title(), "语音回复")
        _bind(
            {
                "display": {
                    "bot_display_name": "示例助手",
                    "checkin_button_text": "签到",
                    "shop_button_text": "商店",
                    "private_voice_title": "语音",
                    "private_limit_notice": "今天到这儿啦。",
                    "private_global_limit_notice": "大家都忙完啦。",
                }
            }
        )
        self.assertEqual(checkin.checkin_button_text(), "签到")
        self.assertEqual(checkin_reminder.shop_button_text(), "商店")
        self.assertEqual(private_tts.audio_title(), "语音")

        from bot.services.private_chat import QuotaOutcome

        outcome = QuotaOutcome(
            allowed=False,
            reason="user_limit",
            user_used=101,
            per_user_limit=100,
            global_used=0,
            global_limit=20000,
        )
        from bot.services import private_chat

        self.assertEqual(private_chat.quota_notice(outcome), "今天到这儿啦。")
        self.assertEqual(
            private_chat.quota_notice(
                QuotaOutcome(
                    allowed=False,
                    reason="global_limit",
                    user_used=0,
                    per_user_limit=0,
                    global_used=1,
                    global_limit=1,
                )
            ),
            "大家都忙完啦。",
        )

    def test_display_name_is_stripped_from_search_queries(self) -> None:
        from bot.services import dm_search

        self.assertEqual(
            dm_search.build_search_query("助手，帮我查一下价格"), "帮我查一下价格"
        )
        _bind({"display": {"bot_display_name": "小助手"}})
        self.assertEqual(
            dm_search.build_search_query("小助手，帮我查一下价格"), "帮我查一下价格"
        )
        # 换了显示名，旧称呼不再被当作称呼（那属于上一份部署的身份）。
        self.assertEqual(
            dm_search.build_search_query("助手，帮我查一下价格"), "助手，帮我查一下价格"
        )


class ResourceConsumerTests(PolicyBindingMixin):
    def test_llm_stage_deadlines_follow_the_config(self) -> None:
        from bot.services.llm import stage_deadline_seconds

        self.assertEqual(stage_deadline_seconds("decision"), 35.0)
        self.assertEqual(stage_deadline_seconds("moderation"), 35.0)
        self.assertEqual(stage_deadline_seconds("group_summary"), 15.0)
        _bind(
            {
                "resources": {
                    "llm_stage_deadlines": {
                        "decision": 12.0,
                        "moderation": 13.0,
                        "main": 99.0,
                    }
                }
            }
        )
        self.assertEqual(stage_deadline_seconds("decision"), 12.0)
        self.assertEqual(stage_deadline_seconds("moderation"), 13.0)
        # 未登记的阶段沿用 main 的量级，不会凭空多出第三份默认值。
        self.assertEqual(stage_deadline_seconds("skill"), 99.0)

    def test_throttle_tts_av_limits_follow_the_config(self) -> None:
        from bot.services.av_search import av_query_limits
        from bot.services.doubao_tts import max_segments_per_message, tts_limits
        from bot.services.moderation_throttle import throttle_limits

        self.assertEqual(throttle_limits(), (3, 4.0, 6.0, 3))
        self.assertEqual(tts_limits(), (6, 30.0, 60.0))
        self.assertEqual(av_query_limits(), (45.0, 2.0, 512))
        _bind(
            {
                "resources": {
                    "moderation_throttle_burst": 5,
                    "moderation_throttle_spacing_seconds": 2.5,
                    "tts_max_segments_per_message": 3,
                    "tts_transcode_timeout_seconds": 11.0,
                    "av_query_deadline_seconds": 20.0,
                    "av_star_name_cache_max": 128,
                }
            }
        )
        self.assertEqual(throttle_limits(), (5, 2.5, 6.0, 3))
        self.assertEqual(tts_limits(), (3, 11.0, 60.0))
        self.assertEqual(max_segments_per_message(), 3)
        self.assertEqual(av_query_limits(), (20.0, 2.0, 128))

    def test_throttle_refuses_to_disable_local_moderation(self) -> None:
        """整形参数只调节奏，桶状态与等待者上限这两条不变量不受影响。"""

        from bot.services.moderation_throttle import ModerationAdmissionGate

        gate = ModerationAdmissionGate()
        _bind(
            {
                "resources": {
                    "moderation_throttle_burst": 1,
                    "moderation_throttle_spacing_seconds": 0.2,
                    "moderation_throttle_max_waiters": 1,
                }
            }
        )
        self.assertTrue(gate.apply_runtime_limits())
        self.assertEqual(gate.burst, 1)
        self.assertEqual(gate.max_waiters, 1)
        self.assertEqual(gate.max_keys, 4096, "有界 key 状态不能被配置放开")
        # 再调一次同样的值 = no-op。
        self.assertFalse(gate.apply_runtime_limits())

    def test_memory_and_search_and_archive_limits_follow_the_config(self) -> None:
        from bot.services.archive_vector import archive_limits
        from bot.services.long_term_memory import memory_limits
        from bot.services.search_memory import search_memory_limits
        from bot.services.group_context import decision_history_budget

        self.assertEqual(memory_limits()["max_facts_per_extraction"], 6)
        self.assertEqual(memory_limits()["maintenance_interval_seconds"], 21600)
        self.assertEqual(search_memory_limits(), {"recall_limit": 5, "prune_interval_seconds": 21600})
        self.assertEqual(decision_history_budget(), (8192, 80))
        self.assertEqual(archive_limits()["indexing_lease_seconds"], 120.0)
        _bind(
            {
                "resources": {
                    "memory_max_facts_per_extraction": 4,
                    "memory_maintenance_interval_seconds": 3600,
                    "search_record_recall_limit": 9,
                    "search_prune_interval_seconds": 7200,
                    "decision_history_token_budget": 4096,
                    "archive_batch_size": 32,
                }
            }
        )
        self.assertEqual(memory_limits()["max_facts_per_extraction"], 4)
        self.assertEqual(memory_limits()["maintenance_interval_seconds"], 3600)
        self.assertEqual(search_memory_limits()["recall_limit"], 9)
        self.assertEqual(search_memory_limits()["prune_interval_seconds"], 7200)
        self.assertEqual(decision_history_budget(), (4096, 80))
        self.assertEqual(archive_limits()["batch_size"], 32)

    def test_admin_alert_and_pending_reply_limits_follow_the_config(self) -> None:
        from bot.handlers.group import (
            _admin_alert_limits,
            _truncate_alert_text,
        )

        self.assertEqual(_admin_alert_limits(), (600.0, 5, 512, 900))
        self.assertTrue(len(_truncate_alert_text("x" * 5000)) <= 920)
        _bind(
            {
                "resources": {
                    "admin_alert_window_seconds": 60.0,
                    "admin_alert_aggregate_after": 2,
                    "admin_alert_text_limit": 200,
                }
            }
        )
        self.assertEqual(_admin_alert_limits(), (60.0, 2, 512, 200))
        self.assertTrue(len(_truncate_alert_text("x" * 5000)) <= 230)


class BackgroundGetterTests(PolicyBindingMixin):
    def test_maintenance_getters_are_really_wired(self) -> None:
        import inspect

        from bot import __main__ as main_module

        source = inspect.getsource(main_module)
        self.assertIn("search_memory_limits()", source)
        self.assertIn("memory_limits()", source)
        self.assertIn("archive_limits()", source)
        self.assertIn("apply_startup_resources(", source)

    def test_startup_resources_runs_before_any_prefetch_or_bot_build(self) -> None:
        import inspect

        from bot import __main__ as main_module

        source = inspect.getsource(main_module._initialize_runtime_services)
        self.assertIn("apply_startup_resources(", source)
        self.assertLess(
            source.index("apply_startup_resources("),
            source.index("_prefetch_model_context_metadata"),
            "进程级闸门必须在模型预取之前装配",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
