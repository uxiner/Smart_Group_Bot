"""P4-4（A-23）：``configure_logging`` 替换 root handler 必须**整段串行**。

现象：改前 ``configure_logging(force=True)`` 只有开头（reaper 检查）和结尾
（发布全局状态）两小段持 ``_LOGGING_STATE_LOCK``，中间「建 listener →
``root.handlers.clear()`` / ``addHandler`` → 关旧 handler」是裸的。两个线程同时
force 重配置时（例如 Mini App 热更新设置撞上关停期的重配置）会各自建一套
listener 往同一个 stdout 写，旧 sink 的半行和新 sink 的半行拼成一条从未发生过的
记录；而且被 ``close()`` 掉的可能是**另一套正在用的** handler。

用例用「A 线程停在临界区中间 → B 线程必须进不去」来钉住这个性质：
这条性质在旧代码上必然失败（B 不需要这把锁，几毫秒就装完了）。
"""

from __future__ import annotations

import logging
import threading
import unittest

from bot.utils import logging_setup


class ConfigureLoggingSerializationTests(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        self._old_handlers = list(root.handlers)
        self._old_level = root.level
        self._old_env = {
            key: __import__("os").environ.get(key)
            for key in ("LOG_TO_FILE", "LOG_FILE_PATH")
        }
        __import__("os").environ.pop("LOG_TO_FILE", None)

    def tearDown(self) -> None:
        import os

        root = logging.getLogger()
        logging_setup.shutdown_logging(timeout=2.0)
        root.handlers = self._old_handlers
        root.setLevel(self._old_level)
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_a_second_reconfiguration_cannot_interleave_with_the_first(self) -> None:
        holder: list[threading.Thread] = []
        entered = threading.Event()
        release = threading.Event()
        original_start = logging_setup._BoundedQueueListener.start

        def blocking_start(self, *args, **kwargs):
            result = original_start(self, *args, **kwargs)
            if threading.current_thread() is holder[0]:
                # 停在「listener 已起、root handler 还没换」的位置：这正是改前
                # 完全没有锁保护的那一段。
                entered.set()
                release.wait(5.0)
            return result

        errors: list[BaseException] = []

        def run_holder() -> None:
            try:
                logging_setup.configure_logging(force=True)
            except BaseException as exc:  # pragma: no cover - 记录后交给断言
                errors.append(exc)

        logging_setup._BoundedQueueListener.start = blocking_start
        try:
            first = threading.Thread(target=run_holder, name="configure-first")
            holder.append(first)
            first.start()
            self.assertTrue(entered.wait(5.0), "第一个重配置没有进入临界区")

            second_done = threading.Event()

            def run_second() -> None:
                try:
                    logging_setup.configure_logging(force=True)
                except BaseException as exc:  # pragma: no cover
                    errors.append(exc)
                finally:
                    second_done.set()

            second = threading.Thread(target=run_second, name="configure-second")
            second.start()
            # 持锁 ⇒ 第二个重配置必须被挡在门外。给足 2 秒的观察窗：旧代码里
            # 第二个根本不需要这把锁，会在几毫秒内完成，于是这里必然失败。
            blocked = not second_done.wait(2.0)
            release.set()
            first.join(10.0)
            second.join(10.0)
        finally:
            release.set()
            logging_setup._BoundedQueueListener.start = original_start

        self.assertEqual(errors, [])
        self.assertTrue(blocked, "第二个 configure_logging 没有被锁挡住：替换不是原子的")
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())

        # 收尾后仍然只有一套在用的 pipeline，且它的 listener 活着。
        root = logging.getLogger()
        self.assertEqual(len(root.handlers), 1, f"root 上应只剩一个 handler：{root.handlers}")
        listener = logging_setup._LOG_LISTENER
        self.assertIsNotNone(listener)
        thread = getattr(listener, "_thread", None)
        self.assertIsNotNone(thread)
        self.assertTrue(thread.is_alive())
        snapshot = logging_setup.logging_resource_health_snapshot()
        self.assertTrue(snapshot["listener_alive"])
        self.assertTrue(snapshot["ok"], f"收尾后 pipeline 应当是健康的：{snapshot}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
