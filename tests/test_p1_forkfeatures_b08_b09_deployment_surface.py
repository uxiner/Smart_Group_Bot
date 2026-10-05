"""修复批 P1-2 / B-08 + B-09：部署面（compose/Dockerfile/.env.example）与文档对齐。

复现的原缺陷：

* **B-08** ``docker-compose.yml`` 挂 ``./bot:/app/bot:ro``，把 ``Dockerfile`` 里
  ``COPY --chown=app:app bot ./bot`` 的成果整个盖住：镜像里的代码成死代码，
  ``docker compose build && up`` 不再决定跑什么；宿主机 ``git pull`` + 重启就换掉
  生产代码，中间没有 diff 确认也没有版本 pin；uid 不匹配时（容器跑 ``APP_UID=1000``
  而镜像账号是 10001）宿主 ``bot/`` 不可读就直接启动失败。
* **B-09** ``.env.example`` 里的 ``AV_REVERSE_*`` 是**已删除功能**的死配置
  （``bot/services/av_image_lookup.py`` 等三个文件在二开后期已 ``D``），代码里零消费；
  而注释描述的是一个高风险外发行为（原始图片字节 multipart POST 发往第三方），
  运维照着设置只会得到「什么都没发生」，出事故时极具误导性。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _volumes_block(compose: str) -> str:
    start = compose.index("\n    volumes:\n")
    end = compose.index("\n    ports:", start)
    return compose[start:end]


class CodeIsNotBindMountedTests(unittest.TestCase):
    """B-08：运行代码 = 镜像里的代码。"""

    def setUp(self) -> None:
        self.compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.volumes = _volumes_block(self.compose)

    def test_compose_does_not_mount_the_code_directory(self) -> None:
        self.assertNotRegex(
            self.volumes,
            r"(?m)^\s*-\s*\./bot\s*:",
            "compose 不得把 ./bot 挂进 /app/bot：那会让镜像里的代码变成死代码",
        )
        self.assertNotIn("/app/bot", self.volumes)

    def test_image_still_bakes_the_code_in(self) -> None:
        self.assertRegex(
            self.dockerfile,
            r"(?m)^COPY\s+--chown=app:app\s+bot\s+\./bot\s*$",
            "镜像必须仍然 COPY bot，否则删掉挂载后容器里没有代码",
        )

    def test_the_remaining_mounts_are_justified(self) -> None:
        """仍挂载的三样各有理由：数据、运行时提示词、一次性导入。"""

        for mount in (
            "./data:/app/data",
            "./prompt:/app/prompt:ro",
            "./config.toml:/app/config.toml:ro",
        ):
            with self.subTest(mount=mount):
                self.assertIn(mount, self.volumes)
        self.assertIn("runtime_config", self.volumes)

    def test_hot_edit_path_is_documented_in_readme(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docker-compose.override.yml", readme)
        self.assertIn("COPY bot ./bot", readme)
        self.assertIn("不**再挂 `./bot`", readme)


class DeadEnvConfigTests(unittest.TestCase):
    """B-09：``.env.example`` 不得承诺代码里不存在的行为。"""

    def setUp(self) -> None:
        self.env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    def test_av_reverse_settings_are_gone(self) -> None:
        self.assertNotIn("AV_REVERSE", self.env_example)
        self.assertNotIn("av_reverse", self.env_example)

    def test_every_env_example_key_has_a_real_consumer(self) -> None:
        """B-09 的根因检查：「文档说有、代码没有」的配置项在出事故时极具误导性。

        消费方可以是 Python 生产代码（``bot/**.py`` / ``start.py``）或
        ``docker-compose.yml``（容器级环境与 healthcheck，例如
        ``MINIAPP_LISTEN_PORT``）。两边都找不到 = 死配置。
        """

        python_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "bot").rglob("*.py"))
        ) + (ROOT / "start.py").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        keys = sorted(set(re.findall(r"(?m)^([A-Z][A-Z0-9_]{2,})=", self.env_example)))
        self.assertTrue(keys, "``.env.example`` 至少要列几个键")
        for key in keys:
            with self.subTest(key=key):
                self.assertTrue(
                    key in python_source or key in compose,
                    f"``.env.example`` 里的 {key} 在生产代码与 compose 里都没有消费者"
                    "（死配置，B-09）",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
