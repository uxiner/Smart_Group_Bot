"""审核回放（dry-run）：把候选规则/提示词放到**已授权、已脱敏、可追溯**的样本上，
和"当前生效口径"逐条对照，**只出数字，不碰线上**。

这个模块存在的理由
-----------------
改审核口径（群规、审核提示词）最贵的不是改代码，是"改错了要等事故来告诉你"。
现在的流程只能上线试。本模块给管理员一条**离线对拍**：冻结当前口径当基线，把
候选当对照组，在一批脱敏样本上各跑一遍，输出"判了几条变化、误伤/漏放各几条、
贵了多少"。**全程不产生任何线上副作用**。

三条硬红线（每一条都有测试钉住）
--------------------------------
1. **不写生产配置**：这里没有任何一条路径把候选规则/提示词写进
   ``ModerationRule`` 或运行时配置。候选只活在 ``ModerationReplayRun`` 这张
   dry-run 表里，"确认回放"也只是一条只读结果。
2. **不吃活链路的预算**：不碰 ``ModerationCallBudget`` / ``ModerationAdmissionGate``
   （那两道闸属于群消息路径），也不给群消息路径加任何一次模型调用。
3. **不做任何处置**：不删消息、不禁言/封禁、不扣分、不发审核卡、不写
   ``violations``/``user_warnings``。判定结果只落 ``moderation_replay_results``。

口径与公式
----------
* ``changed`` = 基线判定（violation/benign/error 三值）与候选判定**不同**。
  "error → violation" 也算变化：正是"原来判不出来、改完能判了"这一类改动，
  不该被 diff 吞掉。
* FP/FN **只对带 gold label 的样本**计算，公式见 :func:`compute_summary` 的
  docstring；无标签样本计入 ``total`` 与 ``labeled_total`` 之间的差额，绝不参与。
* **``conclusive=False`` 一定带可见原因**（``*_error`` 里是一个稳定错误码），
  并且**被排除**在"确认干净/确认违规"以及准确率分母之外。判不出来就是判不出来，
  绝不折成"通过"。
* **金额恒为 null**：仓库里没有模型价格表（已确认），所以只报调用次数与 token
  用量，绝不编一个价格出来。
* token 用量：``LLMService.moderation`` 只返回 ``str``，provider 的 usage 到不了
  这一层，所以用仓库自己的 ``estimate_text_tokens`` 估算（与 cost_report 同一
  口径的上界）。**逻辑调用次数**与**真实 provider 尝试次数**分开报：一次
  ``LLMService`` 调用里含重试与 fallback，它**不等于**一次真实 provider 调用，
  所以真实次数在没有埋点时必须报 ``null`` 而不是 ``1``。

金标（gold label）
-----------------
**金标只能由人工显式确认产生。** 导入一条 ``violations`` 行**不会**得到金标：
机器置信度阈值（``high_confidence_hit``）不是人工判断；``review_state=released``
（"人工放行"）多数时候确实是误伤，但也可能是刻意宽免，不能静默等同于"确认误判"。
机器信号全部落在 ``provenance`` 里，界面上标成 **provenance**，任何统计都不读它。
唯一保留的是 ``review_state=banned``：管理员在证据卡上按过"确认封禁 · 判定准确"，
那是显式确认（并且仍可被 :func:`set_sample_label` 修正或清掉）。

执行模型
--------
回放在 **HTTP 请求之外** 跑（:class:`ReplayExecutor`），并且**任何时刻都不持有
写事务跨越模型网络调用**：抢占（CAS ``queued -> running`` + 租约）、写结果、收尾
各自是一个**极短的独立事务**。于是

* 独立读者随时能看到 ``running`` 而不是卡在 ``queued``；
* 取消不会被 DB 写锁挡住（worker 在模型调用期间不持锁）；
* 第二个 worker 抢同一个 run 会干净地失败（租约未过期）；
* 进程崩了留下的 ``running`` 会被扫成 ``interrupted``，不会永远 running。

留存
----
样本是**别人的发言**（哪怕已脱敏），所以带 TTL：``SAMPLE_TTL_DAYS`` 天后不参与
新的回放，并由 :class:`ReplayMaintenance`（周期任务 + 启动那一次）真的删掉正文
**和它的结果行**；被删掉结果的那些 run 会**立刻重算统计**（并记
``results_purged``），所以报告里永远不会出现一个与现存结果行对不上的数字。
"不参与新回放"**不等于**"删掉了" —— 只有周期性真的删才叫留存。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import regex as safe_regex
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.models import (
    Group,
    ModerationReplayLabelAudit,
    ModerationReplayResult,
    ModerationReplayRun,
    ModerationReplaySample,
    ModerationRule,
    Violation,
)
from bot.services import llm_metrics, usage_scope
from bot.services.llm import LLMService
from bot.services.moderation import (
    # 与 ModerationService.evaluate 共用的解析/结构：**复用，不 fork**。
    _moderation_match_candidates,
    _own_text_objects,
    _parse_confidence,
    _parse_moderation_json,
    _RULES_BLOCK_MAX_CHARS,
    _RULES_BLOCK_POINTER,
    MATCH_SOURCE_OWN,
    MATCH_SOURCE_SEMANTIC,
    ModerationVerdict,
    RULES_BLOCK_LABEL,
    normalize_scan_scope,
    parse_scan_scope,
    split_moderation_text,
    SCAN_SCOPE_QUOTE,
    SCAN_SCOPE_VISION,
)
from bot.services.quality_report import MARGINAL_CONFIDENCE
from bot.services.request_priority import ExecutionPriority, execution_priority_scope
from bot.services.runtime_config import PromptSettingsConfig
from bot.utils.prompts import get_prompt
from bot.utils.security import (
    build_defended_system,
    clean_multiline_text,
    clean_text,
    wrap_untrusted,
)
from bot.utils.timezone import now_shanghai_naive
from bot.utils.tokens import estimate_text_tokens

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量（留存 / 上界 / 版本）
# --------------------------------------------------------------------------- #

#: 样本默认存活天数。样本 = 脱敏后的**用户发言副本**，没有理由永久留存：
#: 30 天足够把"上线后误伤的那几天"复盘完，也和仓库里 user_facts 的同类留存
#: 口径同量级。上界由 ``ReplayRetentionTests.test_default_ttl_is_documented_and_bounded``
#: 钉住（不允许调成一个"等于永久保留"的值）。
SAMPLE_TTL_DAYS = 30
#: 一次回放最多取多少条样本。每条样本要打两次模型调用（基线 + 候选），
#: 不设上界的话管理员一次点错就是 2N 次真实开销。
MAX_SAMPLES_PER_RUN = 200
#: 单轮回放的**默认**样本条数上限（后台执行用）。调用点可以传更小的值，但
#: 不允许"没传" —— ``None`` 会被收敛成这个常量，所以"忘了传界"与"传了界"
#: 不可能长得一样。
REPLAY_RUN_SAMPLE_LIMIT = 200
#: 单轮回放的**默认墙钟预算**（秒）。超出即停止并置 ``cancelled``。
REPLAY_RUN_DEADLINE_SECONDS = 1800.0
#: 单次模型调用的硬超时。回放是离线任务，但**不能**让一个挂住的 provider
#: 把整轮回放拖到天荒地老（异常会被记成 ``model_timeout`` 并照常继续）。
REPLAY_MODEL_TIMEOUT_SECONDS = 30.0
#: **执行租约**时长（秒）。抢占 ``queued -> running`` 时写入，之后每写一条结果
#: 就续期一次。租约到点仍没人续 → 下一个 worker 可以接管；进程重启后的扫描
#: 也会把这类行标成 ``interrupted``（见 :func:`sweep_interrupted_runs`）。
REPLAY_LEASE_SECONDS = 120.0
#: **在 HTTP 请求里同步执行**时的样本条数上限。回放每条样本要打两次模型调用，
#: 而 Mini App 的 initData 只有一个 10 分钟窗口：200 条样本按每次 3 秒算是 20 分钟，
#: 请求会先被反代掐掉，管理员只看到一个没有解释的 504。同步执行是"看一眼"的量级，
#: 想跑全量请分批导入/分批跑。
REPLAY_INLINE_SAMPLE_LIMIT = 25
#: 同步执行的墙钟预算：超时即停止并把状态置为 ``cancelled``
#: （``error="inline_deadline_exceeded"``），已算出的结果行保留，统计只覆盖这些行。
REPLAY_INLINE_DEADLINE_SECONDS = 120.0
#: 未复核命中被当作"建议标签"（**provenance，不是金标**）所需的最低置信度。
#: 用质量报表的 ``MARGINAL_CONFIDENCE``（0.9），避免同一个阈值在两个模块里各写一份。
CONFIRMED_HIT_CONFIDENCE = MARGINAL_CONFIDENCE

#: 后台执行的**全局**并发上限。回放是离线任务：即使模型侧还有名额，也不许把
#: 离线开销堆成任务山 —— 一堆跑不完的 dry-run 对管理员毫无价值。
REPLAY_MAX_CONCURRENCY = 2
#: 单群的并发上限。同一群的样本是同一批人的发言，并行跑没有意义，反而抢活链路
#: 的模型额度。
REPLAY_MAX_CONCURRENCY_PER_GROUP = 1
#: 排队（已接受但还没开始）上限。超过就稳定地 429，而不是无声地堆任务。
REPLAY_MAX_PENDING = 8

#: 回放维护（有界周期任务）的间隔（秒）。维护只做两件**短**事：回收过期租约的
#: ``running``、按 TTL 删过期样本正文。间隔不需要密：留存口径是天级的。
REPLAY_MAINTENANCE_INTERVAL_SECONDS = 900.0
#: 单次维护最多删多少条过期样本。每条都要连带删结果行并重算受影响 run 的统计，
#: 所以这个上界同时也是"一次维护最多持有写锁多久"的上界。
REPLAY_MAINTENANCE_SAMPLE_BATCH = 200

#: 回放统计在进程内的用量账本 stage（与群消息路径的 "moderation" 分开，
#: 免得离线开销把线上成本看板的曲线带偏）。注意这只是**回放自己**的账；
#: ``LLMService`` 内部的重试/fallback 仍会另记一次 provider 尝试到 "moderation"
#: stage —— 两者的差额正是"真实尝试次数 > 逻辑调用次数"的证据。
METRICS_STAGE = "moderation_replay"

SOURCE_KIND_RELEASE = "violation_release"
SOURCE_KIND_CONFIRMED = "violation_confirmed"
SOURCE_KIND_MANUAL = "manual"
SAMPLE_SOURCE_KINDS = (SOURCE_KIND_RELEASE, SOURCE_KIND_CONFIRMED, SOURCE_KIND_MANUAL)

LABEL_VIOLATION = "violation"
LABEL_BENIGN = "benign"
GOLD_LABELS = (LABEL_VIOLATION, LABEL_BENIGN)

#: 标注状态。只有 ``LABEL_STATE_LABELLED`` 的样本参与 FP/FN。
LABEL_STATE_UNLABELLED = "unlabelled"
LABEL_STATE_LABELLED = "labelled"

#: 标注来源。**没有任何机器信号会出现在这里**（见模块 docstring）。
LABEL_SOURCE_ADMIN_BAN = "admin_ban"
LABEL_SOURCE_MANUAL_CONFIRM = "manual_confirm"

#: 标注变更动作（落 ``moderation_replay_label_audits``）。
LABEL_ACTION_MANUAL_CREATE = "manual_create"
LABEL_ACTION_CONFIRM = "admin_confirm"
LABEL_ACTION_CORRECT = "admin_correct"
LABEL_ACTION_CLEAR = "admin_clear"

#: 判定三值。第三个值的存在是整张结果表的核心：失败必须显式可见。
VERDICT_VIOLATION = "violation"
VERDICT_BENIGN = "benign"
VERDICT_ERROR = "error"

#: ``*_error`` 的取值。每种失败**各归各的**，运维一眼能看出是超时还是上游抽风。
#: 最重要的一条口径：``conclusive=False`` **必须**在这里留一个非空码 ——
#: "判不出来"必须自己说得出来，否则它在统计里就等于"干净"。
ERROR_NONE = ""
ERROR_MODEL_ERROR = "model_error"
ERROR_MODEL_TIMEOUT = "model_timeout"
ERROR_UNPARSEABLE = "unparseable_output"
ERROR_EMPTY_RESPONSE = "empty_response"
ERROR_INVALID_VERDICT_FIELD = "invalid_verdict_field"
#: 本地确定性检查没跑完（正则非法/超时/规则为空）→ 判不出来，**不是干净**。
ERROR_INCONCLUSIVE_RULES = "inconclusive_rules"
#: 模型给了 ``violated`` 但没给可解析的 ``confidence`` → 同样判不出来。
ERROR_MISSING_CONFIDENCE = "missing_confidence"
#: run 级别的稳定错误码（进 ``run.error``，不含任何 provider 原文）。
RUN_ERROR_DEADLINE = "deadline_exceeded"
RUN_ERROR_CANCELLED = "cancelled_by_operator"
RUN_ERROR_INTERRUPTED = "worker_lost"
RUN_ERROR_FAILED = "run_failed"
RUN_ERROR_LEASE_LOST = "lease_lost"
RUN_ERROR_INLINE_DEADLINE = "inline_deadline_exceeded"

RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
#: 进程崩了 / 租约过期被接管扫回来的行。区别于 ``failed``：``failed`` 是这一轮
#: 自己报错的，``interrupted`` 是"没人收尾"，两种情况 UI 的说法必须不一样。
RUN_INTERRUPTED = "interrupted"
RUN_TERMINAL_STATUSES = (RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED, RUN_INTERRUPTED)

#: 候选规则允许的取值，与 web 层创建群规的 schema（``_RuleCreate``）逐字一致。
#: 这里重复列一次而不是 import ``_RuleCreate``：service 不该依赖 web 层；一致性
#: 由 ``tests/test_moderation_replay.py`` 的对齐测试钉住。
RULE_TYPES = ("keyword", "regex", "llm")
RULE_ACTIONS = ("warn", "delete", "ban")
#: 单份候选最多多少条规则。群规没有条数上限，但一次回放要按每条规则生成
#: 规则块；给个和样本上界同量级的硬顶，避免一份畸形提案把请求体顶爆。
MAX_CANDIDATE_RULES = 200
#: ``_RuleCreate.pattern`` 的 max_length，群规单条上限 1000 字符。
RULE_PATTERN_MAX_CHARS = 1000

#: ``llm_metrics`` 的 stage 名长度上限是 24，``record`` 自己会截，这里保持一致。
_MAX_METRICS_STAGE = 24

# 脱敏 ---------------------------------------------------------------------- #
#: Telegram user id：连续 6 位及以上数字。取 6 是为了不误伤"666666"这种聊天内容，
#: 同时盖住真实 id（现代 id 普遍 9–10 位）。宁可漏一条样本，也不要把成员 id 落库。
_TELEGRAM_ID_RE = re.compile(r"(?<!\d)\d{6,}(?!\d)")
#: 电话：可选国际号 + 允许 ``-``/空格/括号分隔的数字组（覆盖 138-0013-8000 这种）。
_PHONE_RE = re.compile(r"(?<![\d+])\+?\d[\d\-\s()]{6,}\d(?![\d])")
#: ``@username``：只吃合法的用户名字符集，避免把邮箱域名一起吃掉导致正文被挖空。
_MENTION_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,}")
#: 掩码标记。留一个可读的占位符，而不是抹成空串：空串会让"这里原本有个 id"
#: 变成"这里原本什么都没有"，复盘时无从判断。
REDACTION_MASK = "[id]"
_MENTION_MASK = "@[redacted]"

#: 正文/上下文的存储上限。取 1200 = 送审时 ``clean_text(text, max_len=1200)``
#: 的同一口径：存下来的东西不可能比线上会送审的更多，多存的部分永远用不上，
#: 留存风险却是实打实的。
SAMPLE_TEXT_MAX_CHARS = 1200
SAMPLE_CONTEXT_MAX_CHARS = 1200


class ReplayError(Exception):
    """回放侧的稳定错误码（API 层原样翻成 ``_APIError``）。

    消息是给管理员看的中文短句，**不含** SQL、路径、密钥或任何内部标识。
    """

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# 规范化 / 摘要
# --------------------------------------------------------------------------- #
def canonical_json(value: Any) -> str:
    """规范化 JSON：排序键 + 固定分隔符 + 不转义中文。

    摘要必须和"读的人"无关：同样的结构必须得到同样的字节，否则
    ``baseline_digest`` 就没法在 API/DB/日志之间对账。
    """

    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def digest_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RuleSnapshot:
    """一条群规的**不可变**快照。

    回放必须拿着快照判定，绝不能再去读库里的 ``ModerationRule``：基线那一侧
    的意义就是"建 run 那一刻线上是什么"。
    """

    id: int
    rule_type: str
    pattern: str
    action: str
    enabled: bool = True
    scan_scope: str = "message"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "rule_type": str(self.rule_type),
            "pattern": str(self.pattern),
            "action": str(self.action),
            "enabled": bool(self.enabled),
            "scan_scope": str(self.scan_scope),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuleSnapshot":
        try:
            rule_id = int(value.get("id") or 0)
        except (TypeError, ValueError):
            # 快照里的 id 只是给模型引用的稳定编号，读不出来就退回 0（离线候选
            # 允许没有库 id）。**绝不让一条坏 id 把整轮回放带崩。**
            rule_id = 0
        return cls(
            id=rule_id,
            rule_type=str(value.get("rule_type") or ""),
            pattern=str(value.get("pattern") or ""),
            action=str(value.get("action") or "warn"),
            enabled=bool(value.get("enabled", True)),
            scan_scope=normalize_scan_scope(value.get("scan_scope")),
        )


def rule_snapshot_from_row(row: ModerationRule) -> RuleSnapshot:
    return RuleSnapshot(
        id=int(row.id or 0),
        rule_type=str(row.rule_type or ""),
        pattern=str(row.pattern or ""),
        action=str(row.action or "warn"),
        enabled=bool(row.enabled),
        scan_scope=str(getattr(row, "scan_scope", None) or "message"),
    )


def compute_rules_fingerprint(rules: Sequence[RuleSnapshot]) -> str:
    """与 ``ModerationService.evaluate`` 的 ``rules_fingerprint`` **同一套算法**。

    口径必须一致：回放结果的 fingerprint 要能和活链路日志里的一一对应上，
    否则"这轮回放和线上那次是不是同一批规则"就没人答得上来了。
    """

    raw = "\x1e".join(
        "\x1f".join(
            str(value)
            for value in (rule.id, rule.rule_type, rule.pattern, rule.action)
        )
        for rule in rules
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
def redact_replay_text(
    text: str,
    *,
    max_len: int = SAMPLE_TEXT_MAX_CHARS,
    extra_ids: Sequence[int] = (),
) -> str:
    """把一段群消息脱敏成可以长期留存的副本。

    **抹掉**（一律替换成占位符，保留位置可读）：

    * Telegram user id —— 6 位及以上的裸数字，以及调用方已知的确切 id
      （导入 violations 时我们知道 ``violation.user_id``，先按精确值抹，
      再按形状兜底）；
    * 电话/手机号（含 ``138-0013-8000``、``+8613800138000`` 这类分隔写法）；
    * ``@username``（成员可能自曝的推广联系方式，正是群规最想抓的东西，
      **不能**把它留在回放库里再复现一遍）。

    **保留**：普通正文、标点、**以及换行**。

    换行必须留着：``[reply_to:*]`` / ``[image-vision]`` 这些机器注入的块标记是
    **按行**识别的（``split_moderation_text``），而群规的 ``scan_scope`` 正是靠它们
    把"引文/图片描述里的命中"和"用户自己写的命中"分开。把它们压成一行，用户正文里
    的"这是骗子别信"就会被并进图片描述段，反对信号豁免随之失效 —— 回放会判出
    一个线上根本不会发生的误伤。所以这里用 ``clean_multiline_text``（保留换行、
    同样 1200 字符上限）而不是会 ``\\s+ → " "`` 的 ``clean_text``。
    送模型那一侧仍然会被 ``wrap_untrusted`` 内部的 ``clean_text`` 压平，与线上
    逐字一致。

    截断上限取送审口径（``clean_text(text, max_len=1200)``）：存下来的正文不可能
    比线上会送审的更多，多存的部分永远用不上，留存风险却是实打实的。
    """

    body = str(text or "")
    for raw_id in extra_ids:
        try:
            identifier = int(raw_id)
        except (TypeError, ValueError):
            continue
        if identifier <= 0:
            continue
        body = body.replace(str(identifier), REDACTION_MASK)
    body = _TELEGRAM_ID_RE.sub(REDACTION_MASK, body)
    body = _PHONE_RE.sub(REDACTION_MASK, body)
    body = _MENTION_RE.sub(_MENTION_MASK, body)
    return clean_multiline_text(body, max_len=max_len)


# --------------------------------------------------------------------------- #
# 样本：导入 / 手写 / 列表 / 删除 / 清理
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ImportSignal:
    """从一条 ``violations`` 行里读出来的**机器侧信号**。

    ``gold_label`` 只有在"人明确确认过判定准确"时才是 ``violation``；其余一律
    ``None``。机器认为的答案放在 ``provenance['suggested_label']`` 里，**永远
    不参与 FP/FN**。
    """

    source_kind: str
    gold_label: str | None
    label_source: str
    provenance: dict[str, Any]


def _import_signal_for_violation(row: Violation) -> ImportSignal:
    """把一条 ``violations`` 行映射成 (来源类型, 金标?, 标注来源, provenance)。

    口径（可审计，写死在这里而不是散在判断里）：

    * **只有一种情况产生金标** —— ``review_state == 'banned'``：管理员在证据卡上
      按过"确认封禁 · 判定准确"。那是**人**对这条发言的明确判断，因此
      ``gold_label='violation'`` / ``label_source='admin_ban'``。
    * ``review_state == 'released'``（"人工放行"）→ **无金标**。它多数时候确实
      是误伤，但同样可能是刻意宽免（熟人、被误伤的老成员、当时不方便查证）。
      把"可能是误伤"静默升级成"确认误判"，等于给 FP/FN 喂一条**不可复核**的
      答案键。它只作为 ``suggested_label='benign'`` 出现在 provenance 里。
    * 未经人工复核（``none`` / ``revoked``）→ **一律无金标**，无论置信度多高。
      ``violations`` 每一行都是机器判出来的；把机器的答案当金标，FP/FN 就变成
      "模型和自己对答案"，而置信度阈值（0.9）只是把这种自欺欺人包装得更像回事。
      高置信度命中同样只作为 ``suggested_label='violation'`` 出现在 provenance。
    * ``revoked``（收回放行）与低置信命中：正确答案确实不可知，只留 provenance。
    """

    state = str(getattr(row, "review_state", "") or "").strip().lower()
    confidence = getattr(row, "confidence", None)
    try:
        numeric = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        numeric = None
    finite = numeric is not None and math.isfinite(numeric)
    provenance: dict[str, Any] = {
        "review_state": state or "none",
        "machine_confidence": numeric if finite else None,
        "action_taken": str(getattr(row, "action_taken", "") or ""),
        "source": "violations",
    }
    if state == "released":
        provenance["suggested_label"] = LABEL_BENIGN
        provenance["suggested_label_basis"] = "admin_release"
        provenance["suggested_label_confirmed"] = False
        return ImportSignal(SOURCE_KIND_RELEASE, None, "", provenance)
    if state == "banned":
        provenance["suggested_label"] = LABEL_VIOLATION
        provenance["suggested_label_basis"] = "admin_ban"
        provenance["suggested_label_confirmed"] = True
        return ImportSignal(
            SOURCE_KIND_CONFIRMED, LABEL_VIOLATION, LABEL_SOURCE_ADMIN_BAN, provenance
        )
    if finite and float(numeric) >= CONFIRMED_HIT_CONFIDENCE:
        provenance["suggested_label"] = LABEL_VIOLATION
        provenance["suggested_label_basis"] = "high_confidence_hit"
        provenance["suggested_label_confirmed"] = False
    return ImportSignal(SOURCE_KIND_CONFIRMED, None, "", provenance)


async def _record_label_change(
    session: AsyncSession,
    *,
    sample: ModerationReplaySample,
    previous: str | None,
    new: str | None,
    action: str,
    actor_id: int,
    now: datetime,
) -> None:
    """把一次标注变更落进 append-only 的审计表（**永远不记正文**）。"""

    session.add(
        ModerationReplayLabelAudit(
            sample_id=int(sample.id),
            group_id=int(sample.group_id),
            previous_label=previous,
            new_label=new,
            action=str(action),
            actor_id=int(actor_id or 0),
            changed_at=now,
        )
    )


async def set_sample_label(
    session: AsyncSession,
    group_id: int,
    sample_id: int,
    *,
    label: str | None,
    actor_id: int = 0,
    source: str = LABEL_SOURCE_MANUAL_CONFIRM,
    now: datetime | None = None,
) -> ModerationReplaySample:
    """由管理员**显式**设置 / 确认 / 修正 / 清除一条样本的金标。

    这是唯一能给样本打金标的服务入口（:func:`import_samples_from_violations`
    只会写 provenance）。``label=None`` 表示清回"无标注"。

    每一次变更都写一行 :class:`ModerationReplayLabelAudit`：FP/FN 报告要能说清
    它的答案键是谁、在什么时候定的，中途有没有被改过 —— 否则那份报告不可复核。
    """

    row = await session.get(ModerationReplaySample, int(sample_id))
    if row is None or int(row.group_id) != int(group_id):
        raise ReplayError("replay_sample_not_found", "样本不存在。", status=404)
    normalized = str(label or "").strip().lower() or None
    if normalized is not None and normalized not in GOLD_LABELS:
        raise ReplayError(
            "replay_gold_label_invalid", "标注只能是 violation 或 benign。"
        )
    previous = str(row.gold_label) if row.gold_label else None
    instant = now or now_shanghai_naive()
    row.gold_label = normalized
    row.label_state = LABEL_STATE_LABELLED if normalized else LABEL_STATE_UNLABELLED
    row.label_source = str(source or LABEL_SOURCE_MANUAL_CONFIRM) if normalized else ""
    row.label_reviewed_by = int(actor_id or 0)
    row.label_reviewed_at = instant if normalized else None
    await session.flush()
    await _record_label_change(
        session,
        sample=row,
        previous=previous,
        new=normalized,
        action=(
            LABEL_ACTION_CLEAR
            if normalized is None
            else (LABEL_ACTION_CONFIRM if previous is None else LABEL_ACTION_CORRECT)
        ),
        actor_id=int(actor_id or 0),
        now=instant,
    )
    await session.flush()
    return row


async def list_label_audits(
    session: AsyncSession, sample_id: int
) -> list[ModerationReplayLabelAudit]:
    return list(
        (
            await session.scalars(
                select(ModerationReplayLabelAudit)
                .where(ModerationReplayLabelAudit.sample_id == int(sample_id))
                .order_by(ModerationReplayLabelAudit.id)
            )
        ).all()
    )


async def _ensure_group_row(session: AsyncSession, group_id: int) -> None:
    """回放三张表都挂在 ``groups.id`` 外键上，没有群行就先补一个空行。

    与 ``settings_api._ensure_group_row`` 同一口径（空 settings，不覆盖已有值）。
    """

    row = await session.get(Group, int(group_id))
    if row is None:
        session.add(Group(id=int(group_id), title="", settings={}))
        await session.flush()


async def import_samples_from_violations(
    session: AsyncSession,
    group_id: int,
    *,
    limit: int = 200,
    kinds: Sequence[str] = (SOURCE_KIND_RELEASE, SOURCE_KIND_CONFIRMED),
    created_by: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """从**某一个群**的 ``violations`` 导入样本。

    严格单群：``group_id`` 必填且写进 where —— 这批数据最终是给群管理员看的，
    跨群混一份样本池等于把 A 群的发言摆到 B 群管理员面前。

    幂等：``(group_id, source_violation_id)`` 唯一索引 + ``on_conflict_do_nothing``，
    重复导入只补不重（``imported`` / ``skipped`` 分别计数）。

    存什么：**脱敏后的正文**（见 :func:`redact_replay_text`）、provenance 与
    **仅在人工明确确认时**才有的金标。不存：user_id、username、message_id、
    群标题、任何 Telegram 标识。

    ``provenance`` 额外记一条 ``redaction_applied``：脱敏**有没有改动过被判定
    的文本**。它是回放保真度的一部分（见 :func:`build_fidelity_block`）。

    ``skipped`` 只统计**本轮实际检查过**的行（未达 limit 就得下窗口的那些不计入），
    所以"扫了 800 条、导入 200 条"这种轮次之间不可直接相加。
    """

    gid = int(group_id)
    selected_kinds = tuple(
        kind for kind in (str(item or "").strip() for item in kinds or ())
        if kind in SAMPLE_SOURCE_KINDS
    )
    if not selected_kinds:
        raise ReplayError("replay_import_kinds_invalid", "导入类型无效。")
    window = max(1, min(int(limit or 0) or MAX_SAMPLES_PER_RUN, MAX_SAMPLES_PER_RUN))
    instant = now or now_shanghai_naive()

    stmt = (
        select(Violation)
        .where(Violation.group_id == gid)
        .order_by(Violation.id.desc())
        .limit(window * 4)
    )
    rows = list((await session.scalars(stmt)).all())

    imported = 0
    skipped = 0
    inserted_ids: list[int] = []
    for row in rows:
        signal = _import_signal_for_violation(row)
        kind = signal.source_kind
        if kind not in selected_kinds:
            skipped += 1
            continue
        raw_text = str(getattr(row, "message_text", "") or "")
        redacted = redact_replay_text(
            raw_text,
            extra_ids=(int(getattr(row, "user_id", 0) or 0),),
        )
        if not redacted:
            # 正文被抹空（例如只有一串手机号）的行没有回放价值，也不该占样本位。
            skipped += 1
            continue
        provenance = dict(signal.provenance)
        provenance["redaction_applied"] = bool(redacted != raw_text)
        labelled = bool(signal.gold_label)
        inserted = (
            await session.execute(
                sqlite_insert(ModerationReplaySample)
                .values(
                    group_id=gid,
                    source_kind=kind,
                    source_violation_id=int(row.id),
                    redacted_text=redacted,
                    redacted_context="",
                    gold_label=signal.gold_label,
                    label_state=(
                        LABEL_STATE_LABELLED if labelled else LABEL_STATE_UNLABELLED
                    ),
                    label_source=signal.label_source,
                    label_reviewed_by=int(getattr(row, "reviewed_by", 0) or 0),
                    label_reviewed_at=(
                        getattr(row, "reviewed_at", None) if labelled else None
                    ),
                    provenance=provenance,
                    created_by=int(created_by or 0),
                    created_at=instant,
                    expires_at=instant + timedelta(days=SAMPLE_TTL_DAYS),
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        ModerationReplaySample.group_id,
                        ModerationReplaySample.source_violation_id,
                    ]
                )
                .returning(ModerationReplaySample.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            skipped += 1
            continue
        imported += 1
        inserted_ids.append(int(inserted))
        if imported >= window:
            break
    await session.flush()
    return {
        "imported": imported,
        "skipped": skipped,
        "sample_ids": inserted_ids,
        "group_id": gid,
        "ttl_days": SAMPLE_TTL_DAYS,
    }


async def create_manual_sample(
    session: AsyncSession,
    group_id: int,
    *,
    text: str,
    context: str = "",
    gold_label: str | None = None,
    created_by: int = 0,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> ModerationReplaySample:
    """管理员手写一条样本。

    边界讨论（引文、图片描述、上下文来源、提示词注入）根本不会出现在
    ``violations`` 里 —— 机器人只记录"它判了什么"，不记录"它为什么这么判"，
    也不记录被引用/被描述的那一段。所以必须有一个人工入口。

    与导入一样：正文过 :func:`redact_replay_text`（管理员手抄进来一个
    @username 或手机号，同样不许留在库里），``gold_label`` 只接受
    ``violation``/``benign``/None。

    ``gold_label`` 是**管理员本人**在创建时给的显式答案（同时写下
    ``label_source='manual_confirm'`` / ``label_reviewed_by=created_by``，
    并留一行标注审计）。不给就是无标注样本 —— 它照样能跑回放，只是不进 FP/FN。
    """

    gid = int(group_id)
    label = str(gold_label or "").strip().lower() or None
    if label is not None and label not in GOLD_LABELS:
        raise ReplayError("replay_gold_label_invalid", "标注只能是 violation 或 benign。")
    raw_text = str(text or "")
    redacted = redact_replay_text(raw_text, extra_ids=(int(created_by or 0),))
    if not redacted:
        raise ReplayError("replay_sample_empty", "样本内容不能为空。")
    instant = now or now_shanghai_naive()
    await _ensure_group_row(session, gid)
    row = ModerationReplaySample(
        group_id=gid,
        source_kind=SOURCE_KIND_MANUAL,
        source_violation_id=None,
        redacted_text=redacted,
        redacted_context=redact_replay_text(
            context, max_len=SAMPLE_CONTEXT_MAX_CHARS, extra_ids=(int(created_by or 0),)
        ),
        gold_label=label,
        label_state=LABEL_STATE_LABELLED if label else LABEL_STATE_UNLABELLED,
        label_source=LABEL_SOURCE_MANUAL_CONFIRM if label else "",
        label_reviewed_by=int(created_by or 0) if label else 0,
        label_reviewed_at=instant if label else None,
        provenance={
            "source": "manual",
            "redaction_applied": bool(redacted != raw_text),
        },
        created_by=int(created_by or 0),
        created_at=instant,
        expires_at=(
            expires_at
            if expires_at is not None
            else instant + timedelta(days=SAMPLE_TTL_DAYS)
        ),
    )
    session.add(row)
    await session.flush()
    if label:
        await _record_label_change(
            session,
            sample=row,
            previous=None,
            new=label,
            action=LABEL_ACTION_MANUAL_CREATE,
            actor_id=int(created_by or 0),
            now=instant,
        )
        await session.flush()
    return row


def _active_sample_filter(now: datetime):
    return or_(
        ModerationReplaySample.expires_at.is_(None),
        ModerationReplaySample.expires_at > now,
    )


async def count_active_samples(
    session: AsyncSession,
    group_id: int,
    *,
    now: datetime | None = None,
) -> int:
    """该群**留存期内**的样本总数（不含过期样本）。

    这是"这次到底评全了没有"的唯一判据：回放按 id 升序只取前
    ``sample_limit`` 条，所以池子大于上限时结果**必然**不完整。不把这个数
    暴露出去，管理员只看到 ``status=completed``，无法区分"25 条全评完"和
    "池子里有 800 条、只评了 25 条"。
    """

    return int(
        (
            await session.scalar(
                select(func.count())
                .select_from(ModerationReplaySample)
                .where(
                    ModerationReplaySample.group_id == int(group_id),
                    _active_sample_filter(now or now_shanghai_naive()),
                )
            )
        )
        or 0
    )


async def list_samples(
    session: AsyncSession,
    group_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
    include_expired: bool = False,
    now: datetime | None = None,
) -> list[ModerationReplaySample]:
    instant = now or now_shanghai_naive()
    stmt = select(ModerationReplaySample).where(
        ModerationReplaySample.group_id == int(group_id)
    )
    if not include_expired:
        stmt = stmt.where(_active_sample_filter(instant))
    stmt = stmt.order_by(ModerationReplaySample.id).offset(max(0, int(offset))).limit(
        max(1, min(int(limit or 0) or 100, 500))
    )
    return list((await session.scalars(stmt)).all())


async def delete_sample(
    session: AsyncSession, group_id: int, sample_id: int
) -> bool:
    """删掉一条样本。False = 不存在或不属于该群。

    **数据策略（手删与 TTL 清理完全一致）**：删样本 = 删它的**结果行**，并
    **立刻重算**受影响 run 的统计。理由：一条结果没有样本就只剩一个孤立的
    ``sample_id``，谁也读不出"这条 verdict 说的是哪句话"，留着只会让报表
    看起来比实际覆盖面更广；而如果留着结果行却**不**重算统计，`stats.total`
    就再也不会和实际结果行对上 —— 那是最坏的一种不一致。

    返回 ``True`` 时调用方**必须 commit**（本函数只 flush）。
    """

    row = await session.get(ModerationReplaySample, int(sample_id))
    if row is None or int(row.group_id) != int(group_id):
        return False
    await _purge_sample_rows(session, [int(sample_id)])
    await session.delete(row)
    await session.flush()
    return True


async def _purge_sample_rows(
    session: AsyncSession, sample_ids: Sequence[int]
) -> set[int]:
    """删掉这些样本的结果行，并把受影响 run 的统计重算一遍。

    返回受影响的 ``run_id`` 集合（调用方在同一个事务里提交）。
    """

    ids = [int(value) for value in sample_ids or () if int(value) > 0]
    if not ids:
        return set()
    run_ids = {
        int(value)
        for value in (
            await session.scalars(
                select(ModerationReplayResult.run_id)
                .where(ModerationReplayResult.sample_id.in_(ids))
                .distinct()
            )
        ).all()
    }
    await session.execute(
        delete(ModerationReplayResult).where(
            ModerationReplayResult.sample_id.in_(ids)
        )
    )
    for run_id in sorted(run_ids):
        await _recompute_run_stats(session, run_id)
    return run_ids


async def _recompute_run_stats(session: AsyncSession, run_id: int) -> dict[str, Any]:
    """按**当前还存在的**结果行重算一个 run 的 ``stats``。

    ``stats`` 里永远不会出现一个与结果行对不上的数字：每删一批结果就重算一次，
    并且把累计删掉的行数记进 ``results_purged``，让"这份报告原来覆盖了 200 条、
    现在只剩 180 条"这件事可见，而不是静默缩小分母。
    """

    rows = list(
        (
            await session.scalars(
                select(ModerationReplayResult)
                .where(ModerationReplayResult.run_id == int(run_id))
                .order_by(ModerationReplayResult.id)
            )
        ).all()
    )
    sample_ids = {int(row.sample_id) for row in rows}
    gold_labels: dict[int, str | None] = {}
    if sample_ids:
        labels = (
            await session.scalars(
                select(ModerationReplaySample).where(
                    ModerationReplaySample.id.in_(sorted(sample_ids))
                )
            )
        ).all()
        for sample in labels:
            gold_labels[int(sample.id)] = (
                str(sample.gold_label) if sample.gold_label else None
            )
    stats = compute_summary(rows, gold_labels)
    run = await session.get(ModerationReplayRun, int(run_id))
    if run is None:
        return stats
    previous = dict(run.stats or {})
    purged = int(previous.get("results_purged") or 0)
    purged += int(previous.get("total") or 0) - int(stats.get("total") or 0)
    stats["results_purged"] = max(0, purged)
    stats["recomputed_at"] = now_shanghai_naive().isoformat()
    run.stats = stats
    await session.flush()
    return stats


async def purge_expired_samples(
    session: AsyncSession,
    *,
    group_id: int | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> int:
    """删掉所有过期的样本正文，返回删除的**样本**行数。

    **数据策略**：到期 = 正文连同它的结果行一起删（见
    :func:`delete_sample` 的说明：留下没有样本的结果行只会让报表看起来比实际
    覆盖面更广），并**立刻重算**受影响 run 的统计、记 ``results_purged``。

    结果行里没有任何用户正文或规则内容，所以"删掉正文"这件事本身就完成了
    "不长期留存用户发言与敏感规则数据"的要求；真正需要长期保留的判定历史由
    管理员自己在导出报告里做。

    ``limit`` = 单次**最多**删多少条样本（按 id 升序取前 N 条）。它是给周期
    维护任务用的**有界**闸：没有它，一次清理就是一个"全表扫描 + 一笔可能横跨
    很久的写事务"，而 SQLite 的写锁是**全局**的 —— 一次清理可以把整个面板的
    写请求卡住 5s（然后超时）。传 ``None``（默认）保留"全删"的语义，只给一次性
    的显式调用用。
    """

    instant = now or now_shanghai_naive()
    conditions = [
        ModerationReplaySample.expires_at.isnot(None),
        ModerationReplaySample.expires_at <= instant,
    ]
    if group_id is not None:
        conditions.append(ModerationReplaySample.group_id == int(group_id))
    statement = (
        select(ModerationReplaySample.id)
        .where(*conditions)
        .order_by(ModerationReplaySample.id)
    )
    if limit is not None:
        statement = statement.limit(max(0, int(limit)))
    rows = [int(value) for value in (await session.scalars(statement)).all()]
    if not rows:
        return 0
    await _purge_sample_rows(session, rows)
    # 按**这一批的 id** 删，而不是照着过期条件整片删：后者会让 ``limit`` 只限制
    # 了"选中哪几条要重算统计"，实际删除仍然是全量 —— 有界性就成了假的。
    result = await session.execute(
        delete(ModerationReplaySample).where(
            ModerationReplaySample.id.in_(rows)
        )
    )
    await session.flush()
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# 基线冻结 + 候选校验
# --------------------------------------------------------------------------- #
async def freeze_baseline(
    session: AsyncSession,
    group_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """冻结"这一刻线上生效的口径"：该群启用的群规 + 当前生效的审核提示词。

    ``baseline_digest = sha256(canonical_json(snapshot))``；``baseline_version``
    里带上 ``rules_fingerprint``（与活链路同一个算法）便于和线上日志对照。

    注意快照里的 ``prompt`` 是**全局**审核提示词：它是超管级配置，可能含私有
    运维设定。API 层对普通群管理员**必须**把它抹掉（见
    ``settings_api._replay_snapshot_for_audience``），只留 ``prompt_digest``。
    """

    gid = int(group_id)
    rows = (
        await session.scalars(
            select(ModerationRule)
            .where(
                ModerationRule.group_id == gid,
                ModerationRule.enabled == True,  # noqa: E712 - 与活链路同口径
            )
            .order_by(ModerationRule.id)
        )
    ).all()
    rules = [rule_snapshot_from_row(row) for row in rows]
    prompt = str(get_prompt("moderation") or "")
    snapshot = {
        "group_id": gid,
        "captured_at": (now or now_shanghai_naive()).isoformat(),
        "prompt": prompt,
        "rules": [rule.to_dict() for rule in rules],
    }
    fingerprint = compute_rules_fingerprint(rules)
    prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    return {
        "snapshot": snapshot,
        "digest": digest_of(snapshot),
        "prompt_digest": prompt_digest,
        "version": f"rules:{fingerprint}+prompt:{prompt_digest}",
        "rules_fingerprint": fingerprint,
    }


def _validate_candidate_rule(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReplayError(
            "replay_rule_invalid", f"第 {index + 1} 条候选规则格式无效。"
        )
    unknown = set(value) - {
        "id",
        "rule_type",
        "pattern",
        "action",
        "enabled",
        "scan_scope",
    }
    if unknown:
        raise ReplayError(
            "replay_rule_invalid",
            f"第 {index + 1} 条候选规则包含不允许的字段：{', '.join(sorted(unknown))}",
        )
    rule_type = str(value.get("rule_type") or "keyword").strip().lower()
    if rule_type not in RULE_TYPES:
        raise ReplayError(
            "replay_rule_invalid", f"第 {index + 1} 条候选规则的类型无效。"
        )
    pattern = str(value.get("pattern") or "").strip()
    if not pattern or len(pattern) > RULE_PATTERN_MAX_CHARS:
        raise ReplayError(
            "replay_rule_invalid",
            f"第 {index + 1} 条候选规则的群规内容为空或超过 "
            f"{RULE_PATTERN_MAX_CHARS} 字符。",
        )
    action = str(value.get("action") or "warn").strip().lower()
    if action not in RULE_ACTIONS:
        raise ReplayError(
            "replay_rule_invalid", f"第 {index + 1} 条候选规则的处置动作无效。"
        )
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ReplayError(
            "replay_rule_invalid", f"第 {index + 1} 条候选规则的启用状态必须是布尔值。"
        )
    scan_scope = value.get("scan_scope") or "message"
    if not isinstance(scan_scope, str):
        raise ReplayError(
            "replay_rule_invalid", f"第 {index + 1} 条候选规则的扫描范围无效。"
        )
    # id 只用于让模型能 ``rule_id`` 指回来（离线候选没有库 id）。没给就按位置
    # 从 1 起编 —— **保留**它而不是丢掉：丢掉的话所有规则的 id 都变成 0，
    # 模型回报的 rule_id 就没有任何意义了。
    if "id" in value and value["id"] is not None:
        raw_id = value["id"]
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise ReplayError(
                "replay_rule_invalid", f"第 {index + 1} 条候选规则的 id 无效。"
            )
        rule_id = int(raw_id)
    else:
        rule_id = index + 1
    return {
        "id": rule_id,
        "rule_type": rule_type,
        "pattern": pattern,
        "action": action,
        "enabled": bool(enabled),
        "scan_scope": normalize_scan_scope(scan_scope),
    }


async def validate_candidate(
    candidate: Any,
    *,
    baseline_prompt: str = "",
) -> dict[str, Any]:
    """把候选收敛成一份**版本化提案**，并给出版本号与摘要。

    只接受 ``{"rules": [...], "prompt": "..."}``（``prompt`` 可省略 = 沿用当前
    生效的提示词）。不接受任何自由文本指令：候选必须是"能落成群规/提示词的东西"，
    否则回放就没有可复现的对照物。

    规则逐条过 :func:`_validate_candidate_rule`（与群规 schema 同口径）；
    提示词过 ``PromptSettingsConfig`` **同一个**校验器（含 ``{rules_json}``
    占位符与 ``.format()`` 干净性）——候选提示词要是过不了生产那关，
    就不该被拿来"看起来可用"。
    """

    if not isinstance(candidate, dict):
        raise ReplayError(
            "replay_candidate_invalid",
            "候选必须是 {rules: [...], prompt?: \"...\"} 结构。",
        )
    unknown = set(candidate) - {"rules", "prompt"}
    if unknown:
        raise ReplayError(
            "replay_candidate_invalid",
            f"候选包含不允许的字段：{', '.join(sorted(unknown))}",
        )
    if "rules" not in candidate:
        raise ReplayError(
            "replay_candidate_invalid",
            "候选必须包含 rules 数组（可以是空数组）。",
        )
    raw_rules = candidate.get("rules")
    if not isinstance(raw_rules, list) or len(raw_rules) > MAX_CANDIDATE_RULES:
        raise ReplayError("replay_candidate_invalid", "候选 rules 必须是数组。")
    rules = [
        _validate_candidate_rule(item, index) for index, item in enumerate(raw_rules)
    ]

    raw_prompt = candidate.get("prompt")
    if raw_prompt is None or str(raw_prompt).strip() == "":
        prompt = str(baseline_prompt or "")
    else:
        if not isinstance(raw_prompt, str):
            raise ReplayError("replay_prompt_invalid", "候选提示词必须是字符串。")
        prompt = raw_prompt
        try:
            PromptSettingsConfig(moderation=prompt)
        except Exception as exc:  # pydantic ValidationError / ValueError
            raise ReplayError(
                "replay_prompt_invalid", "候选提示词未通过与线上相同的校验。"
            ) from exc

    canonical = {"prompt": prompt, "rules": rules}
    digest = digest_of(canonical)
    return {
        "rules": rules,
        "prompt": prompt,
        "digest": digest,
        "version": f"candidate:{digest[:16]}",
    }


# --------------------------------------------------------------------------- #
# 模型绑定 + 保真度声明
# --------------------------------------------------------------------------- #
def capture_model_binding(settings: Settings) -> dict[str, Any]:
    """把"这一轮会用哪个模型、怎么调"拍成一份**不可变的非密钥**摘要。

    为什么必须在**建 run 那一刻**拍、而不是执行时现读 ``settings``：回放常常
    排队等一会儿才跑，而这中间任何人都可能改模型配置。等到执行时现读，结果里
    写的"这是模型 X 跑出来的"就可能与真正跑它的模型不是同一个 —— 一份自称可复现
    的报告，两份配置，两种说法。

    **绝不包含**：``api_key``（任何密钥）、价格/单价（仓库里没有价格表，编一个
    出来比没有更糟）、请求 URL 原文（只留摘要）。这些字段一旦进库就等于把部署
    细节抄进了可被群管理员读到的 API 响应里。
    """

    cfg = getattr(settings.bot, "moderation_model", None) or settings.bot.decision_model
    binding: dict[str, Any] = {
        "provider": str(getattr(cfg, "provider", "") or ""),
        "model": str(getattr(cfg, "model", "") or ""),
        # 端点只留摘要：够用来证明"端点没变"，又不至于把内网 URL 发出去。
        "endpoint_digest": digest_of(
            {
                "api_base": str(getattr(cfg, "api_base", "") or ""),
                "endpoint_path": str(getattr(cfg, "endpoint_path", "") or ""),
                "chat_endpoint": str(getattr(cfg, "chat_endpoint", "") or ""),
            }
        )[:16],
        "stream": bool(getattr(cfg, "stream", False)),
        "temperature": float(getattr(cfg, "temperature", 0.0) or 0.0),
        "max_tokens": int(getattr(cfg, "max_tokens", 0) or 0),
        "timeout_sec": float(getattr(cfg, "timeout_sec", 0.0) or 0.0),
        "retry_attempts": int(getattr(cfg, "retry_attempts", 0) or 0),
        "fallback_count": len(getattr(cfg, "fallbacks", ()) or ()),
        # provider 自定义的请求参数只留摘要（可能含运营细节，也可能是密钥名）。
        "request_params_digest": digest_of(
            dict(getattr(cfg, "request_params", None) or {})
        )[:16],
    }
    binding["digest"] = digest_of(binding)
    return binding


def build_fidelity_block(
    *,
    model_digest: str,
    samples_evaluated: int = 0,
    samples_redaction_changed: int = 0,
    executed_model_digest: str | None = None,
) -> dict[str, Any]:
    """把"这份回放在哪些地方**不等于**线上"写进结果元数据。

    一份没有这些限制声明的回放报告会被当成"照着线上跑了一遍"，而事实是：

    * 送进模型的是**脱敏后的副本**，掩码（``[id]`` / ``@[redacted]``）可能正好落在
      群规要抓的词上 —— 所以必须说清"有几条的文本被脱敏改动过"；
    * 样本**没有原始上下文**（群里前 20 条、被引用的话、图片原文都不在），线上是有的；
    * 因此**绝不**等价于一次生产回放。``equivalent_to_production_replay`` 恒为
      ``False``，这不是谦虚，是事实陈述。
    """

    return {
        "model_digest": str(model_digest or ""),
        "executed_model_digest": str(executed_model_digest or ""),
        "model_binding_drift": bool(
            executed_model_digest and model_digest and executed_model_digest != model_digest
        ),
        "redaction_applied": True,
        "samples_evaluated": int(samples_evaluated or 0),
        "samples_redaction_changed": int(samples_redaction_changed or 0),
        "context_absent": True,
        "equivalent_to_production_replay": False,
        "notes": (
            "样本已脱敏且不含原始上下文；本结果不能等同于生产回放，"
            "只能作为改动口径的离线参考。"
        ),
    }


# --------------------------------------------------------------------------- #
# 判定：显式规则快照 + 显式提示词
# --------------------------------------------------------------------------- #
def _regex_deadline_budget(seconds: float = 0.1) -> Callable[[], float]:
    deadline = time.perf_counter() + seconds
    return lambda: max(0.0, deadline - time.perf_counter())


def _scoped_candidates(
    segments: Any, *, include_quote: bool, include_vision: bool
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """按 scan_scope 拼出参与匹配的候选文本（含大小写折叠视图）。

    段拆分与范围语义全部委托给 ``moderation`` 模块（引文块、图片描述块、命令
    别名都在那里），这里只做"这条规则要不要并入引文/图片描述"的取舍。
    """

    if not include_quote and not include_vision:
        source = segments.own
    else:
        source = segments.scoped(
            include_quote=include_quote, include_vision=include_vision
        )
    candidates = _moderation_match_candidates(source)
    return candidates, tuple(item.casefold() for item in candidates)


def _locate_match_source(
    pattern: str,
    *,
    include_quote: bool,
    include_vision: bool,
    keyword_folded: str,
    segments_own_candidates: tuple[str, ...],
    segments_own_folded: tuple[str, ...],
    segments: Any,
    remaining: Callable[[], float],
) -> str:
    """命中已经发生，定位它来自 own / quote / vision。

    与 ``ModerationService.evaluate`` 的归属规则一致：定位不到时**一律按 own
    fail-closed**。猜成"来自引文"等于把被引用者的责任推给引用者（生产事故
    F-001 就是这么来的），宁可误伤也不猜。
    """

    if not include_quote and not include_vision:
        return MATCH_SOURCE_OWN
    if keyword_folded:
        own_hit = any(keyword_folded in item for item in segments_own_folded)
    else:
        own_hit = _regex_hit(pattern, segments_own_candidates, remaining)
    if own_hit:
        return MATCH_SOURCE_OWN
    for bucket, enabled in (
        (SCAN_SCOPE_QUOTE, include_quote),
        (SCAN_SCOPE_VISION, include_vision),
    ):
        if not enabled:
            continue
        payload = segments.quote if bucket == SCAN_SCOPE_QUOTE else segments.vision
        candidates = _moderation_match_candidates(payload)
        if keyword_folded:
            hit = any(keyword_folded in item.casefold() for item in candidates)
        else:
            hit = _regex_hit(pattern, candidates, remaining)
        if hit:
            return bucket
    return MATCH_SOURCE_OWN


def _regex_hit(
    pattern: str, candidates: tuple[str, ...], remaining: Callable[[], float]
) -> bool:
    for candidate in candidates:
        budget = remaining()
        if budget <= 0:
            return False
        try:
            if safe_regex.search(
                pattern,
                candidate,
                flags=safe_regex.IGNORECASE,
                timeout=min(0.02, budget),
            ) is not None:
                return True
        except (safe_regex.error, TimeoutError):
            return False
    return False


def scan_deterministic(
    rules: Sequence[RuleSnapshot], text: str
) -> tuple[ModerationVerdict | None, list[RuleSnapshot], bool]:
    """跑本地关键词/正则规则。

    返回 ``(命中判定 or None, 需要送审的语义规则, 是否出现不可信)``。
    与 ``ModerationService.evaluate`` 的本地段落同口径（段拆分、扫描范围、
    反对信号豁免、命中来源、1.0 置信度占位），差别只有一处：它拿的是**快照**，
    不查库、不写任何东西。

    ``tests/test_moderation_replay.py::test_scan_matches_live_service_on_every_case``
    把两边的判定逐例对拍；改这一段之前先看那个测试。
    """

    semantic_rules: list[RuleSnapshot] = []
    inconclusive = False
    if not rules:
        return None, semantic_rules, inconclusive

    segments = split_moderation_text(str(text or ""))
    own_candidates = _moderation_match_candidates(segments.own)
    own_folded = tuple(item.casefold() for item in own_candidates)
    objection_state: list[bool] = []
    # 与活链路一样：**所有规则共享一个正则预算**（0.1s），不是每条规则各给一份。
    # 给每条一份等于把活链路的"预算耗尽 → inconclusive"这道保险放宽了 N 倍。
    remaining = _regex_deadline_budget()

    def own_objects() -> bool:
        if not objection_state:
            objection_state.append(_own_text_objects(own_candidates))
        return objection_state[0]

    for rule in rules:
        rule_type = (rule.rule_type or "keyword").strip().lower()
        pattern = (rule.pattern or "").strip()
        scope = parse_scan_scope(rule.scan_scope)
        include_quote = SCAN_SCOPE_QUOTE in scope
        include_vision = SCAN_SCOPE_VISION in scope
        if rule_type not in ("keyword", "regex"):
            # 未知的历史类型按语义规则处理（与活链路一致），绝不静默忽略。
            semantic_rules.append(rule)
            continue
        if not pattern:
            inconclusive = True
            log.warning("replay: empty %s rule ignored (rule=%s)", rule_type, rule.id)
            continue
        candidates, folded_candidates = _scoped_candidates(
            segments,
            include_quote=include_quote,
            include_vision=include_vision,
        )
        if rule_type == "keyword":
            folded_pattern = pattern.casefold()
            matched = any(folded_pattern in item for item in folded_candidates)
        else:
            matched = False
            for candidate in candidates:
                budget = remaining()
                if budget <= 0:
                    inconclusive = True
                    log.warning(
                        "replay: regex budget exhausted (rule=%s)", rule.id
                    )
                    break
                try:
                    matched = safe_regex.search(
                        pattern,
                        candidate,
                        flags=safe_regex.IGNORECASE,
                        timeout=min(0.02, budget),
                    ) is not None
                except (safe_regex.error, TimeoutError):
                    inconclusive = True
                    log.warning("replay: invalid/timed-out regex (rule=%s)", rule.id)
                    break
                if matched:
                    break
        if not matched:
            continue
        source = _locate_match_source(
            pattern,
            include_quote=include_quote,
            include_vision=include_vision,
            keyword_folded=(
                pattern.casefold() if rule_type == "keyword" else ""
            ),
            segments_own_candidates=own_candidates,
            segments_own_folded=own_folded,
            segments=segments,
            remaining=remaining,
        )
        if source != MATCH_SOURCE_OWN and own_objects():
            # 用户本人在反对/警示（"这是骗子别信"），不追究。
            log.info(
                "replay: 审核豁免 (命中来自 %s，但本人在警示) rule=%s", source, rule.id
            )
            continue
        reason = "命中关键词规则" if rule_type == "keyword" else "命中正则规则"
        log.info("replay: 命中本地规则 rule=%s source=%s", rule.id, source)
        return (
            ModerationVerdict(
                violated=True,
                reason=reason,
                rule=None,
                conclusive=True,
                confidence=1.0,
                match_source=source,
                deterministic=True,
            ),
            semantic_rules,
            inconclusive,
        )
    return None, semantic_rules, inconclusive


def build_replay_user_input(
    rules: Sequence[RuleSnapshot],
    prompt_template: str,
    text: str,
    context: str = "",
) -> tuple[str, str]:
    """按**活链路同一套拼装**生成 (system, user)。

    与 ``ModerationService.evaluate`` 的送审拼装逐行对应：规则块降级成 user
    轮的被包裹数据块（管理员写的自由文本是**数据**不是指令，F-018），群内
    上下文与待审消息同样走 ``wrap_untrusted``。这一段**一行都不许"优化"**：
    回放的结论要能代表线上，prompt 拼装就不许有第二个版本。

    注意：规则块里的 id 取自快照（候选规则由 ``validate_candidate`` 从 1 起编号）。
    模型返回的 ``rule_id`` 在回放里**只用于展示**：它既不匹配任何处置，也不回写
    任何群规——这正是 dry-run 的边界。
    """

    semantic_rules = [
        rule
        for rule in rules
        if (rule.rule_type or "keyword").strip().lower() not in ("keyword", "regex")
    ]
    rules_payload = [
        {
            "id": int(rule.id),
            "rule_type": rule.rule_type,
            "rule": rule.pattern,
            "action": rule.action,
        }
        for rule in semantic_rules
    ]
    rules_json = json.dumps(rules_payload, ensure_ascii=False, indent=2)
    rules_block = wrap_untrusted(
        RULES_BLOCK_LABEL, rules_json, max_len=_RULES_BLOCK_MAX_CHARS
    )
    system_prompt = build_defended_system(
        str(prompt_template or "").format(rules_json=_RULES_BLOCK_POINTER)
    )
    clean_context = clean_text(str(context or ""), max_len=1200)
    parts = [rules_block]
    if clean_context:
        parts.append(wrap_untrusted("群内上下文", clean_context, max_len=1200))
    parts.append(
        wrap_untrusted("待审核消息", clean_text(str(text or ""), max_len=1200), max_len=1200)
    )
    return system_prompt, "\n\n".join(parts)


@dataclass(frozen=True, slots=True)
class ReplayEvaluation:
    """一次离线判定的完整结果（含**失败**的可见化）。"""

    verdict: str
    violated: bool
    conclusive: bool
    reason: str = ""
    rule_id: int | None = None
    confidence: float | None = None
    match_source: str = ""
    deterministic: bool = False
    error: str = ERROR_NONE
    latency_ms: int = 0
    model_calls: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    rules_fingerprint: str = ""


async def evaluate_snapshot(
    rules: Sequence[RuleSnapshot],
    prompt_template: str,
    text: str,
    context: str = "",
    *,
    llm: Any,
    timeout_seconds: float = REPLAY_MODEL_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.perf_counter,
) -> ReplayEvaluation:
    """拿着**显式**的规则快照与提示词判一条样本。

    这是回放唯一允许的判定入口。它**刻意不复用** ``ModerationService.evaluate``：
    后者从库里读启用中的群规、还会查送审预算与整形闸 —— 那两样都会让"基线"
    在跑的过程中漂移（管理员改一条群规，这一轮的基线就变了）。

    失败语义：模型抛异常 / 超时 / 空输出 / 输出不可解析 / ``violated`` 字段
    非法，各落一个**不同的** ``error`` 值并一律 ``conclusive=False``，
    ``verdict="error"``。失败**绝不**折成"不违规/审核通过"——那正是活链路
    曾经把"模型挂了"记成"干净"的失败模式。
    """

    started = clock()
    fingerprint = compute_rules_fingerprint(rules)
    model_calls = 0
    usage = {"prompt_tokens": 0, "output_tokens": 0}

    def finish(evaluation: ReplayEvaluation) -> ReplayEvaluation:
        elapsed = int(round(max(0.0, clock() - started) * 1000))
        return ReplayEvaluation(
            verdict=evaluation.verdict,
            violated=evaluation.violated,
            conclusive=evaluation.conclusive,
            reason=evaluation.reason,
            rule_id=evaluation.rule_id,
            confidence=evaluation.confidence,
            match_source=evaluation.match_source,
            deterministic=evaluation.deterministic,
            error=evaluation.error,
            latency_ms=elapsed,
            model_calls=model_calls,
            usage=dict(usage),
            rules_fingerprint=fingerprint,
        )

    if not rules:
        # 与活链路一致：没有启用规则 = 没有可判口径，直接干净（conclusive=True）。
        return finish(
            ReplayEvaluation(
                verdict=VERDICT_BENIGN,
                violated=False,
                conclusive=True,
            )
        )

    hit, semantic_rules, deterministic_inconclusive = scan_deterministic(rules, text)
    if hit is not None:
        return finish(
            ReplayEvaluation(
                verdict=VERDICT_VIOLATION,
                violated=True,
                conclusive=True,
                reason=hit.reason,
                rule_id=None,
                confidence=hit.confidence,
                match_source=hit.match_source,
                deterministic=True,
            )
        )
    if not semantic_rules:
        # ``reason`` 与活链路一致留空：它只是给日志看的一句话，不参与任何判定，
        # 留一个字都可能被下游误当成"有理由的放行"。
        #
        # 但 ``conclusive=False`` 必须**带可见原因**：本地扫描没跑完（正则非法 /
        # 预算耗尽 / 规则为空）时，"没扫到"和"扫过了没命中"是两回事，折成后者
        # 就是把一次扫描失败报成"干净"。所以这里落
        # ``ERROR_INCONCLUSIVE_RULES``，并被 :func:`compute_summary` 排除在
        # "确认干净"与通过率之外。
        return finish(
            ReplayEvaluation(
                verdict=VERDICT_BENIGN,
                violated=False,
                conclusive=not deterministic_inconclusive,
                error=(
                    ERROR_INCONCLUSIVE_RULES if deterministic_inconclusive else ERROR_NONE
                ),
            )
        )
    if llm is None:
        return finish(
            ReplayEvaluation(
                verdict=VERDICT_ERROR,
                violated=False,
                conclusive=False,
                error=ERROR_MODEL_ERROR,
            )
        )

    system_prompt, user_input = build_replay_user_input(
        rules, prompt_template, text, context
    )
    usage["prompt_tokens"] = estimate_text_tokens(system_prompt) + estimate_text_tokens(
        user_input
    )
    # 只记**回放自己**能归因的事件。``LLMService`` 内部的重试/fallback 已经按
    # 每次 provider 尝试往 "moderation" stage 记过一次账，这里再记一次就是
    # "同一笔开销进两个 stage"。所以这里只补 llm.py 记不到的**估算** token。
    llm_metrics.record_usage(
        METRICS_STAGE[:_MAX_METRICS_STAGE],
        None,
        prompt_tokens=usage["prompt_tokens"],
        output_tokens=0,
    )
    model_calls = 1
    try:
        # ``background=True`` 是**必需**的，不是锦上添花：``LLMService.chat`` 在
        # ``use_moderation=True`` 时会自己套一个 HIGH 的 scope，光靠外面那层
        # ``execution_priority_scope(BACKGROUND)`` 会被它整个盖掉 —— 于是回放变成
        # 一个普通的 HIGH 消费者，"不吃活链路保留额度"这句话就是假的。这个参数
        # 只作用于**这一次**调用（连同它的重试与 fallback），活链路审核不受影响。
        raw = await asyncio.wait_for(
            llm.moderation(system_prompt, user_input, background=True),
            timeout=max(1.0, float(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        llm_metrics.record(METRICS_STAGE[:_MAX_METRICS_STAGE], timeouts=1)
        log.warning("replay: 模型调用超时，按 error 记录（不折成通过）")
        return finish(
            ReplayEvaluation(
                verdict=VERDICT_ERROR,
                violated=False,
                conclusive=False,
                error=ERROR_MODEL_TIMEOUT,
            )
        )
    except Exception:  # noqa: BLE001 - 任何 provider 异常都归到同一个稳定码
        llm_metrics.record(METRICS_STAGE[:_MAX_METRICS_STAGE], failures=1)
        # **只记稳定错误码**：异常文本、traceback、locals 里都可能带着 provider
        # 原文 / URL / key / 用户发言。``exc_info=True`` 会把原始异常整个写进日志
        # （包括 traceback 的局部变量），所以这里连异常类型名都不记 —— 分类信息
        # 已经由 ``ERROR_MODEL_ERROR`` 这个稳定码承载。
        log.warning("replay: 模型调用失败 error=%s", ERROR_MODEL_ERROR)
        return finish(
            ReplayEvaluation(
                verdict=VERDICT_ERROR,
                violated=False,
                conclusive=False,
                error=ERROR_MODEL_ERROR,
            )
        )

    raw_text = "" if raw is None else str(raw)
    usage["output_tokens"] = estimate_text_tokens(raw_text)
    if not raw_text.strip():
        # "有请求、没出声"与"出声了但不是 JSON"是两回事，看板上分开计。
        llm_metrics.record(METRICS_STAGE[:_MAX_METRICS_STAGE], empty_responses=1)
        return finish(
            ReplayEvaluation(
                verdict=VERDICT_ERROR,
                violated=False,
                conclusive=False,
                error=ERROR_EMPTY_RESPONSE,
            )
        )

    data = _parse_moderation_json(raw_text)
    if not data:
        llm_metrics.record(METRICS_STAGE[:_MAX_METRICS_STAGE], parse_errors=1)
        log.warning("replay: 模型输出不可解析，response_len=%d", len(raw_text))
        return finish(
            ReplayEvaluation(
                verdict=VERDICT_ERROR,
                violated=False,
                conclusive=False,
                error=ERROR_UNPARSEABLE,
            )
        )
    violated_value = data.get("violated", data.get("violation"))
    if not isinstance(violated_value, bool):
        return finish(
            ReplayEvaluation(
                verdict=VERDICT_ERROR,
                violated=False,
                conclusive=False,
                error=ERROR_INVALID_VERDICT_FIELD,
            )
        )
    confidence, confidence_valid = _parse_confidence(data.get("confidence"))
    reason = clean_text(str(data.get("reason", "")).strip(), max_len=120)
    rule_id = None
    raw_rule_id = data.get("rule_id")
    if raw_rule_id is not None:
        try:
            rule_id = int(raw_rule_id)
        except (TypeError, ValueError):
            rule_id = None
    if violated_value:
        return finish(
            ReplayEvaluation(
                verdict=VERDICT_VIOLATION,
                violated=True,
                conclusive=confidence_valid,
                reason=reason or "命中群规（AI判定）",
                rule_id=rule_id,
                confidence=confidence if confidence_valid else None,
                match_source=MATCH_SOURCE_SEMANTIC,
                # 没有可解析的 confidence = "模型没给准头绪"，和"模型给了个低分"
                # 是两回事。它必须自己说得出来，而不是悄悄被当成一个普通的通过。
                error=ERROR_NONE if confidence_valid else ERROR_MISSING_CONFIDENCE,
            )
        )
    return finish(
        ReplayEvaluation(
            verdict=VERDICT_BENIGN,
            violated=False,
            # 两种判不出来各有各的原因码：本地扫描没跑完、模型没给 confidence。
            conclusive=confidence_valid and not deterministic_inconclusive,
            rule_id=rule_id,
            confidence=confidence if confidence_valid else None,
            error=(
                ERROR_INCONCLUSIVE_RULES
                if deterministic_inconclusive
                else (
                    ERROR_NONE if confidence_valid else ERROR_MISSING_CONFIDENCE
                )
            ),
        )
    )


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def _percentile(values: Sequence[int], fraction: float) -> int | None:
    """nearest-rank 分位数：排序后取第 ``ceil(p*n)`` 个。

    为什么不用线性插值：小样本（回放常常只有十几条样本）里插值会造出
    一个**从未发生**的时延；nearest-rank 报出来的每一个数都是真实观测值。
    """

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return int(ordered[min(rank, len(ordered)) - 1])


def _latency_block(prefix: str, values: Sequence[int]) -> dict[str, Any]:
    return {
        f"{prefix}_latency_ms_avg": round(sum(values) / len(values), 3) if values else None,
        f"{prefix}_latency_ms_p50": _percentile(values, 0.5),
        f"{prefix}_latency_ms_p95": _percentile(values, 0.95),
    }


def _error_breakdown(rows: Sequence[Any], prefix: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        code = str(getattr(row, f"{prefix}_error", "") or "")
        if not code:
            continue
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


def compute_summary(
    results: Sequence[Any], gold_labels: dict[int, str | None]
) -> dict[str, Any]:
    """把一批结果行汇总成统计。**口径全部写死在这里**，可逐条对账。

    判定三值：``violation`` / ``benign`` / ``error``。``error`` 既不算违规、
    也不算通过 —— 它只进 ``*_error`` 与 ``*_error_breakdown``。

    **"确认"口径（最重要的一条）**
    ``*_confirmed_violation`` / ``*_confirmed_benign`` **只数 ``conclusive=True``
    的行**。``conclusive=False`` 的行 —— 无论它的 ``verdict`` 字面写着什么 ——
    一律只进 ``*_inconclusive`` 和 ``*_inconclusive_breakdown``（按稳定错误码
    分类）。一次正则非法/预算耗尽的扫描、或一次模型没给 confidence 的回答，
    都不许出现在"干净/违规"这两个计数里，更不许进任何通过率的分母：
    把"判不出来"记成"通过"是这类报表最贵的一次撒谎。

    FP/FN **只对带 gold label 的样本**计算：

    * ``false_positive`` = 候选**确认** ``benign`` 且 gold 是 ``violation``
      （该抓的没抓到，对候选而言是"放过"）
    * ``false_negative`` = 候选**确认** ``violation`` 且 gold 是 ``benign``
      （不该抓的抓了，误伤）
    * ``true_positive`` = 候选**确认** ``violation`` 且 gold 是 ``violation``
    * ``true_negative`` = 候选**确认** ``benign`` 且 gold 是 ``benign``
    * 候选 ``error`` 或 ``conclusive=False`` 的有标签样本**四个都不算**，单列在
      ``labeled_scored`` 与 ``labeled_total`` 的差里。
    * 无标签样本只进 ``total``，``labeled_total`` 不含它们。所以
      ``accuracy`` 在一个全是无标签样本的集合上恒为 ``None``，
      绝不会冒充"100% 准确"。

    用量口径：**逻辑调用次数**与**真实 provider 尝试次数**分开报。
    ``LLMService.moderation`` 只返回 ``str``，内部的重试/fallback 到不了这一层，
    所以真实尝试次数**不知道就是不知道**（``None`` + ``provider_attempts_known
    =False``），绝不把一次 ``LLMService`` 调用记成一次真实 provider 调用。
    token 是 :func:`estimate_text_tokens` 的**估算**，字段名里带 ``estimated``。

    金额：``estimated_cost`` 恒为 ``None``。仓库里没有模型价格表（已确认），
    凭印象写一个单价出来的"成本"比没有成本更糟。
    """

    rows = list(results or [])
    baseline_violation = 0
    candidate_violation = 0
    baseline_benign = 0
    candidate_benign = 0
    baseline_confirmed_violation = 0
    baseline_confirmed_benign = 0
    candidate_confirmed_violation = 0
    candidate_confirmed_benign = 0
    baseline_inconclusive = 0
    candidate_inconclusive = 0
    changed_count = 0
    true_positive = false_positive = true_negative = false_negative = 0
    labeled_total = 0
    labeled_scored = 0
    baseline_latencies: list[int] = []
    candidate_latencies: list[int] = []
    logical_calls = 0
    prompt_tokens = 0
    output_tokens = 0
    attempts_known = False
    attempts_total = 0

    for row in rows:
        baseline_verdict = str(getattr(row, "baseline_verdict", "") or "")
        candidate_verdict = str(getattr(row, "candidate_verdict", "") or "")
        baseline_conclusive = bool(getattr(row, "baseline_conclusive", False))
        candidate_conclusive = bool(getattr(row, "candidate_conclusive", False))
        if baseline_verdict == VERDICT_VIOLATION:
            baseline_violation += 1
            if baseline_conclusive:
                baseline_confirmed_violation += 1
        elif baseline_verdict == VERDICT_BENIGN:
            baseline_benign += 1
            if baseline_conclusive:
                baseline_confirmed_benign += 1
        if candidate_verdict == VERDICT_VIOLATION:
            candidate_violation += 1
            if candidate_conclusive:
                candidate_confirmed_violation += 1
        elif candidate_verdict == VERDICT_BENIGN:
            candidate_benign += 1
            if candidate_conclusive:
                candidate_confirmed_benign += 1
        if not baseline_conclusive:
            baseline_inconclusive += 1
        if not candidate_conclusive:
            candidate_inconclusive += 1
        if baseline_verdict != candidate_verdict:
            changed_count += 1
        for prefix, bucket in (
            ("baseline", baseline_latencies),
            ("candidate", candidate_latencies),
        ):
            latency = getattr(row, f"{prefix}_latency_ms", None)
            if latency is not None:
                bucket.append(int(latency))
            usage = getattr(row, f"{prefix}_usage", None) or {}
            logical_calls += int(usage.get("logical_calls", usage.get("calls")) or 0)
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            attempts = usage.get("provider_attempts")
            if attempts is not None:
                attempts_known = True
                attempts_total += int(attempts)
        sample_id = int(getattr(row, "sample_id", 0) or 0)
        gold = gold_labels.get(sample_id)
        if not gold:
            continue
        labeled_total += 1
        if candidate_verdict == VERDICT_ERROR or not candidate_conclusive:
            # 判不出来的有标签样本既不算 FN 也不算 TN：它在 labeled_scored 之外。
            continue
        labeled_scored += 1
        if candidate_verdict == VERDICT_VIOLATION and gold == LABEL_VIOLATION:
            true_positive += 1
        elif candidate_verdict == VERDICT_BENIGN and gold == LABEL_VIOLATION:
            false_positive += 1
        elif candidate_verdict == VERDICT_VIOLATION and gold == LABEL_BENIGN:
            false_negative += 1
        else:
            true_negative += 1

    combined = baseline_latencies + candidate_latencies
    summary: dict[str, Any] = {
        "total": len(rows),
        "baseline_violation": baseline_violation,
        "baseline_benign": baseline_benign,
        "candidate_violation": candidate_violation,
        "candidate_benign": candidate_benign,
        "baseline_confirmed_violation": baseline_confirmed_violation,
        "baseline_confirmed_benign": baseline_confirmed_benign,
        "candidate_confirmed_violation": candidate_confirmed_violation,
        "candidate_confirmed_benign": candidate_confirmed_benign,
        "changed_count": changed_count,
        "baseline_inconclusive": baseline_inconclusive,
        "candidate_inconclusive": candidate_inconclusive,
        "baseline_inconclusive_breakdown": _error_breakdown(rows, "baseline"),
        "candidate_inconclusive_breakdown": _error_breakdown(rows, "candidate"),
        "baseline_error": sum(
            1
            for row in rows
            if str(getattr(row, "baseline_error", "") or "") not in ("", ERROR_NONE)
        ),
        "candidate_error": sum(
            1
            for row in rows
            if str(getattr(row, "candidate_error", "") or "") not in ("", ERROR_NONE)
        ),
        "baseline_error_breakdown": _error_breakdown(rows, "baseline"),
        "candidate_error_breakdown": _error_breakdown(rows, "candidate"),
        # 有多少结果行因为样本被删（TTL 或手删）而消失。恒为 0 表示"这份报告
        # 覆盖的就是它算出来的全部样本"；>0 时前端**必须**把它显示出来 ——
        # 否则"原来 200 条、现在只剩 180 条"这件事会被静默地藏进分母里。
        "results_purged": 0,
        "labeled_total": labeled_total,
        "labeled_scored": labeled_scored,
        "true_positive": true_positive,
        "true_negative": true_negative,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "accuracy": (
            round((true_positive + true_negative) / labeled_scored, 4)
            if labeled_scored
            else None
        ),
        "false_positive_rate": (
            round(false_positive / labeled_scored, 4) if labeled_scored else None
        ),
        "false_negative_rate": (
            round(false_negative / labeled_scored, 4) if labeled_scored else None
        ),
        **_latency_block("baseline", baseline_latencies),
        **_latency_block("candidate", candidate_latencies),
        "latency_ms_avg": (
            round(sum(combined) / len(combined), 3) if combined else None
        ),
        "latency_ms_p50": _percentile(combined, 0.5),
        "latency_ms_p95": _percentile(combined, 0.95),
        # 逻辑调用次数：回放自己发起了多少次"判一条"（= 样本数 × 2）。
        "logical_call_count": logical_calls,
        # 旧字段保留为逻辑调用次数的别名（它从来就不是 provider 尝试次数）。
        "model_call_count": logical_calls,
        "provider_attempt_count": attempts_total if attempts_known else None,
        "provider_attempts_known": attempts_known,
        "token_usage": {
            "prompt_tokens_estimated": prompt_tokens,
            "output_tokens_estimated": output_tokens,
            "source": "estimated",
        },
        # 仓库里没有模型价格表：宁可没有金额，也不要编一个单价出来的假成本。
        "estimated_cost": None,
    }
    # 兼容旧字段名（token 统计的读法不变），但**同时**给出 estimated 版本名。
    summary["token_usage"]["prompt_tokens"] = prompt_tokens
    summary["token_usage"]["output_tokens"] = output_tokens
    return summary


# --------------------------------------------------------------------------- #
# run：创建 / 执行 / 取消
# --------------------------------------------------------------------------- #
async def create_run(
    session: AsyncSession,
    group_id: int,
    *,
    candidate: Any,
    created_by: int = 0,
    idempotency_key: str = "",
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[ModerationReplayRun, bool]:
    """冻结基线 + 登记候选，创建一个 ``queued`` 的回放。

    返回 ``(run, created)``。``created=False`` 表示同一个 ``idempotency_key``
    已经存在且候选摘要一致 —— 调用方应当**复用**那个 run（网络重试、
    双击按钮都会走到这里）。同键但候选摘要不同 → 409
    ``replay_idempotency_conflict``：绝不静默覆盖别人已经登记的提案。

    ``idempotency_key`` 必填（≤ 64 字符）：它是这张表上唯一的业务键，空值会让
    两次无关的回放在唯一索引上撞车。

    **模型绑定在这一刻冻结**（:func:`capture_model_binding`）。执行时不再回
    ``settings`` 现取 —— 否则"这轮结果是谁跑出来的"和配置改没改之间就成了
    竞态。未传 ``settings`` 时绑定为空（``model_digest=""``），结果里会显式
    标出"未知"，而不是假装有绑定。
    """

    gid = int(group_id)
    key = str(idempotency_key or "").strip()
    if not key:
        raise ReplayError(
            "replay_idempotency_key_required",
            "创建回放必须提供 idempotency_key。",
        )
    if len(key) > 64:
        raise ReplayError("replay_idempotency_key_invalid", "idempotency_key 过长。")

    baseline = await freeze_baseline(session, gid, now=now)
    validated = await validate_candidate(
        candidate, baseline_prompt=str(baseline["snapshot"].get("prompt") or "")
    )
    instant = now or now_shanghai_naive()
    await _ensure_group_row(session, gid)
    binding = (
        capture_model_binding(settings) if settings is not None else {}
    )

    existing = await session.scalar(
        select(ModerationReplayRun).where(
            ModerationReplayRun.idempotency_key == key
        )
    )
    if existing is not None:
        if str(existing.candidate_digest or "") != validated["digest"]:
            raise ReplayError(
                "replay_idempotency_conflict",
                "同一个 idempotency_key 已登记过另一份候选。",
                status=409,
            )
        if int(existing.group_id) != gid:
            raise ReplayError(
                "replay_idempotency_conflict",
                "同一个 idempotency_key 已用于另一个群。",
                status=409,
            )
        return existing, False

    run = ModerationReplayRun(
        group_id=gid,
        baseline_version=str(baseline["version"]),
        baseline_digest=str(baseline["digest"]),
        baseline_snapshot=baseline["snapshot"],
        candidate_version=str(validated["version"]),
        candidate_digest=str(validated["digest"]),
        candidate_rules=validated["rules"],
        candidate_prompt=str(validated["prompt"]),
        model_binding=binding,
        model_digest=str(binding.get("digest") or ""),
        fidelity=build_fidelity_block(model_digest=str(binding.get("digest") or "")),
        status=RUN_QUEUED,
        cancel_requested=False,
        lease_owner="",
        lease_expires_at=None,
        attempt=0,
        stats={},
        error="",
        created_by=int(created_by or 0),
        created_at=instant,
        started_at=None,
        finished_at=None,
        idempotency_key=key,
    )
    session.add(run)
    await session.flush()
    return run, True


def _result_row(*, side: str, evaluation: ReplayEvaluation) -> dict[str, Any]:
    """把一次判定摊成结果行的 ``<side>_*`` 列。

    ``usage`` 里刻意区分三件事：``logical_calls``（回放发起了几次逻辑判定）、
    ``provider_attempts``（真实 provider 尝试次数；``LLMService`` 到不了这一层，
    所以默认 ``None``）、``*_tokens_estimated``（估算，不是 provider 账单）。
    """

    return {
        f"{side}_verdict": evaluation.verdict,
        f"{side}_conclusive": bool(evaluation.conclusive),
        f"{side}_confidence": (
            None if evaluation.confidence is None else float(evaluation.confidence)
        ),
        f"{side}_match_source": str(evaluation.match_source or ""),
        f"{side}_latency_ms": int(evaluation.latency_ms),
        f"{side}_error": str(evaluation.error or ERROR_NONE),
        f"{side}_usage": {
            "logical_calls": int(evaluation.model_calls),
            "provider_attempts": None,
            "provider_attempts_known": False,
            "prompt_tokens": int(evaluation.usage.get("prompt_tokens") or 0),
            "output_tokens": int(evaluation.usage.get("output_tokens") or 0),
            "token_source": "estimated",
        },
    }


@dataclass(frozen=True, slots=True)
class ReplayClaim:
    """一次抢占成功后，worker 需要的**纯值**快照（不持有任何 ORM 行）。

    刻意不返回 ORM 对象：worker 的执行期可能横跨多次短事务/多次会话，
    拿着绑定到某个 session 的对象等于把那个 session 的生命周期拖到整个回放。
    """

    run_id: int
    group_id: int
    owner: str
    attempt: int
    baseline_rules: tuple[RuleSnapshot, ...]
    baseline_prompt: str
    candidate_rules: tuple[RuleSnapshot, ...]
    candidate_prompt: str
    model_digest: str


async def claim_run(
    session_factory: Any,
    run_id: int,
    *,
    owner: str,
    lease_seconds: float = REPLAY_LEASE_SECONDS,
    now: datetime | None = None,
) -> ReplayClaim:
    """在**一个极短的写事务**里 CAS 抢占这条 run，然后立刻提交、释放连接。

    抢到的条件只有两种：

    * ``status == 'queued'``（首次执行）；或
    * ``status == 'running'`` 且租约**已过期**（原 worker 死了/卡死了，接管）。

    ``cancelled`` / ``failed`` 永远抢不到：结果已经出来或已被明确撤销，
    "再执行一次"是一次新的提案，不是接管。

    同一个事务里顺手删掉上一次的结果行（重跑不重复写）。**全程没有任何网络
    调用**：事务在返回之前就已经结束，SQLite 写锁不会被模型调用占住。
    """

    instant = now or now_shanghai_naive()
    lease_until = instant + timedelta(seconds=max(1.0, float(lease_seconds)))
    async with session_factory() as session:
        row = await session.get(ModerationReplayRun, int(run_id))
        if row is None:
            raise ReplayError("replay_run_not_found", "回放不存在。", status=404)
        current = str(row.status or "")
        if current == RUN_CANCELLED:
            await session.rollback()
            raise ReplayError("replay_cancelled", "回放已取消，不能执行。", status=409)
        if current == RUN_FAILED:
            await session.rollback()
            raise ReplayError("replay_failed", "回放已失败，不能执行。", status=409)
        if current == RUN_RUNNING and (
            row.lease_expires_at is not None and row.lease_expires_at > instant
        ):
            await session.rollback()
            raise ReplayError("replay_already_running", "回放正在执行中。", status=409)
        # CAS：条件里带上"当前状态 + 租约"，让两个 worker 不可能同时抢到。
        result = await session.execute(
            update(ModerationReplayRun)
            .where(ModerationReplayRun.id == int(run_id))
            .where(
                or_(
                    ModerationReplayRun.status != RUN_RUNNING,
                    (
                        ModerationReplayRun.lease_expires_at.is_(None)
                        | (ModerationReplayRun.lease_expires_at <= instant)
                    ),
                )
            )
            .values(
                status=RUN_RUNNING,
                lease_owner=str(owner or "")[:64],
                lease_expires_at=lease_until,
                started_at=row.started_at or instant,
                finished_at=None,
                error="",
                cancel_requested=False,
                attempt=ModerationReplayRun.attempt + 1,
            )
        )
        if int(result.rowcount or 0) != 1:
            await session.rollback()
            raise ReplayError("replay_already_running", "回放正在执行中。", status=409)
        await session.execute(
            delete(ModerationReplayResult).where(
                ModerationReplayResult.run_id == int(run_id)
            )
        )
        await session.commit()
    claimed = await load_claim(session_factory, run_id, owner=str(owner))
    if claimed is None:
        raise ReplayError("replay_run_not_found", "回放不存在。", status=404)
    return claimed


async def load_claim(
    session_factory: Any, run_id: int, *, owner: str
) -> ReplayClaim | None:
    """读一次 run 行并摊成纯值快照（**只读**会话，立即结束）。"""

    async with session_factory() as session:
        row = await session.get(ModerationReplayRun, int(run_id))
        if row is None:
            return None
        snapshot = dict(row.baseline_snapshot or {})
        baseline_rules = tuple(
            RuleSnapshot.from_dict(item) for item in (snapshot.get("rules") or [])
        )
        baseline_prompt = str(snapshot.get("prompt") or "")
        candidate_rules = tuple(
            RuleSnapshot.from_dict(item) for item in (row.candidate_rules or [])
        )
        candidate_prompt = str(row.candidate_prompt or "") or baseline_prompt
        return ReplayClaim(
            run_id=int(row.id),
            group_id=int(row.group_id),
            owner=str(owner or ""),
            attempt=int(row.attempt or 0),
            baseline_rules=baseline_rules,
            baseline_prompt=baseline_prompt,
            candidate_rules=candidate_rules,
            candidate_prompt=candidate_prompt,
            model_digest=str(row.model_digest or ""),
        )


async def _load_samples(
    session_factory: Any, group_id: int, *, window: int, now: datetime
) -> list[tuple[int, str, str, str, str | None]]:
    """取本轮要评估的样本，返回**纯值元组**（id/正文/上下文/金标/出处）。

    同样刻意不返回 ORM 行：worker 会在整个回放期里跨多个会话。
    """

    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(ModerationReplaySample)
                .where(
                    ModerationReplaySample.group_id == int(group_id),
                    _active_sample_filter(now),
                )
                .order_by(ModerationReplaySample.id)
                .limit(window)
            )
        ).all()
        return [
            (
                int(row.id),
                str(row.redacted_text or ""),
                str(row.redacted_context or ""),
                str(row.gold_label) if row.gold_label else None,
                str(getattr(row, "source_kind", "") or ""),
            )
            for row in rows
        ]


async def _count_redaction_changed(
    session_factory: Any, sample_ids: Sequence[int]
) -> int:
    ids = [int(value) for value in sample_ids]
    if not ids:
        return 0
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(ModerationReplaySample.provenance).where(
                    ModerationReplaySample.id.in_(ids)
                )
            )
        ).all()
    return sum(
        1 for provenance in rows if (provenance or {}).get("redaction_applied")
    )


async def _write_result_row(
    session_factory: Any,
    *,
    run_id: int,
    group_id: int,
    sample_id: int,
    baseline: ReplayEvaluation,
    candidate: ReplayEvaluation,
    owner: str,
    lease_until: datetime,
    now: datetime,
) -> str:
    """写一条结果 + 续租，**一个极短的写事务**。

    返回三态，因为"丢了"有两种**必须区别对待**的成因：

    * ``"kept"`` —— 正常写入并续租；
    * ``"cancelled"`` —— 租约没了是因为**管理员取消了**这一轮：终态已经由取消
      写好了，我们仍然要把已经算出来的统计如实落库（只是不许改状态）；
    * ``"lost"`` —— 租约被别人接走了：我们必须彻底闭嘴，**绝不**写结果，
      更不许收尾覆盖接管者的终态。
    """

    async with session_factory() as session:
        renewed = await session.execute(
            update(ModerationReplayRun)
            .where(ModerationReplayRun.id == int(run_id))
            .where(
                ModerationReplayRun.status == RUN_RUNNING,
                ModerationReplayRun.lease_owner == str(owner or ""),
            )
            .values(lease_expires_at=lease_until)
        )
        if int(renewed.rowcount or 0) != 1:
            current = await session.scalar(
                select(ModerationReplayRun.status).where(
                    ModerationReplayRun.id == int(run_id)
                )
            )
            await session.rollback()
            return "cancelled" if str(current or "") == RUN_CANCELLED else "lost"
        session.add(
            ModerationReplayResult(
                run_id=int(run_id),
                group_id=int(group_id),
                sample_id=int(sample_id),
                **_result_row(side="baseline", evaluation=baseline),
                **_result_row(side="candidate", evaluation=candidate),
                changed=baseline.verdict != candidate.verdict,
            )
        )
        await session.commit()
    return "kept"


async def _cancel_requested(session_factory: Any, run_id: int) -> bool:
    """**独立读会话**查取消标志 —— 正是"不持锁"让这条查询随时能成功。"""

    async with session_factory() as session:
        row = await session.get(ModerationReplayRun, int(run_id))
        if row is None:
            return True
        return bool(getattr(row, "cancel_requested", False)) or (
            str(row.status or "") == RUN_CANCELLED
        )


async def _finalize_run(
    session_factory: Any,
    run_id: int,
    *,
    owner: str,
    status: str,
    stats: dict[str, Any],
    error: str,
    fidelity: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    """收尾：**CAS** 写终态。返回 ``False`` = 终态已被别人写走，我们没覆盖它。

    ``WHERE status='running' AND lease_owner=:owner`` 这两个条件就是"晚到的
    worker 不许覆盖 cancelled"的全部实现：取消已经把状态改成 ``cancelled``，
    这里的 UPDATE 就一行都改不动。
    """

    instant = now or now_shanghai_naive()
    async with session_factory() as session:
        # 统计**单独**先落一次：取消先落地时，终态 CAS 会失败，但"已经算出来的那
        # 几行"仍然必须如实报出来 —— 否则管理员取消一次，报告上的数字就永远停在
        # 上一轮（或者空）。写入条件是"租约仍在我们手上"**或**"这条已经被取消"，
        # 被别人接管（running + 另一个 owner）的行一条都动不了。
        await session.execute(
            update(ModerationReplayRun)
            .where(ModerationReplayRun.id == int(run_id))
            .where(
                or_(
                    ModerationReplayRun.lease_owner == str(owner or ""),
                    ModerationReplayRun.status == RUN_CANCELLED,
                )
            )
            .values(
                stats=dict(stats or {}),
                **({"fidelity": dict(fidelity)} if fidelity is not None else {}),
            )
        )
        result = await session.execute(
            update(ModerationReplayRun)
            .where(ModerationReplayRun.id == int(run_id))
            .where(
                ModerationReplayRun.status == RUN_RUNNING,
                ModerationReplayRun.lease_owner == str(owner or ""),
            )
            .values(
                status=str(status),
                error=str(error or ""),
                finished_at=instant,
                lease_owner="",
                lease_expires_at=None,
            )
        )
        changed = int(result.rowcount or 0)
        await session.commit()
    return bool(changed)


async def _fail_run(
    session_factory: Any,
    run_id: int,
    *,
    owner: str,
    status: str = RUN_FAILED,
    error: str = RUN_ERROR_FAILED,
) -> None:
    """在**独立事务**里写终态，然后**不再向上抛异常内容**。

    失败状态必须独立于"这次执行"的任何事务落库：直接 raise 的话，上下文管理器
    会把状态一起回滚，这条 run 就永远停在 ``running``（重启后看起来像还在跑）。
    """

    try:
        await _finalize_run(
            session_factory,
            run_id,
            owner=owner,
            status=status,
            stats={},
            error=str(error or ""),
        )
    except Exception:  # pragma: no cover - 连收尾都失败时只能放弃写库
        log.warning("replay: 无法写入终态 status=%s error=%s", status, error)


async def _finalize_orphaned_run(session_factory: Any, run_id: int) -> bool:
    """把一条仍是 ``running`` 的 run 收成 ``interrupted``（CAS，终态不受影响）。

    关停时用：worker 被 ``cancel()`` 之后不会自己收尾（``CancelledError`` 不是
    ``Exception``），于是这条 run 会一直 ``running`` 到租约过期为止。留一个
    "永远 running" 的报表行，比诚实地写成 ``interrupted`` 差得多 —— 后者告诉
    管理员"这轮没跑完，重跑即可"。

    只在 ``status == running`` 时才写：已经收成 ``cancelled``/``completed`` 的
    一律不动（不覆盖别人写的终态）。返回是否真的改了。
    """

    instant = now_shanghai_naive()
    async with session_factory() as session:
        result = await session.execute(
            update(ModerationReplayRun)
            .where(
                ModerationReplayRun.id == int(run_id),
                ModerationReplayRun.status == RUN_RUNNING,
            )
            .values(
                status=RUN_INTERRUPTED,
                error=RUN_ERROR_INTERRUPTED,
                finished_at=instant,
                lease_owner="",
                lease_expires_at=None,
            )
        )
        changed = int(result.rowcount or 0)
        await session.commit()
    return bool(changed)


async def sweep_interrupted_runs(
    session_factory: Any,
    *,
    now: datetime | None = None,
    group_id: int | None = None,
) -> int:
    """把"没人收尾的 running"扫成 ``interrupted``，返回处理行数。

    **什么时候跑**：应用启动那一次（``app.on_startup`` → ``ReplayExecutor
    .ensure_ready``，也就是"上一轮进程留下的 running"），以及每
    :data:`REPLAY_MAINTENANCE_INTERVAL_SECONDS` 秒一次的 :class:`ReplayMaintenance`
    （覆盖"进程还活着、但 worker 崩了"的情形）。判据是**租约过期**，不是"启动
    时间早于进程"：只靠后者没法处理"上一轮 worker 卡死后进程又活了"的情况，
    而那正是需要回收的情况。

    关键约束：它**必须**在**没有任何外层写事务**的时候跑。它用的是自己的会话；
    调用方若正持有 SQLite 写锁（同一个进程的另一条写），两条写会互相等满
    ``busy_timeout`` 然后抛错。

    为什么是 ``interrupted`` 而不是 ``failed``：``failed`` 是这一轮自己知道出了
    什么事，``interrupted`` 是"有人启动了但没人回来"。两者的下一步动作不同
    （前者看错误，后者重跑即可），UI 上必须说得不一样。
    """

    instant = now or now_shanghai_naive()
    conditions = [
        ModerationReplayRun.status == RUN_RUNNING,
        or_(
            ModerationReplayRun.lease_expires_at.is_(None),
            ModerationReplayRun.lease_expires_at <= instant,
        ),
    ]
    if group_id is not None:
        conditions.append(ModerationReplayRun.group_id == int(group_id))
    async with session_factory() as session:
        stale = list(
            (
                await session.scalars(
                    select(ModerationReplayRun.id).where(*conditions)
                )
            ).all()
        )
        if not stale:
            await session.rollback()
            return 0
        await session.execute(
            update(ModerationReplayRun)
            .where(ModerationReplayRun.id.in_([int(value) for value in stale]))
            .where(ModerationReplayRun.status == RUN_RUNNING)
            .values(
                status=RUN_INTERRUPTED,
                error=RUN_ERROR_INTERRUPTED,
                finished_at=instant,
                lease_owner="",
                lease_expires_at=None,
            )
        )
        await session.commit()
    log.warning("replay: 回了 %d 条中断的回放（租约过期）", len(stale))
    return len(stale)


async def run_maintenance_pass(
    session_factory: Any,
    *,
    sample_batch: int = REPLAY_MAINTENANCE_SAMPLE_BATCH,
    group_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """一次**有界**的维护扫描，返回 ``{"interrupted": n, "purged": n}``。

    两件事，**各自一个极短的独立事务**（不是一笔横跨全部工作的大事务）：

    1. :func:`sweep_interrupted_runs` —— 回收租约过期的 ``running``（worker 崩了
       而进程还活着的情形；进程重启的那一轮由应用启动时的那次扫描负责）。
    2. 按 TTL 删过期样本**正文**，连带删它们的结果行并**重算**受影响 run 的统计
       —— 于是报表里永远不会出现一个与现存结果行对不上的数字。

    有界性是硬要求：SQLite 的写锁是**全局**的，一次"全表扫 + 全删"能把面板的
    并发写请求整片卡住直到 ``busy_timeout``。所以样本删除一次最多
    ``sample_batch`` 条；剩下的留给下一次 pass。

    失败**可重试**：任何一步抛错都不写半截状态（各自独立事务），下一轮原样再来。
    """
    interrupted = await sweep_interrupted_runs(session_factory, group_id=group_id)
    async with session_factory() as session:
        purged = await purge_expired_samples(
            session, group_id=group_id, now=now, limit=max(0, int(sample_batch))
        )
        await session.commit()
    if purged:
        log.info("replay: TTL 清理了 %d 条过期样本", purged)
    return {"interrupted": int(interrupted), "purged": int(purged)}


class ReplayMaintenance:
    """回放的**有界周期维护任务**：TTL 清理 + 中断回收。

    为什么必须有它：``purge_expired_samples`` 只是把到期样本从新回放里排除，
    **不等于删掉正文**。没有周期性地真的删，样本正文就会一直躺在库里 —— 那正是
    这张表最不该长期持有的东西（别人的发言，哪怕已脱敏）。

    三条硬约束：

    * **有界**：每一轮都是 :func:`run_maintenance_pass`，删样本数一次不超过
      ``batch_size``，且每件事一个独立短事务；绝不做无界全表工作。
    * **可重试**：一轮失败只记一个稳定错误码并等下一轮，绝不把任务自己停掉。
    * **不泄密**：日志里只有计数与稳定码，没有异常原文、路径或配置。

    周期用**真**计时器：这是要真跑的维护任务，"失败之后等多久再来"也是它契约
    的一部分，用假时钟测等于没测。
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        interval_seconds: float = REPLAY_MAINTENANCE_INTERVAL_SECONDS,
        batch_size: int = REPLAY_MAINTENANCE_SAMPLE_BATCH,
    ) -> None:
        self._session_factory = session_factory
        self._interval = max(0.0, float(interval_seconds))
        self._batch_size = max(0, int(batch_size))
        self._task: asyncio.Task[Any] | None = None
        self._stopping = asyncio.Event()
        self.passes = 0
        self.failures = 0
        self.interrupted_total = 0
        self.purged_total = 0
        self.last_error = ""

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_once(self) -> dict[str, int]:
        """跑一轮并把计数累计进去；失败只记稳定码，绝不抛给调用方。"""

        try:
            outcome = await run_maintenance_pass(
                self._session_factory, sample_batch=self._batch_size
            )
        except Exception:  # noqa: BLE001 - 维护失败绝不能带出异常原文
            self.failures += 1
            self.last_error = "maintenance_pass_failed"
            # 异常文本/traceback 可能带着 provider 原文、路径或配置，绝不进日志。
            log.warning("replay: 维护轮次失败 error=%s", self.last_error)
            return {}
        self.passes += 1
        self.last_error = ""
        self.interrupted_total += int(outcome.get("interrupted") or 0)
        self.purged_total += int(outcome.get("purged") or 0)
        return outcome

    def start(self) -> None:
        """拉起周期任务（幂等）。"""

        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="replay-maintenance")

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=max(0.01, self._interval)
                )
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            if self._stopping.is_set():
                break
            await self.run_once()

    async def stop(self) -> None:
        """停掉周期任务并等它真的退出（不留一个还在用数据库的后台任务）。"""

        self._stopping.set()
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关停不追究原因
            pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "interval_seconds": self._interval,
            "sample_batch": self._batch_size,
            "passes": self.passes,
            "failures": self.failures,
            "interrupted_total": self.interrupted_total,
            "purged_total": self.purged_total,
            "last_error": self.last_error,
        }


