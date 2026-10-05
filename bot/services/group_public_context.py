"""第 3 期 B 项：私聊装配时参考「该用户在已授权群里的公开记录」（**只读**）。

方向规则（隐私红线，本期最重要）：

* **群 → 私聊：允许。** 群里公开说过的话，本来就对该群成员可见；私聊里参考它，
  不扩大任何可见性。注入时必须**标明来源是群聊公开记录**
  （``[群聊公开记录 · 群名/群id]``），不能让模型把群里的公开内容当成对方在私聊里
  说过的话。
* **私聊 → 群：默认禁止，本期不实现读取。** 见
  ``bot.services.search_memory.group_can_read_private_history`` 与本模块
  :func:`assert_no_private_content`——群聊侧装配路径不许读 ``private_chat_messages``。

实现上刻意**复用现有检索能力**，不另造一套：

* 取数走 ``MemoryService.recall_archive``（同一个 ``group_message_archive`` 全文索引 /
  向量索引，且它本身就带保留期过滤与「已被审核删除的消息不再召回」的 F-016 口径）；
* ``MemoryService`` 从 ``memory_holder.get_optional()`` 拿——没就绪（干跑 / 单测 /
  启动未完成）时**不注入**，绝不因为「拿不到记忆服务」而报错。

调用方必须传入 ``group_ids``：**该用户已被确认可访问的群**。私聊准入判定本来就会对
每个授权群打一次 ``getChatMember``（``private_chat.resolve_access``），把确认命中的
群 id 顺带带出来，这里就不再重复查询，也不会把「用户不在的群」的公开内容读进来。

只读：本模块**不写**归档、不改任何内容、不做保留策略的任何决定。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from bot.services import memory_holder
from bot.utils.security import wrap_untrusted_multiline

log = logging.getLogger(__name__)

#: 注入块的标记（也是「这是群聊公开记录」的来源标注前缀）
GROUP_PUBLIC_BLOCK = "[群聊公开记录]"

#: 来源标注模板：``[群聊公开记录 · 群名/群id]``。
GROUP_PUBLIC_SOURCE_TEMPLATE = "[群聊公开记录 · {label}]"

#: 单次最多参考几个群（每个群一次检索，防止一句话打出十几个检索）
GROUP_PUBLIC_MAX_GROUPS = 3
#: 每个群最多取几条
GROUP_PUBLIC_PER_GROUP = 3
#: 单次装配最多带几条（跨群合计）
GROUP_PUBLIC_MAX_RECORDS = 6
#: 单条正文最多注入多少字符（留档/注入都要截断）
GROUP_PUBLIC_CONTENT_MAX_CHARS = 400


def _bounded_text(value: Any, max_len: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if max_len <= 0:
        return ""
    return text[:max_len]


def normalize_group_ids(values: Iterable[Any] | None) -> list[int]:
    """去重、只留整数、保持原顺序（调用方给的顺序=相关度顺序）。"""

    out: list[int] = []
    for value in values or []:
        try:
            group_id = int(value)
        except (TypeError, ValueError):
            continue
        if group_id in out:
            continue
        out.append(group_id)
    return out


def source_label(group_id: int, group_title: str = "") -> str:
    """来源标注里的 ``群名/群id`` 部分。"""

    title = _bounded_text(group_title, 60)
    if title:
        return f"{title}/{int(group_id)}"
    return str(int(group_id))


async def load_group_titles(
    session: Any, group_ids: Iterable[Any] | None
) -> dict[int, str]:
    """取群名（best-effort，只用于来源标注）：读 ``groups.title``。

    读失败/没有行都返回空 dict——来源标注退化成 ``群id``，功能不受影响。
    """

    ids = normalize_group_ids(group_ids)
    if not ids or session is None:
        return {}
    try:
        from sqlalchemy import select

        from bot.db.models import Group

        result = await session.execute(
            select(Group.id, Group.title).where(Group.id.in_(ids))
        )
        return {
            int(row[0]): str(row[1] or "")
            for row in result.all()
            if str(row[1] or "").strip()
        }
    except Exception as exc:
        log.debug("group public context: 群名查询失败（只用群 id 标注） | error=%s", exc)
        return {}


def _record_lines(
    item: dict[str, Any],
    *,
    label_map: dict[int, str],
    content_max_chars: int,
) -> list[str]:
    try:
        group_id = int(item.get("group_id") or 0)
    except (TypeError, ValueError):
        group_id = 0
    title = str(item.get("group_title") or label_map.get(group_id) or "")
    header = GROUP_PUBLIC_SOURCE_TEMPLATE.format(label=source_label(group_id, title))
    sent_at = _bounded_text(item.get("sent_at"), 32)
    sender = _bounded_text(item.get("sender_name"), 40) or "某成员"
    content = _bounded_text(item.get("content"), max(0, int(content_max_chars)))
    if not content:
        return []
    stamp = f"{sent_at} " if sent_at else ""
    return [
        wrap_untrusted_multiline(
            GROUP_PUBLIC_UNTRUSTED_LABEL, f"{header} {stamp}{sender}：{content}"
        )
    ]


GROUP_PUBLIC_HEADER = (
    "下面这些是该用户**所在群**里**公开**的讨论片段（只读参考）——"
    "**可能来自群里的其他成员**，不代表都是他本人说的。"
    "每一条都标了来源群、实际发送者与时间；这是群聊里的公开记录，"
    "不等于他在私聊里说过的话。"
)

#: 注入块的围栏标签。正文是**群成员可控**的原话（B-32）：围栏 + user 角色是它
#: 唯一的信任边界，与长期记忆（``long_term_memory``）用同一套 ``<untrusted:*>``。
GROUP_PUBLIC_UNTRUSTED_LABEL = "group_public_record"

#: 注入块的头部消息（标记 + 说明）。由调用方放进**永不裁剪**的固定层：来源声明不该
#: 因为在预算里排在最前面就被先裁掉——被裁的永远是最旧的一条公开记录。
GROUP_PUBLIC_HEADER_BLOCK = f"{GROUP_PUBLIC_BLOCK}\n{GROUP_PUBLIC_HEADER}"


def render_group_public_block(
    records: Iterable[dict[str, Any]] | None,
    *,
    titles: dict[int, str] | None = None,
    max_records: int = GROUP_PUBLIC_MAX_RECORDS,
    content_max_chars: int = GROUP_PUBLIC_CONTENT_MAX_CHARS,
) -> str:
    """把公开记录渲染成注入块；没有记录时返回空串。

    **每条都带来源标注**（``[群聊公开记录 · 群名/群id]``）与**实际发送者**、时间；
    只给中性说明，不加任何强制指令块——是否用、怎么用由模型自己判断（用户口径）。
    正文套 ``<untrusted:group_public_record>`` 围栏（B-32）：群成员的原话只是**数据**。
    """

    body: list[str] = []
    label_map = titles or {}
    for item in [item for item in (records or []) if isinstance(item, dict)][
        : max(1, int(max_records))
    ]:
        body.extend(
            _record_lines(
                item, label_map=label_map, content_max_chars=content_max_chars
            )
        )
    if not body:
        return ""
    return "\n".join([GROUP_PUBLIC_HEADER_BLOCK, *body])


def render_group_public_messages(
    records: Iterable[dict[str, Any]] | None,
    *,
    titles: dict[int, str] | None = None,
    max_records: int = GROUP_PUBLIC_MAX_RECORDS,
    content_max_chars: int = GROUP_PUBLIC_CONTENT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """把公开记录渲染成**一条记录一条消息**（不含头部）。

    理由与检索留档一致：统一闸门按「条」裁剪，拆开才能在超预算时从最旧的一条开始丢。
    头部说明用 :data:`GROUP_PUBLIC_HEADER_BLOCK`，由调用方放进永不裁剪的固定层。

    **每条都是 ``role="user"`` 且套了 ``<untrusted:group_public_record>`` 围栏**（B-32）：
    内容是群成员的原话，进 system 等于把群里的文字提到 system 优先级。
    """

    items = [item for item in (records or []) if isinstance(item, dict)][
        : max(1, int(max_records))
    ]
    if not items:
        return []
    label_map = titles or {}
    messages: list[dict[str, Any]] = []
    for item in items:
        for line in _record_lines(
            item, label_map=label_map, content_max_chars=content_max_chars
        ):
            messages.append({"role": "user", "content": line})
    return messages


async def load_user_public_group_context(
    *,
    query: str,
    group_ids: Iterable[Any] | None,
    titles: dict[int, str] | None = None,
    memory: Any | None = None,
    max_groups: int = GROUP_PUBLIC_MAX_GROUPS,
    per_group: int = GROUP_PUBLIC_PER_GROUP,
    max_records: int = GROUP_PUBLIC_MAX_RECORDS,
) -> list[dict[str, Any]]:
    """按当前话题，从**该用户可访问的群**里取公开记录（只读，取不到就空）。

    * ``group_ids`` 是**已确认该用户可访问**的群；空列表直接返回空（宁可不给，
      也不猜）。
    * ``query`` 为空时不取：没有话题就没有「相关内容」可言，不硬塞群聊内容。
    * 取数复用 ``recall_archive``：保留期过滤、F-016（已删除消息不再召回）都在它里面。
    * 单个群检索失败只记日志、跳过该群；整体失败按「没有公开记录」处理。
    """

    ids = normalize_group_ids(group_ids)[: max(1, int(max_groups))]
    if not ids:
        return []
    topic = _bounded_text(query, 200)
    if not topic:
        return []
    service = memory if memory is not None else memory_holder.get_optional()
    if service is None:
        return []

    limit = max(1, min(8, int(per_group)))
    records: list[dict[str, Any]] = []
    for group_id in ids:
        try:
            rows = await service.recall_archive(group_id, query=topic, limit=limit)
        except Exception as exc:
            log.warning(
                "group public context: 群公开记录检索失败（跳过该群） | group=%s | error=%s",
                group_id,
                exc,
            )
            continue
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            # 只要成员公开说过的内容：机器人自己的回复不是「群内公开讨论」。
            if str(row.get("role") or "user").strip().lower() != "user":
                continue
            content = _bounded_text(row.get("content"), GROUP_PUBLIC_CONTENT_MAX_CHARS)
            if not content:
                continue
            records.append(
                {
                    "group_id": int(row.get("group_id") or group_id),
                    "group_title": (titles or {}).get(group_id, ""),
                    "sender_name": _bounded_text(row.get("sender_name"), 40),
                    "sent_at": _bounded_text(row.get("sent_at"), 32),
                    "content": content,
                }
            )
            if len(records) >= max(1, int(max_records)):
                break
        if len(records) >= max(1, int(max_records)):
            break
    return records


# ---------------------------------------------------------------------------
# C 项红线的兜底断言（负向用例直接用它）
# ---------------------------------------------------------------------------


class PrivateContentLeakError(AssertionError):
    """群聊上下文里出现了私聊正文。"""


def assert_no_private_content(
    prompt_text: str,
    *,
    private_markers: Iterable[str] | None = None,
) -> None:
    """群聊 prompt 里**绝不许**出现私聊正文的兜底断言。

    把调用方给的哨兵/标记逐个在 ``prompt_text`` 里找，命中就抛
    :class:`PrivateContentLeakError`。

    这是**代码侧**的兜底（生产路径默认不调用）；真正的保证是「群聊装配路径根本不
    读私聊表」——负向用例见 ``tests/test_context_privacy.py``。
    """

    body = str(prompt_text or "")
    for marker in private_markers or []:
        text = str(marker or "")
        if text and text in body:
            raise PrivateContentLeakError(
                f"群聊上下文里出现了私聊内容哨兵：{text!r}"
            )
