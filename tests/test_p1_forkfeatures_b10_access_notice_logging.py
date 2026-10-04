"""修复批 P1-2 / B-10：权限拒绝通知发送失败必须可定位，不能降级成一行 f-string。

复现的原缺陷（``AUDIT-B`` B-10）：

``bot/services/authz.py:_send_access_notice`` 里 fork 加了一个 catch-all，把
「权限不足 / 当前群未授权 / 群内才可用」这条**唯一**的用户可见提示的发送失败变成

    import logging
    logging.getLogger(__name__).warning(f"Suppressed notice failure: {e}")

问题有两层：① 用户可能完全收不到任何反馈；② 日志只有 warning、没有 traceback、
没有上下文（是哪个 title / action 失败都不知道），事后无法定位。另外
``import logging`` 写在函数体内、用 f-string 而不是 ``%s`` 惰性格式化，与全文件
其余部分的写法都不同。
"""

from __future__ import annotations

import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.services import authz

ROOT = Path(authz.__file__).resolve()


class AccessNoticeLoggingTests(unittest.IsolatedAsyncioTestCase):
    def _message(self, error: Exception) -> SimpleNamespace:
        message = SimpleNamespace(answer=AsyncMock(side_effect=error))
        return message

    async def test_send_failure_logs_a_traceback_with_context(self) -> None:
        message = self._message(RuntimeError("telegram is down"))
        with self.assertLogs("bot.services.authz", level="WARNING") as captured:
            await authz._send_access_notice(
                message,
                SimpleNamespace(),  # type: ignore[arg-type]
                title="权限不足",
                action="仅最高管理员可使用该命令。",
            )
        self.assertEqual(len(captured.output), 1)
        line = captured.output[0]
        # 上下文：是哪条通知失败
        self.assertIn("access notice send failed", line)
        self.assertIn("权限不足", line)
        self.assertIn("仅最高管理员可使用该命令。", line)
        # 旧实现把异常对象 f-string 拼进 message：**没有任何 traceback**。
        # 现在 exc_info=True → 完整调用栈必须在日志里。
        self.assertNotRegex(
            line.split("Traceback")[0],
            r"telegram is down",
            "异常文本不得靠 f-string 拼进 message（旧实现的做法）",
        )
        self.assertIn("Traceback", line)
        self.assertIn("RuntimeError: telegram is down", line)

    async def test_send_failure_never_propagates(self) -> None:
        """行为不变：通知发不出去也不能让命令处理崩掉。"""

        message = self._message(RuntimeError("telegram is down"))
        with self.assertLogs("bot.services.authz", level="WARNING"):
            result = await authz._send_access_notice(
                message,
                SimpleNamespace(),  # type: ignore[arg-type]
                title="当前群未授权",
                action="请联系最高管理员完成群组授权。",
            )
        self.assertIsNone(result)

    async def test_auto_delete_failure_is_logged_too(self) -> None:
        """自动删除排队失败也走同一条 warning（以前也在这里被吞掉）。"""

        message = SimpleNamespace(answer=AsyncMock(return_value="sent"))
        with self.assertLogs("bot.services.authz", level="WARNING") as captured:
            with unittest.mock.patch.object(
                authz, "_schedule_auto_delete", new=AsyncMock(side_effect=RuntimeError("nope"))
            ):
                await authz._send_access_notice(
                    message,
                    SimpleNamespace(),  # type: ignore[arg-type]
                    title="无法执行",
                    action="该命令仅可在群内使用。",
                )
        self.assertIn("Traceback", captured.output[0])


class SourceHygieneTests(unittest.TestCase):
    def test_module_level_logger_and_lazy_formatting(self) -> None:
        source = ROOT.read_text(encoding="utf-8")
        self.assertIn("log = logging.getLogger(__name__)", source)
        self.assertNotIn("import logging\n", source.split("log = logging.getLogger")[-1])
        self.assertNotIn("Suppressed notice failure", source)
        self.assertNotRegex(
            source,
            r'log\.\w+\(f"',
            "日志必须用 %s 惰性格式化（f-string 会让未触发的分支也付出格式化代价）",
        )


if __name__ == "__main__":  # pragma: no cover
    logging.getLogger(__name__)
    unittest.main()
