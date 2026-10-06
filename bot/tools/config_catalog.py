"""生成机器可读的运行时配置字段目录（``docs/configuration-fields.json``）。

用 ``python -m bot.tools.config_catalog`` 运行。它**只读** schema，不碰数据库、
不读 ``.env``、不访问网络，所以可以在任何环境里重跑。

清单里每一项都带：``path`` / ``default`` / 上下界 / ``reload_kind`` /
``read_consumers`` / ``api_role`` / ``test_node``。**没有读侧的字段不允许出现在
清单里**——``tests/test_configurable_policy_catalog.py`` 会因此失败，所以"新加
了一个参数但没人读"这种半成品不可能悄悄合进来。

范围：本次新增的运营段（``private_chat`` / ``economy`` / ``activity`` /
``checkin_reminder`` / ``display`` / ``resources``）与本轮新增的审核部署绑定
字段（``moderation.log_channel_id`` / ``moderation.review_handover_mention``）。
既有的 ``models`` / ``bot`` / ``tts`` / … 段不在本清单里，它们由各自的
Mini App 面板与既有测试覆盖。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from bot.services.policy_runtime import consumer_paths
from bot.services.runtime_config import (
    RESTART_REQUIRED_PATHS,
    ActivitySettingsConfig,
    AdminOpsSettingsConfig,
    CheckinReminderSettingsConfig,
    DisplaySettingsConfig,
    EconomySettingsConfig,
    GroupOpsSettingsConfig,
    PrivateChatSettingsConfig,
    ResourceSettingsConfig,
    TelegramSendSettingsConfig,
)

#: 本轮新增的段 → 读侧视图类型。
SECTION_MODELS: dict[str, type[BaseModel]] = {
    "private_chat": PrivateChatSettingsConfig,
    "economy": EconomySettingsConfig,
    "activity": ActivitySettingsConfig,
    "checkin_reminder": CheckinReminderSettingsConfig,
    "display": DisplaySettingsConfig,
    "resources": ResourceSettingsConfig,
    "admin_ops": AdminOpsSettingsConfig,
    "group_ops": GroupOpsSettingsConfig,
    "telegram_send": TelegramSendSettingsConfig,
}

#: 审核段里本轮新增的两个**部署绑定**字段。
MODERATION_FIELDS: tuple[str, ...] = (
    "log_channel_id",
    "review_handover_mention",
)

#: 谁能编辑。全局运营/资源/交接字段只有最高管理员；群管理员本来就看不到
#: ``/api/v1/settings``（``settings_api.register_settings_routes`` 的
#: ``@authenticated`` 装饰器），这里只是把契约写进机器清单。
API_ROLE = "super_admin"

#: 覆盖该字段的测试节点（人可读的定位，方便 review 时逐条核）。
TEST_NODES: dict[str, str] = {
    "moderation.log_channel_id": "tests/test_moderation_log_channel_docs.py",
    "moderation.review_handover_mention": "tests/test_moderation_log_channel.py",
    "activity.weekly_reward_points": "tests/test_activity_incentive.py",
    "economy.lottery_prizes": "tests/test_point_shop.py",
    "resources.llm_request_capacity": "tests/test_startup_resources.py",
}

DEFAULT_TEST_NODE = "tests/test_configurable_policy_catalog.py"

#: 上下界写在 ``field_validator`` 里（而不是 ``Field(...)``）的字段，在这里补齐，
#: 免得机器清单里出现"没有边界"的条目而让人误以为可以随便填。
VALIDATOR_BOUNDS: dict[str, dict[str, Any]] = {
    # 0 = 未配置；否则必须是 -100 开头的频道 chat id。
    "moderation.log_channel_id": {"allowed": [0, "(-1009999999999, -1000000000000]"]},
}


#: pydantic 把 ``Field(ge=…)`` 变成 ``annotated_types.Ge`` 这类约束对象；
#: 它们的类名就是约束名，值在同名属性上。
_BOUND_KINDS = ("ge", "gt", "le", "lt")


def _bounds(field: FieldInfo, path: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    result.update(VALIDATOR_BOUNDS.get(path, {}))
    for meta in field.metadata or ():
        name = type(meta).__name__.lower()
        if name in _BOUND_KINDS:
            result.setdefault(name, getattr(meta, name, None))
        elif name == "len":
            result.setdefault("min_length", getattr(meta, "min_length", None))
            result.setdefault("max_length", getattr(meta, "max_length", None))
        elif name == "multipleof":
            result.setdefault("multiple_of", getattr(meta, "multiple_of", None))
    if getattr(field, "default_factory", None) is not None:
        result["default_from_factory"] = True
    return result


def _jsonable(value: Any) -> Any:
    """把 pydantic 对象 / 元组 / 集合递归成纯 JSON 结构。"""

    if isinstance(value, BaseModel):
        return {key: _jsonable(item) for key, item in value.model_dump().items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _default(field: FieldInfo) -> Any:
    if getattr(field, "default_factory", None) is not None:
        try:
            return _jsonable(field.default_factory())  # type: ignore[misc]
        except TypeError:  # pragma: no cover - 防御
            return None
    return _jsonable(field.default)


def _reload_kind(path: str, field: FieldInfo) -> str:
    extra = field.json_schema_extra
    if isinstance(extra, dict) and extra.get("reload_kind"):
        return str(extra["reload_kind"])
    return "restart" if path in RESTART_REQUIRED_PATHS else "hot"


def _unit(hint: str) -> str:
    return str(hint or "")


def build_catalog() -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for section, model in SECTION_MODELS.items():
        for name, field in model.model_fields.items():
            path = f"{section}.{name}"
            entries.append(
                {
                    "path": path,
                    "type": _json_type(field.annotation),
                    "default": _default(field),
                    "bounds": _bounds(field, path),
                    "reload_kind": _reload_kind(path, field),
                    "unit": _unit(str(field.description or "")),
                    "read_consumers": list(consumer_paths(path)),
                    "api_role": API_ROLE,
                    "test_node": TEST_NODES.get(path, DEFAULT_TEST_NODE),
                }
            )

    from bot.services.runtime_config import ModerationSettingsConfig

    moderation_defaults = ModerationSettingsConfig()
    for name in MODERATION_FIELDS:
        field = ModerationSettingsConfig.model_fields[name]
        path = f"moderation.{name}"
        entries.append(
            {
                "path": path,
                "type": _json_type(field.annotation),
                "default": getattr(moderation_defaults, name),
                "bounds": _bounds(field, path),
                "reload_kind": "hot",
                "unit": "",
                "read_consumers": list(consumer_paths(path)),
                "api_role": API_ROLE,
                "test_node": TEST_NODES.get(path, DEFAULT_TEST_NODE),
            }
        )

    entries.sort(key=lambda item: item["path"])
    return {
        "schema_version": 1,
        "generated_by": "python -m bot.tools.config_catalog",
        "api_role_note": (
            "global sections are editable by the super admin only; group admins "
            "cannot read or write /api/v1/settings"
        ),
        "restart_required_paths": list(RESTART_REQUIRED_PATHS),
        "fields": entries,
    }


def _json_type(annotation: Any) -> str:
    """容器优先——``list[int]`` 的 str() 里也有 ``int``，顺序反了就全判错。"""

    origin = get_origin(annotation)
    if origin in (list, tuple, set, frozenset):
        return "array"
    if origin is dict:
        return "object"
    if annotation is bool:
        return "boolean"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is str:
        return "string"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    return "string"


def render(catalog: dict[str, Any] | None = None) -> str:
    return json.dumps(catalog or build_catalog(), ensure_ascii=False, indent=2) + "\n"


def write(target: str | Path = "docs/configuration-fields.json") -> Path:
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    target = args[0] if args else "docs/configuration-fields.json"
    written = write(target)
    print(f"wrote {written}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
