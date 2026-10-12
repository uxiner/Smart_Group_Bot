"""F1 正式回归：真实 main 失败臂之后，进程级 memory holder 必须回到进入时的原对象。

Linux 集成（64 个非浏览器文件、同一 interpreter、同一进程）里
``tests/test_alert_main_failure_cleanup.py`` 跑真实 ``main()``，把
``memory_holder.init(...)`` 装上的 ``MemoryService`` 留在进程全局；它绑着那个用例
已经被删掉的临时库。排在后面的 ``tests/test_group_moderation_confidence.py`` 于是
从 ``memory_holder.get_optional()`` 拿到"就绪"的服务、走 archive 分支、24 例全红
（单跑该模块 24 passed）。判责为测试卫生，不是产品缺陷。

本文件在**同一进程、同一 interpreter、按顺序**执行父观察到的那个序列：

1. 真实 main 的三条失败臂（``MainFailureCleanupTests``，各自真实临时库 + 真实 engine）；
2. 真实的审核模块 ``GroupModerationConfidenceTests`` 全量（与单跑必须一致）；
3. 每一步之后断言 holder 与进入时**同一个对象**（``is``），而不是"非 None"或"空就行"。

两个原值控制：
* 进入时为 ``None``（历史泄漏场景）；
* 进入时为非 ``None`` 的上游真实对象——必须**原样**还原，且不能被本次清理关掉。

回归判别力：若 ``tests/test_alert_main_failure_cleanup.py`` 的 tearDown 不做
"释放 + 还原"，第 1 步之后的 ``is`` 断言立刻红（泄漏的服务还在 holder 里）。
"""

from __future__ import annotations

import io
import unittest

from bot.services import memory_holder

_FAILURE_ARMS = (
    "tests.test_alert_main_failure_cleanup.MainFailureCleanupTests."
    "test_create_bot_failure_cleans_up",
    "tests.test_alert_main_failure_cleanup.MainFailureCleanupTests."
    "test_identity_bootstrap_failure_cleans_up_before_bot_close",
    "tests.test_alert_main_failure_cleanup.MainFailureCleanupTests."
    "test_warm_cache_failure_cleans_up_and_disposes_engine",
)

_MODERATION_MODULE = (
    "tests.test_group_moderation_confidence.GroupModerationConfidenceTests"
)


class _UpstreamService:
    """上游留下的"真实服务"替身：只关心它是否被原样保留、有没有被误关掉。"""

    def __init__(self) -> None:
        self.shutdown_calls = 0

    async def shutdown(self, *, timeout_seconds: float = 5.0) -> None:
        self.shutdown_calls += 1


class MemoryHolderHygieneV5Tests(unittest.TestCase):
    """holder 卫生：真实 main 失败臂不得把临时库服务泄漏给后续套件。"""

    def _run(self, names) -> unittest.result.TestResult:
        suite = unittest.TestLoader().loadTestsFromNames(list(names))
        return unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0
        ).run(suite)

    def _assert_clean_run(self, step: str, result: unittest.result.TestResult) -> None:
        problems = [
            f"{test}: {detail.strip().splitlines()[-1] if detail.strip() else ''}"
            for test, detail in [*result.failures, *result.errors]
        ]
        self.assertEqual(result.testsRun > 0, True, f"{step}: 一个用例都没跑到")
        self.assertEqual(problems, [], f"{step} 必须与单跑一致（0 失败 0 错误）")

    def test_none_baseline_is_restored_for_the_later_moderation_module(self) -> None:
        incoming = memory_holder._instance
        memory_holder._instance = None
        try:
            # 1) 三条真实失败臂：每条都真的把 MemoryService 装上进程级 holder。
            result = self._run(_FAILURE_ARMS)
            self.assertEqual(result.testsRun, len(_FAILURE_ARMS))
            self._assert_clean_run("真实 main 失败臂", result)
            self.assertIsNone(
                memory_holder.get_optional(),
                "失败臂结束后 holder 还指向本次临时库的 MemoryService："
                "后续套件会走 archive 分支（Linux 集成的 24 例审核红）",
            )

            # 2) 同一进程、紧接着跑真实审核模块全量。
            result = self._run([_MODERATION_MODULE])
            self._assert_clean_run("真实审核模块", result)
            self.assertGreaterEqual(result.testsRun, 24)
            self.assertIsNone(
                memory_holder.get_optional(),
                "审核模块跑完后 holder 不该被它改写",
            )
        finally:
            memory_holder._instance = incoming

    def test_non_none_incoming_holder_survives_verbatim(self) -> None:
        incoming = memory_holder._instance
        upstream = _UpstreamService()
        memory_holder._instance = upstream
        try:
            result = self._run(
                ["tests.test_alert_main_failure_cleanup.MainFailureCleanupTests."
                 "test_create_bot_failure_cleans_up"]
            )
            self._assert_clean_run("真实 main 失败臂（非 None 原值）", result)
            self.assertIs(
                memory_holder.get_optional(), upstream,
                "holder 必须还原成进入时的原对象，而不是无条件置 None",
            )
            self.assertEqual(
                upstream.shutdown_calls, 0,
                "本次清理不能顺手把上游留下的服务关掉（它不是本次装的）",
            )
        finally:
            memory_holder._instance = incoming


if __name__ == "__main__":
    unittest.main()
