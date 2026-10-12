"""全局运行时配置的**变更历史**：脱敏快照、结构化差异与状态元数据。

这个模块刻意**不 import** ``bot.services.runtime_config``：它只吃普通 dict，
所以"脱敏"这件事不依赖 pydantic 模型是否记得登记每一个密钥字段。方向是反的
——``runtime_config`` import 这里，不是有���么。

为什么要这一层（``storage_payload()`` 不是已经清空密钥了吗）
------------------------------------------------------------
``RuntimeConfig.storage_payload()`` = ``with_secrets({})`` + ``model_dump``，对
**当前** schema 里的 9 个静态路径 + 每个 ``providers.<name>.api_key`` 确实一个不漏
（``tests/test_runtime_config_history.py::StoragePayloadRedactionTests`` 逐个路径
验证过）。但它完全依赖 ``with_secrets`` **手工列举**每一个密钥字段：以后有人给
模型加一个带密钥的字段、忘了加进 ``_STATIC_SECRET_PATHS``，``storage_payload()``
就会**静默**把明文写进 ``runtime_config.payload``，历史表也会跟着抄一份。

所以这里再叠一层**递归脱敏**，两道都过才算脱敏完成：

1. 显式 secret 路径（``RuntimeConfig.secret_paths()``）；
2. 键名启发式（见 :data:`SECRET_KEY_HINTS`：``api_key`` / ``secret`` /
   ``token`` / ``password`` / ``ciphertext`` / ``authorization`` /
   ``access_key``）——纵深防御，不指望下一次加字段的人记得登记。

启发式有三条刻意的收窄（否则脱敏本身就会把配置改坏，这条是踩出来的）：

* **整组件匹配**，不是子串匹配。``bot.max_context_tokens``、
  ``resources.llm_tokenizer_concurrency`` 的键名里都有 ``token``，但它们是**数量**。
  按子串匹配会把它们换成字符串标记，回滚时严格 schema 直接报
  ``Input should be a valid integer``。
* **不作用于数字/布尔/null**。``resources.decision_history_token_budget`` 是 int，
  不是凭据；把它脱敏成 ``<redacted:current>`` 同样会撑爆回滚时的校验。
* **但作用于 list / dict**。``models.main.request_params.extra_headers`` 是
  ``["Bearer …"]``：键名不像密钥、值又不是字符串，于是它**整条绕过**了脱敏，
  哨兵原样进了历史。列表和字典不是绕过的门。

第 2、3 条是互补的：凭据几乎不可能是一个裸数字，所以"数字不脱敏"不损失安全
性；而"列表/字典不脱敏"曾经直接就是一个真实的凭据泄露。

自由 JSON 与 URL（第 4 条防线）
------------------------------
``request_params`` 是运营随便塞的 JSON，键名可以叫 ``extra_headers``；供应商的
``api_base`` 可以是 ``https://example.com/v1?token=…``。键名启发式对这两种形状
都无能为力，所以另加一层**值形状**判定：

* 认证方案前缀（``Bearer …``）与请求头整行（``Authorization: …``、``X-*-Token: …``）
  → 整串替换成 ``<redacted:current>``；
* 任何字符串里 URL 的 **userinfo** 与**敏感 query 参数**就地替换
  （``https://<redacted:url-userinfo>@host/v1?token=<redacted:url-query>``），
  host / path / 参数名保留，管理员仍看得出地址指向哪。

回滚策略（这一层的代价，也是必须的代价）
------------------------------------
``<redacted:current>`` 与就地替换过的 URL **回滚时取当前值**
（:func:`restore_opaque_values`），取不到就抛
:class:`RuntimeConfigHistoryNotRestorableError` 明确拒绝。**绝不**把它们当成空值
——"回滚后凭据消失"比"回滚失败"危险得多，而失败是一个管理员看得见的结果。
代价要说清楚：一条曾经把凭据写进 ``extra_headers`` 的历史版本，**回滚不了那两个
字段**，只能保留当前值（界面会把 ``status: "current"`` 显示出来）。

被替换掉的值不是掩码（``sk-…abcd``），而是**状态标记**
``<redacted:configured>`` / ``<redacted:empty>``：掩码会把长度和前后缀漏给任何
能读历史表的人。状态标记保留了"当时配没配"这个唯一有用的审计事实。

列表规则（本模块的路径命名契约）
--------------------------------
差异必须落到**具体字段**。整份文档比对只会得到"整体变了"，管理员看不出到底改了
什么；列表更不能塌成一行。:func:`_walk` 产出的路径形状：

* 对象里的标量：``models.main.temperature``
* 列表元素：``<prefix>[<key>].<field>``，``<key>`` 按顺序取：

  1. 该列表**每个元素都是 dict 且都带同一个非空标量身份字段**（依次尝试
     ``name`` / ``id`` / ``key``，且取值互不重复）→ 用身份值，例
     ``models.providers[gemini].api_base``。这样在列表头部插入或删除一个供应商
     时，其余供应商的路径**不会整体移位**，差异才读得懂。
  2. 否则退化成下标：``economy.weekly_reward_points[3]``、
     ``models.providers[1].api_base``。

两种写法都**逐元素**产出路径；"整个列表算一条"是明确禁止的。标量列表
（``[25, 12, 12, 4]``）同样逐下标产出。

确定性
------
输出按 ``path`` 排序（同一次调用重复执行结果完全一致，测试直接比对两次结果）。
超过 :data:`MAX_DIFF_STRING_CHARS` 的字符串会被截断并在该条上标 ``truncated``：
提示词动辄几 KB，不截断的话一个差异响应就是几百 KB，而且长文本本来就不可读。
条目数超过 :data:`MAX_DIFF_ENTRIES` 时只保留前 N 条（调用方据此回报"已截断"）。

这些上限约束的是**响应**；真正会一直占盘的是**存储**那一侧，见下一节。

存储增长与保留（本批的显式取舍）
--------------------------------
**没有 TTL、没有条数上限、没有清理任务。** 每次成功保存或回滚都**永久追加**一整份
脱敏快照（追加写、只增不改、从不重写旧行），全新部署的默认配置一份约 82 KB（其中约
65 KB 是提示词），所以体积大致是"每次保存 +82 KB"。**这张表不会自己变小**，运维
必须知道这一点——完整说明见 ``docs/configuration-reference.md`` 的"历史保留/清理"。

本批能给的唯一刹车是**单份快照不超过 :data:`MAX_SNAPSHOT_BYTES`（1 MiB）**：
:func:`ensure_snapshot_within_limit` 超限就**拒绝**这次保存（``RuntimeConfigManager.
_prepare_revision`` 在**打开 session 之前**调用它，所以数据库一个字节都不写；保存
预览走的是**同一个** ``_prepare_revision``，于是"预览接受 / 保存拒绝"不可能发生）。
不用"截断"是因为截断后的快照会让"回滚到这一版"悄悄回滚到半份配置；不用"跳过历史"
是因为那会直接破坏"主表与历史同生共死"的不变量。1 MiB 相对默认配置留了 10 倍以上
余量，**正常部署碰不到它**——它是病态输入的兜底，不是配额。

量的口径必须是**列里真正存下来的那个字节串**：``payload_redacted`` 是 SQLAlchemy
的 ``JSON`` 列，用 JSON 序列化器默认参数写文本（``ensure_ascii=True``），所以
:func:`snapshot_size_bytes` 也按 ``ensure_ascii=True`` 量。曾经按
``ensure_ascii=False`` 量，于是"1 MiB"实际能存下约 2 MiB——上限形同虚设。

已知的诚实边界
--------------
**两个都还"已配置"的密钥，在只比 payload 快照时看不出值被换过**——快照里只有
``<redacted:configured>``。所以 :func:`build_diff` 接受 ``changed_secret_paths``：
调用方（``RuntimeConfigManager.save``）从 keep/clear/replace 协议里知道哪些路径
被显式替换过，把它传进来，差异里就会多一条 ``configured → unchanged`` 的状态记录。
没有这个参数时，**两侧状态相同**的密钥/不透明路径一律**不产生任何条目**（"没变"
就不该出现在变更列表里），这一点在 API 响应里由 ``secret_paths_changed`` 补齐。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

#: 脱敏后替换掉密钥值的**状态标记**。刻意不用掩码：``sk-a***z`` 仍然泄露长度和
#: 前后缀，而历史表会被所有有库读权限的人看到。
REDACTED_MARKER_TEMPLATE = "<redacted:{status}>"
SECRET_STATUS_CONFIGURED = "configured"
SECRET_STATUS_EMPTY = "empty"
SECRET_STATUS_CLEARED = "cleared"
SECRET_STATUS_UNCHANGED = "unchanged"
#: **这一版的值从来没有落进历史**（它属于 ``request_params`` 这种自由 JSON，或属于
#: 一条把凭据写进了 userinfo / query 的 URL）。它既不是"当时有值"，也不是"当时
#: 没值"——历史根本不知道。回滚时只能取**当前**配置里同一个路径的值（见
#: :func:`restore_opaque_values`）；取不到就明确拒绝（见
#: :class:`RuntimeConfigHistoryNotRestorableError`），**绝不**悄悄清空一个当前
#: 还在用的凭据。
SECRET_STATUS_CURRENT = "current"

#: URL 里被**就地**抹掉的两段凭据。它们是"这一版的字符串被裁掉了这一段"，而不是
#: 整个值被替换，所以 :func:`marker_status` 不认它们（不以 ``<redacted:`` 开头）；
#: 只有 :func:`needs_current_value` 认——回滚时整串取当前值。
URL_USERINFO_MARKER = "<redacted:url-userinfo>"
URL_QUERY_MARKER = "<redacted:url-query>"

_MARKER_PREFIX = "<redacted:"

#: 纵深防御的**键名启发式**：键名按 ``_`` 拆成组件后，命中下面任何一组就算密钥，
#: **即使**它没被登记进 ``secret_paths``。
#:
#: 必须是"整组件"匹配而不是子串匹配：``bot.max_context_tokens`` /
#: ``resources.llm_tokenizer_concurrency`` 这类字段名里有 ``token``，但它们是
#: **数量**，不是凭据。按子串匹配会把它们当成密钥脱敏成一个字符串标记，回滚时
#: 直接把严格 schema 撑爆（``Input should be a valid integer``）——这是一次
#: "为了脱敏把配置改坏"的真实失败模式。
SECRET_KEY_HINTS: tuple[frozenset[str], ...] = (
    frozenset({"api", "key"}),  # api_key / api-key
    frozenset({"api", "keys"}),  # api_keys —— 自由 JSON 里的复数写法
    frozenset({"secret"}),  # *_secret / secrets
    frozenset({"secrets"}),
    frozenset({"token"}),  # *_token（注意不含 tokens / token_budget / token_limit）
    frozenset({"password"}),
    frozenset({"passwd"}),
    frozenset({"ciphertext"}),  # 密文同样不该出现在历史里
    frozenset({"authorization"}),
    frozenset({"access", "key"}),  # aws_access_key_id / access_key
)

#: 差异里单个字符串值的截断上限（字符）。
MAX_DIFF_STRING_CHARS = 200
#: 差异条目数上限。超出的部分直接丢弃，调用方必须回报"已截断"。
MAX_DIFF_ENTRIES = 500
#: 摘要里最多列出的路径条数。
MAX_SUMMARY_PATHS = 12
#: 摘要总长度上限（字符）。
MAX_SUMMARY_CHARS = 1000

#: **单份历史快照**的存储上限（UTF-8 字节）。
#:
#: 为什么必须有：保留/清理（TTL）在这一批**没有**实现（见
#: ``docs/configuration-reference.md`` 的"历史保留/清理"）。那么唯一还剩的、
#: 能在这一批里说清楚的刹车就是"一行能有多大"——否则"不能无说明无限复制大型
#: prompt"就只约束了差异输出，没约束真正会一直占盘的那份脱敏快照。
#:
#: 为什么是 1 MiB：全新部署的默认配置脱敏后约 82 KB（其中约 65 KB 是提示词），
#: 1 MiB 留了 10 倍以上余量，**任何正常部署都不会碰到它**；而一条 1 MiB 的配置
#: 本身就已经是病态输入（提示词能到几 MB）。这个值是"兜底"，不是"预算"。
MAX_SNAPSHOT_BYTES = 1024 * 1024

#: 供应商列表在**密钥命名空间**里的前缀。``RuntimeConfig.secret_paths()`` 用的是
#: ``providers.<name>.api_key``（不带 ``models.``），这是本仓既有的约定，不是这里
#: 另立的名目。
_PROVIDERS_SECRET_PREFIX = "providers"
#: 供应商列表在 payload 里的位置。
_PROVIDERS_PAYLOAD_NS = "models.providers"
#: 列表元素的候选身份字段（按优先级）。
_IDENTITY_FIELDS: tuple[str, ...] = ("name", "id", "key")

# ---------------------------------------------------------------------------
# 值形状启发式：键名对不上的自由 JSON（``request_params``）与 URL 里的凭据
# ---------------------------------------------------------------------------
#: 一条 URL 里的敏感 **query 参数名**（小写、``-`` 归一成 ``_`` 后比对）。
#: 命中的参数**值**整段替换成 :data:`URL_QUERY_MARKER`，参数名保留——运维要看到
#: "这一版的 URL 确实带了 token 参数"，但看不到 token。
SECRET_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "session_token",
        "key",
        "apikey",
        "api_key",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "auth",
        "authorization",
        "signature",
        "sig",
        "credential",
        "credentials",
        "access_key",
    }
)

#: 字符串**开头**是这些 HTTP 认证方案时，整串就是凭据（``Bearer sk-...``）。
#: 只认前缀：提示词正文里几乎不会出现"以 Bearer 开头的一整段话"，而
#: "Bearer 出现在句子中间"太常见（"Bearer of bad news"），不能那样判。
_AUTH_SCHEME_RE = re.compile(
    r"(?:Bearer|Basic|Digest|HMAC|Negotiate|HOBA|Token|ApiKey|SSO|OAuth)\s+\S+",
    re.IGNORECASE,
)

#: ``Name: value`` 形的 HTTP 头行。值允许含空格（``Authorization: Bearer x``
#: 的值是三段），但**绝不跨行**：提示词正文里大量存在"某一行正好长得像请求头"的
#: 情况（``Authorization: 见上文``），跨行匹配会把整段提示词误判成凭据。
_HEADER_LINE_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9-]{0,63})[ \t]*:[ \t]*\S[^\n]*"
)

#: 头名本身不含密钥组件、但**按语义就是凭据**的头（``Cookie`` 的组件是
#: {cookie}，任何子串/组件启发式都命中不了，只能显式登记）。
CREDENTIAL_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "www-authenticate",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "apikey",
        "x-auth-token",
        "x-access-token",
        "x-goog-api-key",
        "x-secret-key",
        "x-amz-security-token",
        "x-hub-signature",
        "x-signature",
    }
)

#: 一段文本里出现 ``scheme://…`` 就按 URL 处理（就地抹 userinfo 与敏感 query）。
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{1,15}://[^\s<>\"'`\\]+")


class RuntimeConfigHistoryError(RuntimeError):
    """历史读取/回滚的失败（对外一律映射成稳定的错误码，不暴露内部细节）。"""


class RuntimeConfigHistoryNotFoundError(RuntimeConfigHistoryError):
    """目标 revision 没有历史行，或那一行的 payload 已损坏。"""


class RuntimeConfigHistoryNotRestorableError(ValueError):
    """这一版历史里有槽位**无法安全回滚**，回滚被拒绝。

    什么会触发：:data:`SECRET_STATUS_CURRENT` / :data:`URL_QUERY_MARKER` 这类
    "这一版的值从未落库"的占位，**当前**配置里同一个路径又没有可借的值。回滚到
    那一版只有两条诚实的走法——取当前值（:func:`restore_opaque_values`），或者
    明确告诉管理员"这一版回滚不了"。**没有第三条**：悄悄清空一个当前还在用的
    凭据（``extra_headers`` 变空列表、URL 里的 token 变没），比回滚失败危险得多。

    继承 ``ValueError``：与
    :class:`RuntimeConfigHistorySnapshotTooLargeError` 一样，回滚/预览路由的
    ``except ValueError`` 能接住它；对外由路由映射成**稳定**错误码
    ``history_revision_not_restorable``（预览与真正回滚同码）。

    消息里只有**路径名**，绝不回显任何值。
    """


class RuntimeConfigHistorySnapshotTooLargeError(ValueError):
    """这一版配置的脱敏快照超过 :data:`MAX_SNAPSHOT_BYTES`，**保存被拒绝**。

    刻意继承 ``ValueError``：所有既有路由（``put_settings`` /
    ``post_settings_history_restore``）都已经有 ``except ValueError → 400
    invalid_settings``，于是这个失败在每一处调用点都自动拿到**稳定**错误码，
    不用去改任何一个 handler。它同时是 ``RuntimeConfigHistoryError`` 的一部分，
    但**不是** ``RuntimeConfigHistoryNotFoundError``——那一条对应 404"回滚不到
    这一版"，两者的处置完全不同（这一条是"你改的东西太大，别存"）。

    为什么失败而不是"跳过历史照存主表"：后者等于把已经修好的"主表已更新、历史
    缺失"不变量又放回去——配置改了，却查不到是谁在什么时候改的。

    消息里**只带字节数与上限**，不回显配置正文：这条异常最终会进 HTTP 响应体。
    """


def key_components(name: object) -> frozenset[str]:
    return frozenset(
        part
        for part in str(name or "").lower().replace("-", "_").split("_")
        if part
    )


def is_secret_key(name: object) -> bool:
    """键名看起来像密钥吗（整组件匹配，大小写与连字符无关）。

    见 :data:`SECRET_KEY_HINTS`：必须整组件命中，否则 ``max_context_tokens``
    这种"数量"字段会被误判成凭据。
    """

    parts = key_components(name)
    return any(hint <= parts for hint in SECRET_KEY_HINTS)


def is_secret_slot(name: object, value: Any, *, registered: bool = False) -> bool:
    """这个键位该被当成密钥吗。

    显式登记的（``registered=True``）**无条件**算密钥——不管值是字符串、列表
    还是字典，整个值都被替换。这是审计要求的第一条：**列表/字典不能成为绕过的
    门**。``models.main.request_params.extra_headers`` 是一个 list，它里面的
    ``"Bearer …"`` 曾经因为"键名不像密钥 + 值不是字符串"而原样落进历史。

    键名启发式对 ``str`` / ``list`` / ``dict`` 生效，但对 **数字、布尔、null
    不生效**：``resources.decision_history_token_budget`` 这种 int 字段名里带
    ``token``，但它是个数量。把它换成字符串标记会让回滚时的严格 schema 校验直接
    失败（"为了脱敏把配置改坏"）——而一个裸数字本来也不可能是凭据。
    """

    if registered:
        return True
    if not is_secret_key(name):
        return False
    return isinstance(value, (str, list, dict))


def snapshot_size_bytes(payload: Any) -> int:
    """一份历史快照**真正落库后**占多少字节。

    口径必须与写库的那一步逐字一致。``runtime_config_history.payload_redacted``
    是 SQLAlchemy 的 ``JSON`` 列，它用 JSON 序列化器的**默认参数**写文本，也就是
    ``json.dumps(payload)``：``ensure_ascii=True``。非 ASCII 字符会被转成
    ``\\uXXXX``，**每一个中文提示词字符要 6 字节而不是 UTF-8 的 3 字节**。

    这里曾经用 ``ensure_ascii=False`` 去量，于是"1 MiB 上限"实际能存下约 2 MiB：
    180 000 个中字符量成 540 031 字节（放行），真正序列化后是 1 080 031 字节
    （超限）。上限只是写着好看。

    所以量的是 ``ensure_ascii=True`` 的编码长度——与列里实际存下来的那个字符串
    **逐字节相同**（``tests/test_runtime_config_history.py`` 里有 ``SELECT``
    回读比对守着这条）。这不是把上限调大，是把口径修对。
    """

    if not isinstance(payload, dict):
        return 0
    return len(encode_snapshot_json(payload).encode("utf-8"))


def encode_snapshot_json(payload: Any) -> str:
    """快照落库时会变成的那一段 JSON 文本（口径只有这一处）。"""

    return json.dumps(payload, ensure_ascii=True, allow_nan=False)


def snapshot_limit() -> int:
    """:data:`MAX_SNAPSHOT_BYTES` 的**调用时**读数。

    刻意做成函数而不是让调用方 ``from … import MAX_SNAPSHOT_BYTES``：上限判断
    读的是本模块的全局，而 API 响应里"上限是多少"也必须报**同一个**数。用
    ``import`` 进来的副本会在有人（测试、或将来某个部署开关）改动本模块全局时
    悄悄过期，于是响应告诉管理员 1 MiB、实际按 4 KiB 拒。
    """

    return MAX_SNAPSHOT_BYTES


def ensure_snapshot_within_limit(payload: Any) -> int:
    """快照超过 :data:`MAX_SNAPSHOT_BYTES` 就**拒绝**，绝不截断或跳过。

    返回实际字节数。截断一份快照会让"回滚到这一版"悄悄回滚到**半份**配置——
    比直接拒绝危险得多；跳过历史则破坏"主表与历史同生共死"的不变量。两条都
    不能做，只能让这次保存失败，让管理员知道自己提交的东西太大了。
    """

    size = snapshot_size_bytes(payload)
    if size > MAX_SNAPSHOT_BYTES:
        raise RuntimeConfigHistorySnapshotTooLargeError(
            f"配置过大，无法纳入变更历史：脱敏后 {size} 字节，"
            f"上限 {MAX_SNAPSHOT_BYTES} 字节。"
            "请先精简提示词等大字段再保存（本次保存未生效，历史与当前配置均未改动）。"
        )
    return size


def secret_marker(status: str) -> str:
    return REDACTED_MARKER_TEMPLATE.format(status=status)


def marker_status(value: Any) -> str | None:
    """值是不是**整值**脱敏标记？是的话返回它的状态，否则 ``None``。"""

    if not isinstance(value, str) or not value.startswith(_MARKER_PREFIX):
        return None
    status = value[len(_MARKER_PREFIX) :].rstrip(">")
    return status or None


def is_current_restore_marker(value: Any) -> bool:
    """这个值是不是"从未落库、只能取当前值"的整值标记。"""

    return marker_status(value) == SECRET_STATUS_CURRENT


def contains_inline_marker(value: Any) -> bool:
    """字符串里是否**就地**抹掉了 URL 凭据（``…/v1?token=<redacted:url-query>``）。"""

    return isinstance(value, str) and (
        URL_USERINFO_MARKER in value or URL_QUERY_MARKER in value
    )


def needs_current_value(value: Any) -> bool:
    """这个值能不能从历史里直接回滚？

    不能的两种：整值被替换成 :data:`SECRET_STATUS_CURRENT`（自由 JSON 里的凭据），
    或者字符串里就地抹掉了 URL 凭据。两者回滚时都**整段取当前值**，绝不还原出
    一个半截的 URL 或一个空的头列表。
    """

    return is_current_restore_marker(value) or contains_inline_marker(value)


def secret_status(value: Any) -> str:
    """一个密钥槽位在某版配置里的状态：``configured`` / ``empty`` / ``current``。

    四种输入都认：脱敏标记、真实值、``None``、``None`` 之外的各种容器。这样同一
    份 diff 引擎既能处理脱敏快照（历史对比），也能处理未脱敏的内存对象（保存时
    摘要）。
    """

    status = marker_status(value)
    if status is not None:
        return status
    if isinstance(value, (list, dict, tuple)):
        return SECRET_STATUS_CONFIGURED if value else SECRET_STATUS_EMPTY
    return SECRET_STATUS_CONFIGURED if str(value or "").strip() else SECRET_STATUS_EMPTY


def is_redaction_marker(value: Any) -> bool:
    return marker_status(value) is not None


def strip_redaction_markers(value: Any) -> Any:
    """把 ``<redacted:configured|empty|cleared>`` 还原成空串。

    回滚历史时用：登记在册的密钥在历史里只是标记，真实值一律从**当前**密钥表
    合并回来。

    **``<redacted:current>`` 刻意不清空**：它表示"这一版的值从未落库"，清空等于
    静默抹掉一个当前还在用的凭据。它必须先由 :func:`restore_opaque_values` 换成
    当前值；没换就走到这里，说明调用顺序错了，宁可留下一个明显非法的标记让严格
    schema 报错，也不要悄悄给管理员一个空凭据。
    """

    if is_current_restore_marker(value):
        return value
    if is_redaction_marker(value):
        return ""
    if isinstance(value, dict):
        return {key: strip_redaction_markers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [strip_redaction_markers(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# 值形状与 URL 脱敏
# ---------------------------------------------------------------------------


def is_sensitive_query_param(name: object) -> bool:
    """URL 的 query 参数名敏感吗（大小写、连字符无关）。"""

    normalized = str(name or "").strip().lower().replace("-", "_")
    return normalized in SECRET_QUERY_PARAMS or is_secret_key(normalized)


def _scrub_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.netloc:
        return url
    netloc = parts.netloc
    changed = False
    if "@" in netloc:
        # ``https://user:SECRET@host`` → ``https://<redacted:url-userinfo>@host``
        netloc = f"{URL_USERINFO_MARKER}@{netloc.rsplit('@', 1)[1]}"
        changed = True
    query = parts.query
    if query:
        chunks: list[str] = []
        for chunk in query.split("&"):
            name, separator, _value = chunk.partition("=")
            if separator and is_sensitive_query_param(name):
                chunks.append(f"{name}={URL_QUERY_MARKER}")
            else:
                chunks.append(chunk)
        rebuilt = "&".join(chunks)
        changed = changed or rebuilt != query
        query = rebuilt
    if not changed:
        return url
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_url_credentials(text: str) -> str:
    """任何字符串里 URL 的 userinfo 与敏感 query 参数一律抹掉。

    审计要求：URL 的凭据要**按形状**处理，不能只看字段名——
    ``models.providers[].api_base = "https://example.com/v1?token=SECRET"`` 的
    字段名是 ``api_base``，任何键名启发式都命中不了，凭据却原样进了历史。

    抹完保留 host/path/参数名，管理员仍能看出"这一版的地址指向哪、带没带 token
    参数"，但看不到 token 本身。
    """

    if "://" not in text:
        return text
    return _URL_RE.sub(lambda match: _scrub_url(match.group(0)), text)


def is_credential_string(value: Any) -> bool:
    """这个字符串本身**看起来就是凭据**（与键名无关）。

    两种形状，都是 HTTP 生态里真实存在的写法：

    * ``Bearer sk-…`` / ``Basic dXNlcjpwYXNz`` —— 认证方案前缀；
    * ``Authorization: Bearer …`` / ``X-Api-Key: k`` —— 请求头整行。

    第二种要求**头名**本身命中 :data:`SECRET_KEY_HINTS` 或
    :data:`CREDENTIAL_HEADER_NAMES`（``X-Custom-Token`` 命中组件启发式，
    ``Cookie`` 走显式名单），否则 ``"Note: 记得复核"`` 这种正文也会被当凭据。
    """

    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if _AUTH_SCHEME_RE.fullmatch(text):
        return True
    match = _HEADER_LINE_RE.fullmatch(text)
    if not match:
        return False
    name = match.group("name")
    return is_secret_key(name) or name.lower() in CREDENTIAL_HEADER_NAMES


def redact_scalar(value: Any) -> Any:
    """一个**非密钥槽位**里的标量在历史里长什么样。"""

    if not isinstance(value, str):
        return value
    if is_credential_string(value):
        return secret_marker(SECRET_STATUS_CURRENT)
    return redact_url_credentials(value)


def _is_secret_namespace(path: str, secret_paths: set[str]) -> bool:
    return path in secret_paths


def redact_payload(payload: Any, secret_paths: set[str]) -> Any:
    """递归脱敏，覆盖**整个值**——不只是字符串。

    三层，从外到内：

    1. **显式 secret 路径**：整个值换成状态标记，与值的类型无关。
    2. **键名启发式**（``str`` / ``list`` / ``dict``）：整个值换成
       ``<redacted:current>``。列表和字典**不是**绕过的门——历史里从来不允许出现
       "因为它是个 list，所以放行"。
    3. **值形状与 URL**（任何位置的标量字符串）：认证方案 / 请求头整行整串抹掉；
       任何 URL 的 userinfo 与敏感 query 参数就地抹掉。

    第 2、3 层产生的东西在回滚时**取当前值**（:func:`restore_opaque_values`），
    所以"历史里没有"永远不会变成"回滚后被清空"。

    不修改入参（调用方可能还拿着同一份 dict 去算差异）。
    """

    normalized = {str(path) for path in secret_paths}
    return _redact_node(payload, "", normalized)


def _redact_node(node: Any, namespace: str, secret_paths: set[str]) -> Any:
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            child_namespace = f"{namespace}.{key}" if namespace else str(key)
            if _is_secret_namespace(child_namespace, secret_paths):
                result[str(key)] = secret_marker(secret_status(value))
            elif is_secret_slot(key, value):
                # 键名可疑、值是字符串/列表/字典：整个值都没落库。
                result[str(key)] = secret_marker(SECRET_STATUS_CURRENT)
            else:
                result[str(key)] = _redact_node(value, child_namespace, secret_paths)
        return result
    if isinstance(node, list):
        if namespace == _PROVIDERS_PAYLOAD_NS:
            return [
                _redact_node(
                    item,
                    f"{_PROVIDERS_SECRET_PREFIX}.{_provider_name(item, index)}",
                    secret_paths,
                )
                for index, item in enumerate(node)
            ]
        return [_redact_node(item, namespace, secret_paths) for item in node]
    return redact_scalar(node)


def _provider_name(item: Any, index: int) -> str:
    if isinstance(item, dict):
        name = str(item.get("name") or "").strip().lower()
        if name:
            return name
    return str(index)


def _list_identity(items: list[Any]) -> list[str] | None:
    """列表元素的身份键；不可用时返回 ``None``（调用方退化成下标路径）。"""

    if not items or not all(isinstance(item, dict) for item in items):
        return None
    for field in _IDENTITY_FIELDS:
        values: list[str] = []
        for item in items:
            raw = item.get(field)
            if not isinstance(raw, (str, int)) or isinstance(raw, bool):
                break
            text = str(raw).strip()
            if not text:
                break
            values.append(text)
        else:
            # 身份必须唯一：重名的话路径会撞车，宁可退化成下标。
            if len(set(values)) == len(values):
                return values
    return None


@dataclass(frozen=True)
class _Leaf:
    value: Any
    secret: bool
    #: 密钥在**配置根**下的规范路径（``providers.gemini.api_key``）。与展示用的
    #: 扁平路径（``models.providers[gemini].api_key``）不是一回事：历史详情要按
    #: 规范路径回报"配没配"，不能把同一个密钥列两次。
    namespace: str = ""


def _walk(
    node: Any,
    path: str,
    namespace: str,
    secret_paths: set[str],
    out: dict[str, _Leaf],
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            child_namespace = f"{namespace}.{key}" if namespace else str(key)
            registered = _is_secret_namespace(child_namespace, secret_paths)
            if registered:
                out[child_path] = _Leaf(secret_marker(secret_status(value)), True, child_namespace)
            elif is_secret_slot(key, value):
                out[child_path] = _Leaf(
                    secret_marker(SECRET_STATUS_CURRENT), True, child_namespace
                )
            else:
                _walk(value, child_path, child_namespace, secret_paths, out)
        return
    if isinstance(node, list):
        # 空列表本身算一个叶子：否则"从空变成有内容"会一条差异都报不出来。
        if not node:
            out[path] = _Leaf(node, False, path)
            return
        identity = _list_identity(node)
        for index, item in enumerate(node):
            key = identity[index] if identity else str(index)
            child_path = f"{path}[{key}]"
            if namespace == _PROVIDERS_PAYLOAD_NS and isinstance(item, dict):
                child_namespace = (
                    f"{_PROVIDERS_SECRET_PREFIX}.{_provider_name(item, index)}"
                )
            else:
                child_namespace = f"{namespace}.{index}"
            _walk(item, child_path, child_namespace, secret_paths, out)
        return
    # 标量：**先脱敏再进差异**。差异响应是逐字回给浏览器的，如果不走这一步，
    # ``extra_headers[0] = "Bearer SENTINEL"`` 会在历史 diff 里原样出现——脱敏
    # 只保护了存储，没保护响应。
    out[path] = _Leaf(redact_scalar(node), False, path)


def _leaves(payload: Any, secret_paths: set[str]) -> dict[str, _Leaf]:
    normalized = {str(path) for path in secret_paths}
    out: dict[str, _Leaf] = {}
    if not isinstance(payload, dict):
        return out
    _walk(payload, "", "", normalized, out)
    return out


def _truncate(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str) and len(value) > MAX_DIFF_STRING_CHARS:
        return value[:MAX_DIFF_STRING_CHARS] + "…", True
    return value, False


def build_diff(
    old: Any,
    new: Any,
    secret_paths: set[str],
    *,
    changed_secret_paths: Any = None,
) -> list[dict[str, Any]]:
    """两份配置之间的**脱敏**结构化差异，按 ``path`` 排序。

    条目形状::

        {"path": str, "change": "added"|"removed"|"modified",
         "old": Any | None, "new": Any | None, "secret": bool}

    secret 路径的 ``old`` / ``new`` 是 ``{"status": ...}`` 字典，状态只有三种：
    ``configured``（当时有值）/ ``cleared``（回滚成空）/ ``unchanged``（值被显式
    替换过——快照看不出新值，靠 ``changed_secret_paths`` 才敢这么写）。**永远
    不含值。**

    ``changed_secret_paths`` 只有 ``save()`` 知道（keep/clear/replace 协议）；
    不传时，两侧同为 ``configured`` 的密钥路径不产生条目。

    最多返回 :data:`MAX_DIFF_ENTRIES` 条（secret 条目计入）；命中上限时调用方
    应把 ``len(result) >= MAX_DIFF_ENTRIES`` 当作"已截断"回报给前端——一次把
    上千个字段的改动原样塞进响应既没人看得完，也会把响应体撑大。
    """

    old_leaves = _leaves(old, secret_paths)
    new_leaves = _leaves(new, secret_paths)
    forced = {str(path) for path in (changed_secret_paths or ())}

    entries: list[dict[str, Any]] = []
    for path in sorted(set(old_leaves) | set(new_leaves)):
        if len(entries) >= MAX_DIFF_ENTRIES:
            break
        old_leaf = old_leaves.get(path)
        new_leaf = new_leaves.get(path)
        is_secret = bool((old_leaf and old_leaf.secret) or (new_leaf and new_leaf.secret))
        if is_secret:
            entry = _secret_diff_entry(path, old_leaf, new_leaf, forced)
            if entry is not None:
                entries.append(entry)
            continue
        if old_leaf is not None and new_leaf is not None:
            if old_leaf.value == new_leaf.value:
                continue
            change = "modified"
        elif old_leaf is not None:
            change = "removed"
        else:
            change = "added"
        old_value, old_cut = _truncate(old_leaf.value if old_leaf else None)
        new_value, new_cut = _truncate(new_leaf.value if new_leaf else None)
        entry = {
            "path": path,
            "change": change,
            "old": old_value,
            "new": new_value,
            "secret": False,
        }
        if old_cut or new_cut:
            entry["truncated"] = True
        entries.append(entry)
    return entries


def _diff_status(value: Any) -> str:
    """差异语境下的状态：``empty`` 读作 ``cleared``（"这一版把它清掉了"）。"""

    status = secret_status(value)
    return SECRET_STATUS_CLEARED if status == SECRET_STATUS_EMPTY else status


def _secret_diff_entry(
    path: str,
    old_leaf: _Leaf | None,
    new_leaf: _Leaf | None,
    forced: set[str],
) -> dict[str, Any] | None:
    old_status = _diff_status(old_leaf.value) if old_leaf is not None else None
    new_status = _diff_status(new_leaf.value) if new_leaf is not None else None
    replaced = path in forced

    if old_leaf is None and new_leaf is None:  # pragma: no cover - 不会发生
        return None
    if old_leaf is None:
        return {
            "path": path,
            "change": "added",
            "old": None,
            "new": {"status": new_status or SECRET_STATUS_CLEARED},
            "secret": True,
        }
    if new_leaf is None:
        return {
            "path": path,
            "change": "removed",
            "old": {"status": old_status or SECRET_STATUS_CLEARED},
            "new": None,
            "secret": True,
        }
    if old_status == new_status:
        if not replaced:
            # **两侧状态相同且没有显式替换：没有可报告的变更。**
            # 这条对所有状态成立，不只是 configured：
            # ``<redacted:empty> → <redacted:empty>`` 曾被报成
            # ``cleared → cleared``，于是"什么都没改"的保存预览给出 10 条伪变更
            # （包括 ``api_key cleared -> cleared``）。差异是给人看"改了什么"的，
            # 编一条就是骗人。
            return None
        # 显式替换：值确实变了，但快照里看不见，只能记成"换过"。
        return {
            "path": path,
            "change": "modified",
            "old": {"status": old_status or SECRET_STATUS_CLEARED},
            "new": {"status": SECRET_STATUS_UNCHANGED},
            "secret": True,
        }
    return {
        "path": path,
        "change": "modified",
        "old": {"status": old_status or SECRET_STATUS_CLEARED},
        "new": {"status": new_status or SECRET_STATUS_CLEARED},
        "secret": True,
    }


def build_summary(
    entries: list[dict[str, Any]],
    changed_secret_paths: Any = None,
) -> str:
    """一行人类可读摘要：**只有计数和路径，永远没有值**。

    值会进 ``payload_redacted``（那是给回滚用的），摘要进历史列表的每一行，
    必须是"改了哪几个字段"而不是"改成了什么"——运营的浏览器历史、截图、聊天
    转发都会把摘要带出去。
    """

    added = sum(1 for entry in entries if entry.get("change") == "added")
    modified = sum(1 for entry in entries if entry.get("change") == "modified")
    removed = sum(1 for entry in entries if entry.get("change") == "removed")
    paths = [str(entry.get("path") or "") for entry in entries]
    shown = paths[:MAX_SUMMARY_PATHS]
    if len(paths) > MAX_SUMMARY_PATHS:
        shown.append("…")
    head = (
        f"新增 {added} / 修改 {modified} / 移除 {removed}："
        f"{'、'.join(shown) if shown else '无变化'}"
    )
    secrets = sorted({str(path) for path in (changed_secret_paths or ())})
    if secrets:
        listed = secrets[:MAX_SUMMARY_PATHS]
        if len(secrets) > MAX_SUMMARY_PATHS:
            listed.append("…")
        head += f"；密钥 {len(secrets)} 项：{'、'.join(listed)}"
    return head[:MAX_SUMMARY_CHARS]


def discover_secret_paths(payload: Any, base_paths: Any) -> set[str]:
    """历史 payload 里出现的 secret 路径 ∪ 调用方已知的路径。

    历史详情要显示"这一版当时配没配密钥"。只拿**当前**配置的 secret 路径不够：
    那一版里有、现在已经被删掉的供应商会凭空消失；只拿 payload 里的也不够——
    现在新增的供应商同理。取并集，两边都不漏。
    """

    paths = {str(path) for path in base_paths}
    if not isinstance(payload, dict):
        return paths
    models = payload.get("models")
    providers = models.get("providers") if isinstance(models, dict) else None
    for item in providers or []:
        name = _provider_name(item, 0) if isinstance(item, dict) else ""
        if name:
            paths.add(f"{_PROVIDERS_SECRET_PREFIX}.{name}.api_key")
    return paths


def secret_status_metadata(
    payload: Any,
    secret_paths: set[str],
) -> list[dict[str, str]]:
    """给定一份（通常已脱敏的）payload，返回**每个 secret 路径**的状态。

    读历史详情时用它替代"给不给值"：管理员要知道"当时配没配"，不需要知道
    配的是什么。
    """

    leaves = _leaves(payload, secret_paths)
    statuses: dict[str, str] = {}
    for path, leaf in leaves.items():
        if leaf.secret:
            # 规范路径（``providers.gemini.api_key``），不是展示用的扁平路径——
            # 同一个密钥不能因为路径写法不同被列两次。
            statuses[leaf.namespace or path] = secret_status(leaf.value)
    for path in secret_paths:
        statuses.setdefault(path, SECRET_STATUS_EMPTY)
    return [
        {"path": path, "status": statuses[path]}
        for path in sorted(statuses)
    ]


def configured_secret_paths(secrets: dict[str, str]) -> set[str]:
    """密钥表里"当前真的有值"的路径名。"""

    return {path for path, value in secrets.items() if str(value or "").strip()}


def secret_status_change(
    previous: dict[str, str],
    current: dict[str, str],
) -> list[str]:
    """两次配置之间"有没有配这个密钥"发生变化的路径名（**没有值**）。

    只看"配没配"，不看值本身：两个都配着的密钥在这里查不出替换——那件事只有
    ``save()`` 知道，靠 keep/clear/replace 协议单独上报。
    """

    return sorted(
        configured_secret_paths(previous).symmetric_difference(
            configured_secret_paths(current)
        )
    )


# ---------------------------------------------------------------------------
# secret_changes：唯一的解析器（save 与 preview 共用）
# ---------------------------------------------------------------------------
#: keep/clear/replace 的动作名。**只有一个**动作表：以前 ``preview`` 自己抄了一
#: 份，只认 replace/clear，于是 ``{"action": "explode"}``、未知路径、空的 replace
#: 在预览里"通过"，在真保存时被拒——管理员点保存才看到错误。
SECRET_CHANGE_ACTIONS: frozenset[str] = frozenset({"keep", "clear", "replace"})


def resolve_secret_changes(
    secret_changes: Mapping[str, Any] | None,
    *,
    allowed_paths: set[str],
    current_secrets: Mapping[str, str],
    proposed_secrets: Mapping[str, str],
) -> tuple[dict[str, str], set[str]]:
    """把 ``secret_changes`` 解析成"这次保存后的密钥表"。

    返回 ``(secrets, replaced_paths)``。**``save`` 与 ``preview`` 必须都走这里**，
    于是同一份请求体在两条路径上得到逐字相同的结论：

    * 未知路径（``providers.nope.api_key``）→ ``ValueError``
    * 未知动作（``explode`` / 空）→ ``ValueError``
    * ``replace`` 但值是空串 → ``ValueError``（真保存本来就拒）
    * ``keep`` → 不动；``clear`` → 删；``replace`` → 覆盖

    ``allowed_paths`` = 候选配置里**存在的**密钥槽位。删掉一个供应商之后，
    ``providers.<它>.api_key`` 就不再是合法槽位——对它做 clear/replace 会被拒，
    而不是悄悄改一张与配置无关的密钥行。
    """

    secrets: dict[str, str] = dict(current_secrets)
    secrets.update(proposed_secrets)
    replaced: set[str] = set()
    for raw_path, change in (secret_changes or {}).items():
        path = str(raw_path)
        if path not in allowed_paths:
            raise ValueError(f"unknown secret setting: {path}")
        action = str((change or {}).get("action") or "").strip().lower()
        if action == "keep":
            continue
        if action == "clear":
            secrets.pop(path, None)
            replaced.add(path)
            continue
        if action == "replace":
            value = str((change or {}).get("value") or "").strip()
            if not value:
                raise ValueError(f"replacement secret is empty: {path}")
            secrets[path] = value
            replaced.add(path)
            continue
        raise ValueError(f"invalid secret action for {path}")
    return (
        {
            key: value
            for key, value in secrets.items()
            if key in allowed_paths and str(value or "").strip()
        },
        replaced,
    )


# ---------------------------------------------------------------------------
# 回滚：把"从未落库"的槽位换成当前值
# ---------------------------------------------------------------------------


def _resolve_node(
    history_node: Any,
    current_node: Any,
    path: str,
    namespace: str,
    missing: list[str],
) -> Any:
    if needs_current_value(history_node):
        # 只有真正"要取当前值"的槽位才可能失败。当前值不存在、或者当前那个
        # 位置本身也是占位 → 记下**这个路径**，让管理员知道去补哪一条。
        if current_node is None or needs_current_value(current_node):
            missing.append(path or "<root>")
            return history_node
        return current_node
    if isinstance(history_node, dict):
        # 刻意**不**因为"当前没有这棵子树"就整棵报错：继续往下走到真正需要
        # 当前值的那一个叶子，报出来的才是管理员能照着改的字段路径。
        source = current_node if isinstance(current_node, dict) else {}
        return {
            str(key): _resolve_node(
                value,
                source.get(key),
                f"{path}.{key}" if path else str(key),
                f"{namespace}.{key}" if namespace else str(key),
                missing,
            )
            for key, value in history_node.items()
        }
    if isinstance(history_node, list):
        identity = _list_identity(history_node)
        current_items = current_node if isinstance(current_node, list) else []
        current_by_name: dict[str, Any] | None = None
        if namespace == _PROVIDERS_PAYLOAD_NS:
            current_by_name = {
                _provider_name(item, index): item
                for index, item in enumerate(current_items)
                if isinstance(item, dict)
            }
        resolved: list[Any] = []
        for index, item in enumerate(history_node):
            key = identity[index] if identity else str(index)
            name = _provider_name(item, index) if isinstance(item, dict) else str(index)
            if current_by_name is not None:
                current_item = current_by_name.get(name)
            else:
                current_item = current_items[index] if index < len(current_items) else None
            if namespace == _PROVIDERS_PAYLOAD_NS and isinstance(item, dict):
                child_namespace = (
                    f"{_PROVIDERS_SECRET_PREFIX}.{_provider_name(item, index)}"
                )
            else:
                child_namespace = f"{namespace}.{index}"
            resolved.append(
                _resolve_node(
                    item,
                    current_item,
                    f"{path}[{key}]",
                    child_namespace,
                    missing,
                )
            )
        return resolved
    return history_node


def redact_opaque_leaves(payload: Any) -> Any:
    """只做第 2、3 层（可疑键名 + 值形状 + URL），**空值一律不动**。

    给 ``GET /api/v1/settings`` 用：那份响应是运营的**编辑表单**，里面的
    ``request_params`` / ``api_base`` 就是他刚填进去的凭据，逐字回显等于把它
    写进浏览器历史、devtools 与任何一次抓包。返回的文档因此带
    ``<redacted:current>`` / ``<redacted:url-query>`` 标记。

    与 :func:`redact_payload` 的区别有两条，都是为了让"回传这份文档"是无损的：

    * 不碰**登记在册**的密钥路径（``storage_payload()`` 已经把它们清空了，这里
      再标一次会让未配置的密钥显示成一个占位符，UI 会误以为"配过了"）。
    * **空值不脱敏**：``api_key: ""`` 就是真的没配，占位符只会制造假象。

    配套的读侧是 :func:`restore_opaque_values`——``save`` / ``preview`` 在校验
    之前把标记换回**当前**值，所以"编辑表单原样回传"与"显式改写"都能正确落库。
    """

    return _redact_opaque_node(payload, "", "")


def _redact_opaque_node(node: Any, path: str, namespace: str) -> Any:
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            child_namespace = f"{namespace}.{key}" if namespace else str(key)
            if value and is_secret_slot(key, value):
                result[str(key)] = secret_marker(SECRET_STATUS_CURRENT)
            else:
                result[str(key)] = _redact_opaque_node(value, child_path, child_namespace)
        return result
    if isinstance(node, list):
        if namespace == _PROVIDERS_PAYLOAD_NS:
            return [
                _redact_opaque_node(
                    item,
                    f"{path}[{_provider_name(item, index)}]",
                    f"{_PROVIDERS_SECRET_PREFIX}.{_provider_name(item, index)}",
                )
                for index, item in enumerate(node)
            ]
        return [
            _redact_opaque_node(item, f"{path}[{index}]", f"{namespace}.{index}")
            for index, item in enumerate(node)
        ]
    return redact_scalar(node) if node else node


def restore_opaque_values(history_payload: Any, current_payload: Any) -> Any:
    """把历史里"从未落库"的槽位换成**当前**配置里同一个路径的值。

    这是本模块对自由 JSON（``request_params`` / ``extra_headers``）与 URL 内嵌
    凭据的**回滚策略**，只有两条走法：

    * **(a) 保留当前值**：当前配置里同一个路径有值，就用当前值。这是默认路径。
    * **(b) 明确拒绝**：当前配置里取不到（路径不存在、或者当前那个位置本身也
      是占位）→ 抛 :class:`RuntimeConfigHistoryNotRestorableError`，消息里只列
      **路径名**，让管理员知道"这一版回滚不了，去把 X 填上再回滚"。

    **没有第三条**——把 `<redacted:current>` 当成空值清掉。那等于让一次回滚悄悄
    抹掉管理员今天还在用的凭据（头列表变空、URL 里的 token 消失），而"回滚了
    一版"看上去仍然成功。宁可拒绝。

    只处理**标量**上的占位：登记在册的 ``<redacted:configured|empty>`` 留给
    :func:`strip_redaction_markers` → 密钥表合并，那条路径一直是对的。
    """

    missing: list[str] = []
    resolved = _resolve_node(history_payload, current_payload, "", "", missing)
    if missing:
        # 措辞刻意**不**绑死"回滚"：同一个异常也走"把带占位的编辑表单原样回传"
        # 的保存路径，那里的动作是保存。文案只说事实 + 该做什么。
        raise RuntimeConfigHistoryNotRestorableError(
            "以下字段的值从未写入历史，且当前配置里没有可保留的值："
            f"{'、'.join(sorted(set(missing))[:12])}。"
            "请先在当前配置中把这些字段填好（或直接写入新值），再重试；"
            "系统不会把它们悄悄清空。"
        )
    return resolved
