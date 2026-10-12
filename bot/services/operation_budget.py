"""功能操作额度的判定入口（TTS 直发边界用）。

两条约束：

* **同一次逻辑发送只扣一次，但同一次技能调用里的第二次发送仍要扣**。技能入口
  （``SkillService``）与底层 TTS 直发都接了额度，同一条语音链路会经过两层 ——
  中央先扣一次，并留下**单次可用**的凭证，底层 sender 领走它就不再扣；领走即
  失效，所以同一技能调用里第二次 sender 照常扣费。
* **凭证绑定建立它的那个任务**。``ContextVar`` 会被子任务复制 —— 如果只按
  (群, feature) 认凭证，一次准入就能被 ``create_task`` 出来的两个子发送各领一份，
  等于一次付费发两条。所以凭证记下 owner task，只有**原任务**能领；子任务拿到的
  副本领不走，只能自己去扣（cap 用完就拒）。

绝不用公共 arguments/kwargs 上的 skip_budget 布尔（调用方随手就能绕过），
也不用全局"已经扣过"变量（跨群/并发会互相污染）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from bot.services import usage_scope as scopes

log = logging.getLogger(__name__)

_FEATURE = "tts"

#: 当前上下文里**尚未被领取**的凭证：(owner_task, 群, feature)。
#:
#: 不可变元组 + owner task：`ContextVar.set` 每次产生新值，asyncio 建新任务时
#: 复制的是当时的元组（所以子任务看得到），但 owner task 不匹配 —— 子任务领不走，
#: 不会把一次准入变成两条免费发送。
_CREDITS: ContextVar[tuple[tuple[object, int, str], ...]] = ContextVar(
    "operation_budget_credits", default=()
)


def _owner() -> object:
    return asyncio.current_task()


@contextmanager
def admitted(group_id: int, feature: str = _FEATURE) -> Iterator[None]:
    """中央准入成功后建立**绑定当前任务、单次可用**的凭证，退出时严格还原。"""

    key = (_owner(), int(group_id), str(feature))
    token = _CREDITS.set(tuple(_CREDITS.get()) + (key,))
    try:
        yield
    finally:
        _CREDITS.reset(token)


def claim_credit(group_id: int, feature: str = _FEATURE) -> bool:
    """领走本任务留给这个 (群, feature) 的凭证；领走即失效。

    只删**一个**对应凭证；其它群/其它任务的凭证原样保留。
    """

    key = (_owner(), int(group_id), str(feature))
    current = _CREDITS.get()
    if key not in current:
        return False
    rest = list(current)
    rest.remove(key)
    _CREDITS.set(tuple(rest))
    return True


def group_id_for(chat_id: int) -> int | None:
    """把 chat_id 归一成"应套群额度的群 id"，私聊/未知返回 None。"""

    try:
        chat_id = int(chat_id or 0)
    except (TypeError, ValueError):
        return None
    if chat_id < 0:
        # 显式的负 chat_id 就是群：即使当前任务没有（或不是）群 scope，
        # 也不能把它当成系统免单。
        return chat_id
    if chat_id > 0:
        # 显式正 chat_id = 私聊。**不**看 scope —— 继承了别的群 scope 的私聊
        # 依然不能扣那个群的钱。
        return None
    # chat_id == 0：来源不明，只有可信的群 scope 才套额度。
    try:
        scope = scopes.current_usage_scope()
    except Exception:  # pragma: no cover - 取不到就按"不套"处理
        return None
    if str(getattr(scope, "kind", "")) != scopes.SCOPE_GROUP:
        return None
    try:
        group = int(getattr(scope, "group_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    return group if group < 0 else None


async def consume_tts(group_id: int | None) -> str | None:
    """扣一次 tts 操作额度。

    返回 ``None`` 表示可以继续；否则是明确的错误码
    （``operation_budget_denied`` / ``operation_budget_unavailable``）。

    * 私聊/非群返回 None（不套群额度）；
    * 本任务领到中央留下的凭证也返回 None（两层只扣一次）；
    * **共享预算服务没装配 / 装配失败** → 群语音一律 fail-closed，返回
      ``operation_budget_unavailable``。"这台部署没启用可选额度"不能由"共享
      服务缺失"来证明 —— 缺服务意味着预算装配那一步没走完（例如告警启动等前置
      失败），此时放行才是真的绕过。
    """

    if group_id is None:
        return None
    if claim_credit(group_id):
        return None

    from bot.services import group_budget as budget_module

    service = budget_module.shared_service_or_none()
    if service is None:
        log.warning(
            "tts 操作额度未装配，拒绝本次群语音 | group=%s", group_id
        )
        return "operation_budget_unavailable"
    try:
        outcome = await service.consume_operation(
            group_id=group_id, feature=_FEATURE
        )
    except Exception:  # noqa: BLE001 - 存储故障 -> fail closed
        log.warning(
            "tts 操作额度不可用，拒绝本次语音 | group=%s", group_id, exc_info=True
        )
        return "operation_budget_unavailable"
    if not outcome.admitted:
        return "operation_budget_denied"
    return None