async def run_replay(
    session_factory: Any,
    run_id: int,
    *,
    llm: Any,
    owner: str = "",
    sample_limit: int | None = REPLAY_RUN_SAMPLE_LIMIT,
    timeout_seconds: float = REPLAY_MODEL_TIMEOUT_SECONDS,
    deadline_seconds: float | None = REPLAY_RUN_DEADLINE_SECONDS,
    lease_seconds: float = REPLAY_LEASE_SECONDS,
    clock: Callable[[], float] = time.perf_counter,
    now: datetime | None = None,
    model_binding: dict[str, Any] | None = None,
) -> ModerationReplayRun:
    """执行一轮回放：每条样本各跑**基线与候选**两次判定。

    **事务边界（本函数存在的全部理由）**

    1. :func:`claim_run`：一个极短的写事务做 CAS 抢占 + 清旧结果，立刻提交并
       **释放连接**。
    2. 模型调用期间**不持有任何写锁**、也不持有任何数据库连接。
    3. 每条样本的结果 + 续租：各一个极短的写事务。
    4. 收尾：一个 CAS 写终态的事务。

    于是：独立读者随时能看到 ``running``；取消不会被写锁挡住；第二个 worker
    会因为租约未过期而干净失败；worker 崩了留下的 ``running`` 会被扫成
    ``interrupted``。

    ``session_factory`` 必须是 ``async_sessionmaker``（不是一个 session）：本函数
    需要在执行期多次开关**独立**会话，才能让"不持锁"这件事真的成立。

    * ``changed = 基线判定 != 候选判定``（三值比较，见 :func:`compute_summary`）。
    * **只写** ``moderation_replay_results`` 与本行的 ``status``/``stats``。不删
      消息、不禁言/封禁、不扣分、不发审核卡、不写 ``violations``/``user_warnings``、
      不碰审核调用预算与送审整形闸。
    * 样本按 id 升序取前 ``sample_limit`` 条（缺省 ``REPLAY_RUN_SAMPLE_LIMIT``）；
      ``stats.total`` 是**实际评估条数**，与样本池大小不一致时以它为准。
    * ``deadline_seconds`` 是**墙钟预算**（``None`` = 不限）：超出即停止，状态置
      ``cancelled`` 且 ``error="deadline_exceeded"``，已算出的结果行保留。
    * **取消**：worker 在**两次模型调用之间**检查取消标志，停下后收尾成
      ``cancelled``；如果取消先落地，收尾的 CAS 会失败，``cancelled`` 不会被覆盖。
    """

    run_token = str(owner or "") or f"replay-{uuid.uuid4().hex[:16]}"
    instant = now or now_shanghai_naive()
    window = max(
        1,
        min(
            int(sample_limit or 0) or REPLAY_RUN_SAMPLE_LIMIT,
            MAX_SAMPLES_PER_RUN,
        ),
    )

    claim = await claim_run(
        session_factory,
        int(run_id),
        owner=run_token,
        lease_seconds=lease_seconds,
        now=instant,
    )
    samples = await _load_samples(
        session_factory, claim.group_id, window=window, now=instant
    )
    if not samples:
        # 没有样本就别"假装跑过"：把状态收回终态，错误码稳定可读。
        await _fail_run(
            session_factory,
            int(run_id),
            owner=run_token,
            status=RUN_FAILED,
            error="replay_empty_sample_set",
        )
        raise ReplayError("replay_empty_sample_set", "没有可用于回放的样本。")

    gold_labels: dict[int, str | None] = {}
    deadline_at = (
        None if deadline_seconds is None else time.monotonic() + float(deadline_seconds)
    )
    stop_reason = ""
    lease_until = instant + timedelta(seconds=max(1.0, float(lease_seconds)))

    try:
        for sample_id, text, context, gold_label, _source_kind in samples:
            if deadline_at is not None and time.monotonic() >= deadline_at:
                # 墙钟预算用尽：停下并如实标记，别把"只跑了一半"伪装成 completed。
                stop_reason = RUN_ERROR_DEADLINE
                break
            if await _cancel_requested(session_factory, int(run_id)):
                stop_reason = RUN_ERROR_CANCELLED
                break
            with execution_priority_scope(ExecutionPriority.BACKGROUND), usage_scope.lenient_scope(
                # 回放的模型调用显式归属到**本回放所属的群**，且是后台可选用途：
                # 不绑的话它会继承触发这次回放的那位管理员的上下文，把这个群的
                # 回放成本记到别人的私聊名下（后台调度级别 ≠ 预算用途，两者分开）。
                kind=usage_scope.SCOPE_GROUP,
                group_id=int(claim.group_id),
                purpose=usage_scope.PURPOSE_BACKGROUND_OPTIONAL,
                stage="replay",
            ):
                # 回放的模型调用跑在 BACKGROUND 优先级上：它只会占用
                # ``_LLM_PRIORITY_GATE`` 的**背景名额**（≤2，且与普通回复共享
                # normal 上限），永远吃不到为审核/权限变更保留的 HIGH/CRITICAL 额度。
                # 也就是说回放再忙也不会把活链路的审核挤下队。
                baseline_eval = await evaluate_snapshot(
                    claim.baseline_rules,
                    claim.baseline_prompt,
                    text,
                    context,
                    llm=llm,
                    timeout_seconds=timeout_seconds,
                    clock=clock,
                )
                # 两次模型调用之间再查一次取消：一条样本两次调用之间正是最容易
                # 被管理员按"取消"的窗口。
                if await _cancel_requested(session_factory, int(run_id)):
                    stop_reason = RUN_ERROR_CANCELLED
                    break
                candidate_eval = await evaluate_snapshot(
                    claim.candidate_rules,
                    claim.candidate_prompt,
                    text,
                    context,
                    llm=llm,
                    timeout_seconds=timeout_seconds,
                    clock=clock,
                )
            lease_until = now_shanghai_naive() + timedelta(
                seconds=max(1.0, float(lease_seconds))
            )
            outcome = await _write_result_row(
                session_factory,
                run_id=int(run_id),
                group_id=claim.group_id,
                sample_id=int(sample_id),
                baseline=baseline_eval,
                candidate=candidate_eval,
                owner=run_token,
                lease_until=lease_until,
                now=now_shanghai_naive(),
            )
            if outcome == "cancelled":
                # 管理员在两次调用之间取消了：这一行的模型调用已经发生，但按
                # 约定它**不落库**（否则会凭空多出一行"已评估"）。停，收尾。
                stop_reason = RUN_ERROR_CANCELLED
                break
            if outcome != "kept":
                # 租约被别人接走了：立刻闭嘴，连统计都不许覆盖接管者写的。
                stop_reason = RUN_ERROR_LEASE_LOST
                break
            gold_labels[int(sample_id)] = gold_label
    except Exception:  # noqa: BLE001 - 失败必须落成稳定码，且不得带出原文
        await _fail_run(
            session_factory, int(run_id), owner=run_token, status=RUN_FAILED
        )
        # 只记稳定码 + run id：异常文本/traceback 可能带着 provider 原文、URL、
        # key 或用户发言，**绝不能**进日志、响应或历史。
        log.warning("replay: run 执行失败 error=%s run=%s", RUN_ERROR_FAILED, run_id)
        raise

    # 统计按**真正写入库的行**算：cancel/deadline 中断时，中断那条的模型调用
    # 已经发生但结果没落库，把它算进去就是凭空多出一行。
    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(ModerationReplayResult)
                    .where(ModerationReplayResult.run_id == int(run_id))
                    .order_by(ModerationReplayResult.id)
                )
            ).all()
        )
    stats = compute_summary(rows, gold_labels)
    redacted_changed = await _count_redaction_changed(
        session_factory, [int(row.sample_id) for row in rows]
    )
    executed_digest = str((model_binding or {}).get("digest") or "")
    fidelity = build_fidelity_block(
        model_digest=claim.model_digest,
        samples_evaluated=len(rows),
        samples_redaction_changed=redacted_changed,
        executed_model_digest=executed_digest,
    )
    if stop_reason == RUN_ERROR_LEASE_LOST:
        # 终态已经由接管者/取消方写好了，我们什么都不用做（也不许覆盖）。
        log.info("replay: 租约已易主，本 worker 不再收尾 run=%s", run_id)
        return await get_run_any(session_factory, int(run_id))
    final_status = RUN_CANCELLED if stop_reason else RUN_COMPLETED
    await _finalize_run(
        session_factory,
        int(run_id),
        owner=run_token,
        status=final_status,
        stats=stats,
        error=str(stop_reason or ""),
        fidelity=fidelity,
    )
    finished = await get_run_any(session_factory, int(run_id))
    if finished is None:
        raise ReplayError("replay_run_not_found", "回放不存在。", status=404)
    log.info(
        "replay finished | run=%s group=%s status=%s evaluated=%s",
        run_id,
        claim.group_id,
        finished.status,
        len(rows),
    )
    return finished


