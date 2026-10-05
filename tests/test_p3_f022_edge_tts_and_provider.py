"""P3-5 / F-022：``edge-tts`` 依赖声明要一致；TTS provider 要有显式选择项。

复现的两个原缺陷
----------------
1. **两个锁文件已分叉**（C3-03）：``requirements.lock`` 里有 ``edge-tts==7.2.8``，
   ``pyproject.toml`` **没声明**、``uv.lock`` 里**没有**这个包（连它的传递依赖
   ``tabulate`` 也没有）。容器（``Dockerfile`` 从 requirements.lock 装）能跑，
   任何按 pyproject/uv.lock 构建的路径都装不上——``doubao_tts.py`` 选中 Edge
   provider 时 ``import edge_tts`` 直接失败。
2. **provider 靠隐式形状判断**：``tts.speaker`` 写成 ``zh-TW-HsiaoChenNeural``
   就走 Edge、写成别的就走豆包。音色名是用户随手填的字段，拿它的**形状**去决定
   走哪个 provider，既不可读也没法显式表达意图。

修复
----
* ``pyproject.toml`` 声明 ``edge-tts>=7.2``，``uv.lock`` 补上 ``edge-tts`` 与
  ``tabulate`` 两个包并把依赖挂到根包上，``requirements.lock`` 里那句"本地追加"
  的注释改成"两个锁文件已一致"。
* 新增显式配置键 ``tts.provider``（``doubao`` / ``edge``，**留空 = 今天的隐式
  行为**）。隐式的 ``speaker`` 形状判断保留为兼容回退。

用例里**不导入**新符号（provider 名与配置键在下面独立写一遍），这样这份用例在
**未修复**的代码上也能跑起来并如实变红，而不是在 import 阶段就炸掉。
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.config import Settings
from bot.services.doubao_tts import DoubaoTTSService

ROOT = Path(__file__).resolve().parents[1]

#: 显式 provider 的两个取值（期望值独立写一遍，不从被测实现里导入）。
TTS_PROVIDER_DOUBAO = "doubao"
TTS_PROVIDER_EDGE = "edge"
#: 隐式回退认的 Edge 音色名形状（实现里是 ``_EDGE_VOICE_RE``；这里独立写一遍）。
EDGE_VOICE = "zh-TW-HsiaoChenNeural"


def _settings() -> Settings:
    return Settings(_env_file=None)


# --------------------------------------------------------------------------- #
# ① 依赖：pyproject 声明 + 两个锁文件一致
# --------------------------------------------------------------------------- #


def _requirements_lock_pins() -> dict[str, str]:
    """``requirements.lock`` → {归一化名: 版本}（忽略环境标记与注释）。"""

    pins: dict[str, str] = {}
    for raw in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        name = re.split(r"[\s;\[]", name)[0].strip().lower().replace("_", "-")
        # ``x==1 ; marker`` 的版本段里可能带着环境标记。
        version = re.split(r"[;\s]", version.strip())[0]
        pins[name] = version
    return pins


def _uv_lock_pins() -> dict[str, str]:
    """``uv.lock`` → {归一化名: 版本}（解析 ``[[package]] name/version`` 块）。"""

    text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    return {
        name.lower().replace("_", "-"): version
        for name, version in re.findall(
            r'\[\[package\]\]\nname = "([^"]+)"\nversion = "([^"]+)"', text
        )
    }


def _uv_root_block(text: str) -> str:
    return text.split('name = "smart-group-bot"', 1)[1]


def _uv_lock_root_dependencies() -> set[str]:
    block = _uv_root_block((ROOT / "uv.lock").read_text(encoding="utf-8")).split(
        "dependencies = [", 1
    )[1].split("]", 1)[0]
    return {name.lower().replace("_", "-") for name in re.findall(r'\{ name = "([^"]+)"', block)}


def _uv_lock_requirements_dist() -> set[str]:
    metadata = _uv_root_block((ROOT / "uv.lock").read_text(encoding="utf-8")).split(
        "[package.metadata]", 1
    )[1]
    block = metadata.split("requires-dist = [", 1)[1].split("]", 1)[0]
    return {
        name.lower().replace("_", "-")
        for name in re.findall(r'\{ name = "([^"]+)"', block)
    }


class DependencyParityTests(unittest.TestCase):
    """① 依赖在 pyproject / uv.lock / requirements.lock 三处一致。"""

    def test_pyproject_declares_edge_tts(self) -> None:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = {
            re.split(r"[<>=!~\[; ]", item.strip())[0].lower().replace("_", "-")
            for item in data["project"]["dependencies"]
        }
        self.assertIn("edge-tts", declared, "pyproject.toml 必须声明 edge-tts")

    def test_edge_tts_is_pinned_in_both_lock_files(self) -> None:
        requirements = _requirements_lock_pins()
        uv = _uv_lock_pins()
        self.assertIn("edge-tts", requirements)
        self.assertIn("edge-tts", uv, "uv.lock 里没有 edge-tts（两个锁文件已分叉）")
        self.assertEqual(uv["edge-tts"], requirements["edge-tts"])

    def test_uv_lock_root_package_depends_on_edge_tts(self) -> None:
        self.assertIn("edge-tts", _uv_lock_root_dependencies())
        self.assertIn("edge-tts", _uv_lock_requirements_dist())

    def test_uv_lock_covers_edge_tts_transitive_deps(self) -> None:
        """edge-tts 自己的依赖也必须在 uv.lock 里（否则 uv sync 装不上）。"""

        text = (ROOT / "uv.lock").read_text(encoding="utf-8")
        if 'name = "edge-tts"' not in text:
            self.fail("uv.lock 里没有 edge-tts")
        block = text.split('name = "edge-tts"', 1)[1].split("\n[[package]]", 1)[0]
        uv = _uv_lock_pins()
        for name in re.findall(r'\{ name = "([^"]+)"', block):
            self.assertIn(
                name.lower().replace("_", "-"),
                uv,
                f"edge-tts 的依赖 {name} 在 uv.lock 里缺失",
            )

    def test_the_two_lock_files_agree_on_every_common_package(self) -> None:
        """两个锁文件的公共包版本必须一致（不允许再分叉）。"""

        requirements = _requirements_lock_pins()
        uv = _uv_lock_pins()
        missing = sorted(set(requirements) - set(uv))
        self.assertEqual(missing, [], f"requirements.lock 有、uv.lock 没有：{missing}")
        mismatched = {
            name: (requirements[name], uv[name])
            for name in set(requirements) & set(uv)
            if requirements[name] != uv[name]
        }
        self.assertEqual(mismatched, {}, f"两个锁文件版本不一致：{mismatched}")

    def test_edge_tts_is_importable_in_this_environment(self) -> None:
        """容器构建后能 import（本地环境里也直接验一次真导入）。"""

        import edge_tts  # noqa: F401

        self.assertTrue(hasattr(edge_tts, "Communicate"))


# --------------------------------------------------------------------------- #
# ② provider：显式选择项 + 隐式兼容回退
# --------------------------------------------------------------------------- #


def _service(**overrides) -> DoubaoTTSService:
    """用鸭子类型的 settings 构造服务（``DoubaoTTSService`` 只用 ``getattr``）。"""

    fields = {
        "enabled": True,
        "api_base": "https://openspeech.bytedance.com",
        "app_id": "",
        "app_key": "",
        "access_key": "",
        "resource_id": "seed-tts-2.0",
        "model": "",
        "speaker": "",
        "audio_format": "ogg_opus",
        "sample_rate": 24000,
        "bit_rate": 0,
        "emotion": "",
        "emotion_scale": 4,
        "speech_rate": 0,
        "loudness_rate": 0,
        "silence_duration_ms": 0,
        "http_timeout_sec": 20.0,
        "max_text_length": 500,
        # F-022 的新键；老代码里没这个字段也无妨（getattr 有默认值）。
        "provider": "",
    }
    for key, value in overrides.items():
        fields[key] = value
    settings = SimpleNamespace(
        **{f"doubao_tts_{key}": value for key, value in fields.items()}
    )
    return DoubaoTTSService(settings)


class TTSProviderSelectionTests(unittest.TestCase):
    """② 显式 provider 生效；③ 不配时旧的隐式行为不变。"""

    def test_provider_key_defaults_to_unset(self) -> None:
        # None（字段不存在）也要红：要求"字段存在且默认未设置"。
        self.assertEqual(getattr(_settings(), "doubao_tts_provider", None), "")

    def test_explicit_edge_provider_wins_over_the_speaker_shape(self) -> None:
        """显式 edge：一个**不像** Edge 音色的 speaker 也照样走 Edge。"""

        service = _service(provider=TTS_PROVIDER_EDGE, speaker="custom-voice")
        self.assertEqual(service.edge_voice, "custom-voice")
        self.assertTrue(service.available)

    def test_explicit_edge_provider_overrides_doubao_credentials(self) -> None:
        """显式 edge：配了豆包凭据也走 Edge（显式选择优先于隐式回退）。"""

        service = _service(
            provider=TTS_PROVIDER_EDGE,
            speaker=EDGE_VOICE,
            app_id="app-id",
            access_key="access-key",
        )
        self.assertEqual(service.edge_voice, EDGE_VOICE)

    def test_explicit_doubao_provider_ignores_an_edge_looking_speaker(self) -> None:
        """显式 doubao：speaker 长得再像 Edge 音色也不切 provider。"""

        service = _service(
            provider=TTS_PROVIDER_DOUBAO,
            speaker=EDGE_VOICE,
            app_id="app-id",
            access_key="access-key",
        )
        self.assertEqual(service.edge_voice, "")

    def test_explicit_doubao_provider_without_credentials_is_not_available(self) -> None:
        """显式 doubao 但没配凭据 → 不可用（而不是偷偷回退到 Edge）。"""

        service = _service(provider=TTS_PROVIDER_DOUBAO, speaker=EDGE_VOICE)
        self.assertEqual(service.edge_voice, "")
        self.assertFalse(service.available)

    def test_unset_provider_keeps_the_legacy_shape_based_fallback(self) -> None:
        """③ 不配 provider 时，旧的隐式行为一字不变。"""

        implicit_edge = _service(speaker=EDGE_VOICE)
        self.assertEqual(getattr(implicit_edge, "provider", None), "")
        self.assertEqual(implicit_edge.edge_voice, EDGE_VOICE)
        self.assertTrue(implicit_edge.available)

        implicit_doubao = _service(
            speaker="voice_1", app_id="app-id", access_key="access-key"
        )
        self.assertEqual(implicit_doubao.edge_voice, "")

    def test_doubao_credentials_still_win_over_the_voice_shape_when_unset(self) -> None:
        """凭据齐全时豆包优先（今天就是这样，不能因为加了键就变）。"""

        service = _service(
            speaker=EDGE_VOICE, app_id="app-id", access_key="access-key"
        )
        self.assertEqual(service.edge_voice, "")

    def test_unknown_provider_value_falls_back_to_the_legacy_behaviour(self) -> None:
        service = _service(provider="azure", speaker=EDGE_VOICE)
        self.assertEqual(getattr(service, "provider", None), "")
        self.assertEqual(service.edge_voice, EDGE_VOICE)


class TTSProviderConfigSurfaceTests(unittest.TestCase):
    """运行时配置（Mini App）能读到/写到这个键，默认值保持隐式口径。"""

    def test_runtime_config_exposes_provider_with_an_empty_default(self) -> None:
        from bot.services.runtime_config import TTSSettingsConfig

        self.assertEqual(getattr(TTSSettingsConfig(), "provider", None), "")
        self.assertEqual(
            TTSSettingsConfig(provider=TTS_PROVIDER_EDGE).provider, TTS_PROVIDER_EDGE
        )

    def test_runtime_config_applies_provider_to_settings(self) -> None:
        from bot.services.runtime_config import RuntimeConfig, TTSSettingsConfig

        settings = _settings()
        config = RuntimeConfig(tts=TTSSettingsConfig(provider=TTS_PROVIDER_EDGE))
        config.apply_to_settings(settings, apply_prompts=False)
        self.assertEqual(getattr(settings, "doubao_tts_provider", None), TTS_PROVIDER_EDGE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
