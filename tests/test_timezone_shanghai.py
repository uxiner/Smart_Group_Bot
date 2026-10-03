"""时区口径回归：容器跑 UTC 时，机器人也必须按中国时间说话、按中国时间算时段。

生产原型问题（2026-10-04 用户报）：容器 TZ 未设（进程本地时间 = UTC），

* ``[CURRENT_TIME]`` 块用 ``datetime.now().astimezone()`` 取「本地时间」，于是机器人
  告诉用户的「现在/今天」比中国时间**早 8 小时**；
* 主动话题的静默时段（``proactive_quiet_hours_*``）整体平移 8 小时——半夜活跃、
  下午装死。

两道防线各测一半：**代码**里时钟块改为不依赖容器时区（下面用子进程把 TZ 强制成 UTC
来证明），**部署**上容器必须声明 ``TZ=Asia/Shanghai``（compose 声明 + 镜像有 tzdata，
否则 TZ 会被静默忽略）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta

from bot.utils.runtime_context import build_current_time_context
from bot.utils.timezone import now_shanghai

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse_block(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("[") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


class CurrentTimeBlockTests(unittest.TestCase):
    def test_block_reports_asia_shanghai(self) -> None:
        fields = _parse_block(build_current_time_context())
        self.assertEqual(fields["timezone"], "Asia/Shanghai (UTC+08:00)")
        self.assertIn("local_datetime", fields)
        self.assertIn("utc_datetime", fields)

    def test_block_local_time_tracks_real_china_time(self) -> None:
        fields = _parse_block(build_current_time_context())
        reported = datetime.strptime(fields["local_datetime"], "%Y-%m-%d %H:%M:%S")
        expected = now_shanghai().replace(tzinfo=None, microsecond=0)
        self.assertLess(abs((reported - expected).total_seconds()), 120)
        # UTC 行必须比本地行早 8 小时（不是被当成"本地时间"）
        utc_reported = datetime.strptime(fields["utc_datetime"], "%Y-%m-%d %H:%M:%S")
        self.assertAlmostEqual(
            (reported - utc_reported).total_seconds(), 8 * 3600, delta=120
        )

    def test_block_weekday_is_china_weekday(self) -> None:
        fields = _parse_block(build_current_time_context())
        names = ("一", "二", "三", "四", "五", "六", "日")
        expected = f"星期{names[now_shanghai().weekday()]}"
        self.assertEqual(fields["local_weekday_cn"], expected)

    def test_block_is_china_time_when_process_tz_is_utc(self) -> None:
        """进程时区 = UTC（就是生产容器改造前的情况）时，仍然必须给中国时间。

        用子进程把 TZ 强制成 UTC 跑一遍，避免在测试进程里 tzset 影响其它用例。
        """
        code = (
            "import json, sys;"
            f"sys.path.insert(0, {_REPO_ROOT!r});"
            "from bot.utils.runtime_context import build_current_time_context;"
            "print(build_current_time_context())"
        )
        env = {**os.environ, "TZ": "UTC", "PYTHONPATH": _REPO_ROOT}
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        fields = _parse_block(proc.stdout)
        self.assertEqual(fields["timezone"], "Asia/Shanghai (UTC+08:00)", proc.stdout)
        reported = datetime.strptime(fields["local_datetime"], "%Y-%m-%d %H:%M:%S")
        self.assertLess(
            abs((reported - now_shanghai().replace(tzinfo=None, microsecond=0)).total_seconds()),
            180,
            "进程时区为 UTC 时时钟块必须仍是中国时间",
        )


class ShanghaiOffsetTests(unittest.TestCase):
    def test_now_shanghai_offset_is_plus_eight(self) -> None:
        self.assertEqual(now_shanghai().utcoffset(), timedelta(hours=8))

    def test_shanghai_tz_is_resolvable_not_fallback_only(self) -> None:
        from bot.utils.timezone import SHANGHAI_TZ

        self.assertEqual(datetime(2026, 10, 4, tzinfo=SHANGHAI_TZ).utcoffset(), timedelta(hours=8))


class DeploymentTimezoneTests(unittest.TestCase):
    """部署面防线：容器必须声明并具备中国时区，否则 TZ 会被静默忽略。"""

    def test_compose_declares_asia_shanghai(self) -> None:
        path = os.path.join(_REPO_ROOT, "docker-compose.yml")
        with open(path, encoding="utf-8") as handle:
            compose = handle.read()
        self.assertIn("TZ:", compose, "compose 必须声明容器时区")
        self.assertIn("Asia/Shanghai", compose, "容器时区必须是 Asia/Shanghai")

    def test_image_installs_tzdata(self) -> None:
        path = os.path.join(_REPO_ROOT, "Dockerfile")
        with open(path, encoding="utf-8") as handle:
            dockerfile = handle.read()
        install_lines = [
            line for line in dockerfile.splitlines() if "apt-get install" in line
        ]
        self.assertTrue(install_lines, "Dockerfile 里应有 apt-get install 行")
        self.assertTrue(
            any("tzdata" in line for line in install_lines),
            "镜像必须装 tzdata，否则 TZ=Asia/Shanghai 会被静默忽略",
        )


if __name__ == "__main__":
    unittest.main()