async def get_run_any(session_factory: Any, run_id: int) -> ModerationReplayRun | None:
    """按 id 读一条 run（**不校验群归属**，只给服务层内部用）。"""

    async with session_factory() as session:
        row = await session.get(ModerationReplayRun, int(run_id))
        if row is None:
            return None
        session.expunge(row)
        return row


async def cancel_run(
    session: AsyncSession, run_id: int, *, now: datetime | None = None
) -> ModerationReplayRun:
    """取消一个还没跑完的回放。

    这条路径**必须**能在 worker 正在跑的时候立刻写成功，所以它自己就是一个
    极短的写事务（调用方只 flush/commit 自己的 session，绝不与 worker 的写锁
    争用）。

    语义：置 ``status='cancelled'`` + ``cancel_requested=True``。worker 在两次
    模型调用之间看到 ``cancel_requested`` 就停下并收尾成 ``cancelled``；如果它
    收尾时状态已经被取消改掉了，它的 CAS 会失败，**不会**把 ``cancelled`` 覆盖成
    ``completed``。

    已完成/已失败/已中断的 run 不可取消（409）：报告已经出来了，"取消"只会让
    管理员以为自己撤回了结果。对已取消的 run 重复调用是幂等的。
    """

    run = await session.get(ModerationReplayRun, int(run_id))
    if run is None:
        raise ReplayError("replay_run_not_found", "回放不存在。", status=404)
    if run.status in (RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED):
        raise ReplayError(
            "replay_not_cancellable", "回放已经结束，无法取消。", status=409
        )
    if run.status != RUN_CANCELLED:
        run.status = RUN_CANCELLED
        run.cancel_requested = True
        # 取消的原因码在这里写一次：谁先落地谁负责说清"为什么停"。
        run.error = RUN_ERROR_CANCELLED
        run.finished_at = now or now_shanghai_naive()
        run.lease_owner = ""
        run.lease_expires_at = None
        await session.flush()
    return run


async def list_runs(
    session: AsyncSession,
    group_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[ModerationReplayRun]:
    return list(
        (
            await session.scalars(
                select(ModerationReplayRun)
                .where(ModerationReplayRun.group_id == int(group_id))
                .order_by(ModerationReplayRun.id.desc())
                .offset(max(0, int(offset)))
                .limit(max(1, min(int(limit or 0) or 50, 200)))
            )
        ).all()
    )


async def get_run(
    session: AsyncSession, group_id: int, run_id: int
) -> ModerationReplayRun:
    run = await session.get(ModerationReplayRun, int(run_id))
    if run is None or int(run.group_id) != int(group_id):
        raise ReplayError("replay_run_not_found", "回放不存在。", status=404)
    return run


async def list_results(
    session: AsyncSession, run_id: int, *, limit: int = 200, offset: int = 0
) -> list[ModerationReplayResult]:
    return list(
        (
            await session.scalars(
                select(ModerationReplayResult)
                .where(ModerationReplayResult.run_id == int(run_id))
                .order_by(ModerationReplayResult.id)
                .offset(max(0, int(offset)))
                .limit(max(1, min(int(limit or 0) or 200, 500)))
            )
        ).all()
    )


# --------------------------------------------------------------------------- #
# 模型构造（与 ``bot/services/patrol.py`` 同一口径：按需构造，不常驻）
# --------------------------------------------------------------------------- #
def build_replay_llm(settings: Settings) -> LLMService:
    """按当前配置构造一个 ``LLMService`` 供回放调用。

    与 ``patrol._build_llm`` 完全同一套构造参数：回放用的是**审核模型**
    （``llm.moderation``），和线上群消息路径同型号 —— 拿别的模型回放，
    比出来的差异里就混进了"换了个模型"这件事。
    """

    return LLMService(
        settings.bot.main_model,
        settings.bot.decision_model,
        settings.bot.compress_model,
        moderation=settings.bot.moderation_model,
        vision=settings.bot.vision_model,
        embed=settings.bot.embed_model,
        max_context_tokens=settings.bot.max_context_tokens,
        context_window_mode=getattr(settings.bot, "context_window_mode", None),
        business_context_tokens=getattr(settings.bot, "context_budget_tokens", None),
        context_reserve_tokens=getattr(settings.bot, "context_reserve_tokens", None),
    )


# --------------------------------------------------------------------------- #
# 后台执行器：有界、异步、可轮询、可取消
# --------------------------------------------------------------------------- #
class _CapacityReservation:
    """一张"后台名额"的票（:meth:`ReplayExecutor.reserve` 发的）。

    只有一个 :meth:`ReplayExecutor.submit` 带它调用能消费；没消费就
    :meth:`release`。两件事都幂等，所以调用方在 ``try/finally`` 里无条件归还
    也不会把名额多还一次。
    """

    __slots__ = ("_executor", "group_id", "_consumed", "_released")

    def __init__(self, executor: "ReplayExecutor", group_id: int) -> None:
        self._executor = executor
        self.group_id = int(group_id)
        self._consumed = False
        self._released = False

    @property
    def consumed(self) -> bool:
        return self._consumed

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        """没被消费就归还（幂等）。"""

        self._executor.release_reservation(self)

    def __enter__(self) -> "_CapacityReservation":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.release()
        return False


class ReplayExecutor:
    """把回放从 HTTP 请求里搬出去，并且**有界**。

    为什么需要它

    * Mini App 的 initData 只有 10 分钟窗口：一条无界的回放只会得到一个没有解释的
      504，而管理员不知道它跑到哪儿了。
    * 取消必须**真的能取消**。同步跑在请求里的回放没法被另一个请求打断。

    四条硬约束

    * **有界**：全局 ``max_concurrency`` + 单群 ``max_concurrent_per_group`` +
      排队 ``max_pending``。超了就是稳定的 ``replay_busy``（429），而不是安静地
      堆成一山跑不完的任务。名额用 :meth:`reserve` **预留**、由 :meth:`submit`
      消费，于是"检查容量"与"提交"之间没有 ``await``，也就没有"检查通过、提交
      时被抢走、run 永远留在 queued"的窗口。
    * **绝不进活链路**：worker 里所有模型调用都跑在 ``BACKGROUND`` 优先级上，
      只会占用为离线任务预留的额度，审核/权限变更的保留额度一个都碰不到。
    * **可观测/可回收**：启动那一次 ``ensure_ready`` 回收"上一轮进程留下的
      running"，之后由 :class:`ReplayMaintenance` 周期回收"worker 崩了"的；
      :meth:`shutdown` 在服务停下时把在飞的每一轮收成终态。
    * **回收绝不在一个已经持有写锁的请求里跑**：那正是它曾经卡满
      ``busy_timeout`` 的原因（见 :meth:`ensure_ready`）。

    ``settings_provider`` 是一个零参可调用对象（而不是把 ``Settings`` 抓在手里），
    因为运行时配置是可以热改的：抓在手里就等于把"建 run 时的模型绑定"和"执行时
    的模型"混成同一个东西，正好是这次要消灭的漂移。
    """

    def __init__(
        self,
        session_factory: Any,
        settings_provider: Callable[[], Settings],
        *,
        max_concurrency: int = REPLAY_MAX_CONCURRENCY,
        max_concurrent_per_group: int = REPLAY_MAX_CONCURRENCY_PER_GROUP,
        max_pending: int = REPLAY_MAX_PENDING,
        llm_factory: Callable[[Settings], Any] | None = None,
        sample_limit: int | None = None,
        deadline_seconds: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings_provider = settings_provider
        # 刻意**不**在构造时绑定 ``build_replay_llm``：调用时才去取模块属性。
        # 绑死的话，运行时配置（以及测试替身）替换掉它时会对后台执行器完全
        # 无效 —— 而那正是“回放用的是哪个模型”唯一能观察的地方。
        self._llm_factory = llm_factory
        self._max_concurrency = max(1, int(max_concurrency))
        self._max_per_group = max(1, int(max_concurrent_per_group))
        self._max_pending = max(1, int(max_pending))
        # 同 ``llm_factory``：界**不在构造时**绑死到常量上，而是在提交那一刻从
        # 模块属性现取。否则"上界写在模块里，执行器却在启动时抄了一份"就会让改
        # 常量对已启动的进程无效 —— 正是本轮审计点名的那类回归。
        self._sample_limit = sample_limit
        self._deadline_seconds = deadline_seconds
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._group_locks: dict[int, asyncio.Semaphore] = {}
        self._inflight: dict[int, asyncio.Task[Any]] = {}
        self._inflight_groups: dict[int, int] = {}
        self._pending = 0
        self._ready = False
        # 并发的首次初始化只允许**一个**扫描在跑：没有它，两个同时到达的请求
        # 会各自去 ``UPDATE``，其中一个必然等锁超时（同一进程的 SQLite 写锁）。
        self._ready_lock = asyncio.Lock()
        self._reserved_groups: dict[int, int] = {}
        self._closed = False

    # -- 容量 ------------------------------------------------------------- #
    def capacity_snapshot(self) -> dict[str, Any]:
        return {
            "running": len(self._inflight),
            "pending": int(self._pending),
            "reserved": int(sum(self._reserved_groups.values())),
            "max_concurrency": self._max_concurrency,
            "max_concurrency_per_group": self._max_per_group,
            "max_pending": self._max_pending,
            "running_groups": sorted(int(key) for key in self._inflight_groups),
        }

    def has_capacity(self, group_id: int) -> bool:
        if len(self._inflight) >= self._max_concurrency:
            return False
        if self._pending >= self._max_pending:
            return False
        active = int(self._inflight_groups.get(int(group_id), 0))
        return active < self._max_per_group

    def reserve(self, group_id: int) -> "_CapacityReservation | None":
        """**同步**地预留一个后台名额；没有名额立刻返回 ``None``（不排队等待）。

        为什么需要"预留"而不是"检查后再提交"：``has_capacity()`` 与
        ``submit()`` 之间只要隔着一次 ``await``（提交前必须先把 DB 事务提交掉），
        另一个并发请求就能把刚看好的名额抢走，于是"检查通过"的请求在提交时拿到
        ``False`` —— 那正是 ``POST /runs`` 留下孤儿 ``queued`` run 的窗口。
        预留把"看名额"和"占名额"合成一次**不 await** 的原子步骤：预留里已经
        记了一个 ``pending``，后续 :meth:`submit` 直接消费它，不再二次判定。

        预必须显式归还（:class:`_CapacityReservation.release`，幂等）：请求在
        预留与提交之间失败（回滚、异常）时，名额要还给池子，不能凭空少一个。
        """

        gid = int(group_id)
        if self._closed or not self.has_capacity(gid):
            return None
        self._pending += 1
        self._reserved_groups[gid] = int(self._reserved_groups.get(gid, 0)) + 1
        return _CapacityReservation(self, gid)

    def _consume_reservation(self, reservation: "_CapacityReservation") -> None:
        """把一个预留转成真正的排队：只去掉"预留计数"，``pending`` 保持不变。"""

        gid = reservation.group_id
        left = int(self._reserved_groups.get(gid, 0)) - 1
        if left <= 0:
            self._reserved_groups.pop(gid, None)
        else:
            self._reserved_groups[gid] = left
        reservation._consumed = True

    def release_reservation(self, reservation: "_CapacityReservation") -> None:
        """归还一个没被消费的预留（幂等）。"""

        if reservation._consumed or reservation._released:
            return
        reservation._released = True
        gid = reservation.group_id
        self._pending = max(0, self._pending - 1)
        left = int(self._reserved_groups.get(gid, 0)) - 1
        if left <= 0:
            self._reserved_groups.pop(gid, None)
        else:
            self._reserved_groups[gid] = left

    # -- 启动回收 ---------------------------------------------------------- #
    async def ensure_ready(self) -> bool:
        """回收"没人收尾的 running"（= 进程重启后的启动扫描）；成功返回 ``True``。

        **必须在任何外层写事务之外调用。** 这条约束是硬红线：本方法用**另一个**
        会话做 ``UPDATE``，如果调用方此刻正持有 SQLite 写锁（``POST /runs`` 在
        外层 session 里 flush 了新 run 就会这样），两条写会互相等满
        ``busy_timeout``（5s）然后抛错。真实路径上它在 ``app.on_startup`` 里跑
        （见 ``bot/web/settings_api.py``），请求路径只作为"启动钩子没跑"的兜底，
        且兜底调用也一律排在写事务**之前**。

        ``_ready`` **只在成功后**置位：老实现是"先置 True 再扫、失败被 except
        吞掉"，于是扫描永远不会被重试，上一轮崩掉的 run 会永远停在 ``running``。
        并发的首次初始化由 ``_ready_lock`` 串起来：否则两个同时到达的请求各扫
        一次，其中一个必然等锁超时。

        失败只记稳定码：异常原文可能带 provider 原文 / 路径 / 配置，绝不进日志。
        """

        if self._ready:
            return True
        async with self._ready_lock:
            if self._ready:
                return True
            try:
                await sweep_interrupted_runs(self._session_factory)
            except Exception:  # noqa: BLE001 - 失败可重试，且不得带出异常原文
                log.warning("replay: 启动扫描中断回放失败 error=startup_scan_failed")
                return False
            self._ready = True
            return True

    # -- 提交 ------------------------------------------------------------- #
    async def submit(
        self,
        run_id: int,
        *,
        group_id: int,
        reservation: "_CapacityReservation | None" = None,
    ) -> bool:
        """把一轮回放排到后台；没有容量时**立即**返回 ``False``。

        调用方负责把 ``False`` 翻成稳定的 ``replay_busy``（429）。这里刻意不排队
        等待：一等一个 HTTP 请求去等一个后台名额，等于把"有界"又变回"无界"。

        传了 ``reservation``（:meth:`reserve` 的结果）就**直接消费**它，不再重新
        判定容量 —— 于是"容量检查 → 提交"这一段没有 await，也就没有被别人抢走
        名额再把 run 丢成孤儿 ``queued`` 的窗口。
        """

        if reservation is not None:
            self._consume_reservation(reservation)
        else:
            await self.ensure_ready()
            if self._closed or not self.has_capacity(group_id):
                return False
            self._pending += 1
        run_key = int(run_id)
        if run_key in self._inflight:
            # 已经在跑：不重复排。刚占的那一个名额还给池子。
            if reservation is not None:
                self._pending = max(0, self._pending - 1)
            return True
        try:
            task = asyncio.create_task(
                self._worker(run_key, int(group_id)), name=f"replay-run-{run_key}"
            )
        except BaseException:
            self._pending = max(0, self._pending - 1)
            raise
        self._inflight[run_key] = task
        self._inflight_groups[int(group_id)] = (
            int(self._inflight_groups.get(int(group_id), 0)) + 1
        )
        task.add_done_callback(lambda _t: self._forget(run_key, int(group_id)))
        return True

    def _forget(self, run_id: int, group_id: int) -> None:
        self._inflight.pop(int(run_id), None)
        left = int(self._inflight_groups.get(int(group_id), 0)) - 1
        if left <= 0:
            self._inflight_groups.pop(int(group_id), None)
        else:
            self._inflight_groups[int(group_id)] = left

    async def _worker(self, run_id: int, group_id: int) -> None:
        async with self._semaphore:
            self._pending = max(0, self._pending - 1)
            try:
                await self._execute(run_id)
            except asyncio.CancelledError:  # pragma: no cover - 关停时的正常路径
                raise
            except Exception:  # noqa: BLE001 - run_replay 已落终态，这里只记稳定码
                log.warning(
                    "replay: 后台执行异常 error=%s run=%s", RUN_ERROR_FAILED, run_id
                )

    async def _execute(self, run_id: int) -> None:
        settings = self._settings_provider()
        binding = capture_model_binding(settings)
        llm = (self._llm_factory or build_replay_llm)(settings)
        sample_limit = (
            REPLAY_RUN_SAMPLE_LIMIT
            if self._sample_limit is None
            else int(self._sample_limit)
        )
        deadline_seconds = (
            REPLAY_RUN_DEADLINE_SECONDS
            if self._deadline_seconds is None
            else float(self._deadline_seconds)
        )
        await run_replay(
            self._session_factory,
            run_id,
            llm=llm,
            sample_limit=sample_limit,
            deadline_seconds=deadline_seconds,
            model_binding=binding,
        )

    async def drain(self) -> None:
        """等所有在飞的回放跑完（测试用；生产路径不依赖它）。"""

        while self._inflight:
            await asyncio.gather(
                *list(self._inflight.values()), return_exceptions=True
            )

    async def shutdown(self) -> None:
        """关停：取消在飞的回放，并把每一轮**收成终态**。

        为什么必须收成终态：``task.cancel()`` 抛的是 ``CancelledError``
        （``BaseException``），:func:`run_replay` 那个"失败落稳定码"的兜底
        ``except Exception`` 抓不到它 —— 于是 run 会永远停在 ``running``，直到
        租约过期后被下一次启动扫描回收。那不是"没有后台任务了"，那是"库里留了一
        条没人负责的 running"。所以这里在取消并等待之后，显式把这些 run 写成
        ``interrupted``：它们**确实**是"有人启动了但没人回来"。

        关停之后这个执行器不再接受提交（:meth:`reserve` 返回 ``None``），
        于是不会有新的后台任务在服务停下之后还去用数据库。
        """

        self._closed = True
        tasks = list(self._inflight.items())
        for _run_id, task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(
                *[task for _run_id, task in tasks], return_exceptions=True
            )
        for run_id, _task in tasks:
            try:
                await _finalize_orphaned_run(self._session_factory, int(run_id))
            except Exception:  # noqa: BLE001 - 关停期间不得把异常抛给上层
                log.warning(
                    "replay: 关停时无法收尾 run=%s error=%s",
                    run_id,
                    RUN_ERROR_INTERRUPTED,
                )
        # 预留出去但没人消费的名额一律还回来，免得容量被永久扣掉。
        self._reserved_groups.clear()
        self._pending = 0


__all__ = [
    "CONFIRMED_HIT_CONFIDENCE",
    "ERROR_EMPTY_RESPONSE",
    "ERROR_INCONCLUSIVE_RULES",
    "ERROR_INVALID_VERDICT_FIELD",
    "ERROR_MISSING_CONFIDENCE",
    "ERROR_MODEL_ERROR",
    "ERROR_MODEL_TIMEOUT",
    "ERROR_NONE",
    "ERROR_UNPARSEABLE",
    "GOLD_LABELS",
    "ImportSignal",
    "LABEL_ACTION_CLEAR",
    "LABEL_ACTION_CONFIRM",
    "LABEL_ACTION_CORRECT",
    "LABEL_ACTION_MANUAL_CREATE",
    "LABEL_BENIGN",
    "LABEL_SOURCE_ADMIN_BAN",
    "LABEL_SOURCE_MANUAL_CONFIRM",
    "LABEL_STATE_LABELLED",
    "LABEL_STATE_UNLABELLED",
    "LABEL_VIOLATION",
    "MAX_CANDIDATE_RULES",
    "MAX_SAMPLES_PER_RUN",
    "REPLAY_INLINE_DEADLINE_SECONDS",
    "REPLAY_INLINE_SAMPLE_LIMIT",
    "REPLAY_LEASE_SECONDS",
    "REPLAY_MAINTENANCE_INTERVAL_SECONDS",
    "REPLAY_MAINTENANCE_SAMPLE_BATCH",
    "REPLAY_MAX_CONCURRENCY",
    "REPLAY_MAX_CONCURRENCY_PER_GROUP",
    "REPLAY_MAX_PENDING",
    "REPLAY_MODEL_TIMEOUT_SECONDS",
    "REPLAY_RUN_DEADLINE_SECONDS",
    "REPLAY_RUN_SAMPLE_LIMIT",
    "RUN_ERROR_CANCELLED",
    "RUN_ERROR_DEADLINE",
    "RUN_ERROR_FAILED",
    "RUN_ERROR_INTERRUPTED",
    "RUN_ERROR_LEASE_LOST",
    "RUN_CANCELLED",
    "RUN_COMPLETED",
    "RUN_FAILED",
    "RUN_INTERRUPTED",
    "RUN_QUEUED",
    "RUN_RUNNING",
    "RUN_TERMINAL_STATUSES",
    "ReplayClaim",
    "ReplayError",
    "ReplayEvaluation",
    "ReplayExecutor",
    "ReplayMaintenance",
    "RuleSnapshot",
    "SAMPLE_CONTEXT_MAX_CHARS",
    "SAMPLE_SOURCE_KINDS",
    "SAMPLE_TTL_DAYS",
    "SAMPLE_TEXT_MAX_CHARS",
    "SOURCE_KIND_CONFIRMED",
    "SOURCE_KIND_MANUAL",
    "SOURCE_KIND_RELEASE",
    "VERDICT_BENIGN",
    "VERDICT_ERROR",
    "VERDICT_VIOLATION",
    "build_fidelity_block",
    "build_replay_llm",
    "build_replay_user_input",
    "cancel_run",
    "canonical_json",
    "capture_model_binding",
    "claim_run",
    "compute_rules_fingerprint",
    "compute_summary",
    "count_active_samples",
    "create_manual_sample",
    "create_run",
    "delete_sample",
    "digest_of",
    "evaluate_snapshot",
    "freeze_baseline",
    "get_run",
    "get_run_any",
    "import_samples_from_violations",
    "list_label_audits",
    "list_results",
    "list_runs",
    "list_samples",
    "purge_expired_samples",
    "redact_replay_text",
    "rule_snapshot_from_row",
    "run_maintenance_pass",
    "run_replay",
    "scan_deterministic",
    "set_sample_label",
    "sweep_interrupted_runs",
    "validate_candidate",
]
