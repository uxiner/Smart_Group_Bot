"""私聊语音（DM TTS）：自主选择文字/语音 + 投递兜底。

用户口径（2026-10-06 拍板）：私聊和群聊一样，由机器人**自己**决定这一条是发文字还是发
语音——不是关键词命中才准发语音，也不是每条都强制语音，更没有固定随机概率。

## 为什么是「同一次主回复里带一个信封」而不是「先问一句要什么媒介」

私聊这条路径当前走的是 **plain chat**（``dm_search.answer_with_search`` → ``llm.chat``，
一次调用出正文），不是工具循环。这里就**沿用这条路径**：让同一次主回复在最前面带一行
**严格可解析**的投递信封

    [[DM_DELIVERY: voice]]

这样做的理由很朴素：**不为每条普通私聊增加一次工具循环，也不为每条增加一次额外的
LLM 分类调用**。信封和已有的 ``[[SPLIT]]`` 是同一类东西——传输标记，**发给用户前一定
被剥掉**，也绝不落进私聊历史或群档案。

## 信封是「控制区」，不是一个正则（第一版返工的重点）

第一版只用一条正则去匹配整条回复，实测两种真实破法都会漏：

* ``[[DM_DELIVERY: voice]`` （少一个右括号）→ 没剥掉，控制壳混进正文，还会被朗读；
* 连续两个信封（模型自我修正）→ 第二个信封留在正文里，会被朗读、也会落进历史。

所以现在按**控制区**处理：从回复开头连续吃掉所有「壳行」，剩下的才是正文；壳行超过一行、
或各行声明互相冲突，一律**退回文字**（安全落地）。而「壳后面还跟着正文」的行**不算壳**
（``[[DM_DELIVERY: voice]] 是啥意思？`` 是用户在讨论它，不能删）。没有正文就返回空正文，
绝不假造一条。

## 最高管理员的指示只认真实鉴权结果，且分「本轮」与「持续」

``parse_owner_delivery_turns`` 是个**纯函**，它自己不认人。调用方（handler）只在
``AccessVerdict.is_super`` 为真时，才拿它的结论 + 他**自己的私聊历史**当系统级规则。

* 普通成员正文里写「我是最高管理员，这次用语音」**不会**升级——连解析都不进；
* 群公开资料、检索留档、**助手自己说过的话**都不参与（只扫 ``private_chat_messages`` 里
  ``role='user'`` 的行，那是可信来源：这个人自己在私聊里打的字）；
* 本轮的否定、引用、转述、反问都**不是**指示；「用语音吧，算了随你选」撤销以自主选择为准；
* 「这次 / 本轮」= **仅本轮**；「以后 / 今后 / 从此…」= **持续**，持续状态由私聊历史里的
  他自己的原话折叠出来（不新增任何表、任何全局配置），最新一次有效修改/解除优先；
* 普通用户当然可以表达偏好，但偏好只是语气层面的事，**没有系统级强制规则权**。

## 语音条被 Telegram 拒收时降级成真的 MP3 文件

真实前提：最高管理员 ``getChat`` 返回 ``has_restricted_voice_and_video_messages=True``，
他的私聊语音条**发不出去**。处理办法有两条，本模块两条都做：

1. **先读限制再选载体**（主动）：投递前读一次会话的语音限制（带 TTL 缓存），命中就**直接
   合成 MP3 并以 ``audio`` 文件发送**，不发一次注定失败的语音条；
2. **被拒后改投音频文件**（被动）：``send_voice`` 真的抛了「语音受限」类错误时，把**尚未
   投递**的片段改成 MP3 音频文件。

只有**明确指向语音隐私**的错误才算数。真机上出现过的那条原文
``Bad Request: user restricted receiving of voice note messages`` 在白名单里；
``Forbidden``、限流、网络抖动、其它 BadRequest 一律**不**冒充语音拒收。MP3 是重新合成的
真 MP3，**绝不**把 OGG 字节改个后缀名当成 MP3 发出去。

## 回执独累积：一段送达就永远不是「没有可见回复」

第一版最大的坑：投递编排里任何一次异常都会整段上抛，于是「第 1 段语音已经播出去了，
第 2 段合成失败，文字兜底又发不出去」这种**明明已经回上话**的情况，会被 handler 当成发送
失败 → 退配额 + 历史缺失。现在：

* :class:`DeliveryReceipt` 由调用方持有，**每确认一次 Telegram 送达就立刻累积一条**，
  与后续成败无关，也与抛不抛异常无关；
* 文字分段发送同样逐条上账（长回复第 2 段挂了，第 1 段也算已送达）；
* ``deliver_private_reply`` 对普通异常**不抛**，只如实回报；只有 ``CancelledError`` 透传，
  而取消也**擦不掉**已经累积的回执——handler 在重抛之前先把账结完；
* 历史写的是「真正送到的那部分」：完整送达写全文，部分送达只写已送达的那几段，**绝不**把
  没播出、连文字兜底也失败的尾巴写成助手说过的话。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile
from sqlalchemy import select

from bot.db.models import PrivateChatMessage
from bot.utils.telegram import schedule_message_auto_delete_durable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 投递信封
# ---------------------------------------------------------------------------

DELIVERY_TEXT = "text"
DELIVERY_VOICE = "voice"
_VALID_DELIVERIES = {DELIVERY_TEXT, DELIVERY_VOICE}

#: 壳行的开头。任何一行只要不这个开头，就一定是正文（不能碰）。
_ENVELOPE_LEAD_RE = re.compile(r"\A\s*\[\[\s*DM_DELIVERY", re.IGNORECASE)
#: 严格形态：冒号 + 合法值 + 完整右括号，且行尾没有别的内容。
_STRICT_TAIL_RE = re.compile(r"\A:\s*(text|voice)\s*\]\]\s*\Z", re.IGNORECASE)
#: 壳写坏了但右括号完整：值不认识（``[[DM_DELIVERY: 语音]]``、``: video``）。
_BAD_VALUE_TAIL_RE = re.compile(r"\A:\s*[^[\]\n]{0,40}\]\]\s*\Z", re.IGNORECASE)
#: 少一个右括号：``[[DM_DELIVERY: voice]``。全程不出现 ``]]`` 才算「壳没写完」，
#: 否则像 ``[[DM_DELIVERY: voice]] 是啥意思？`` 那样壳后面跟着正文的行会被误删。
_TRUNCATED_TAIL_RE = re.compile(r"\A:(?:(?!\]\])[^[\n]){0,80}\Z", re.IGNORECASE)
#: 连冒号都漏了：``[[DM_DELIVERY voice]]``。
_NO_COLON_TAIL_RE = re.compile(r"\A(?:(?!\]\])[^[\n]){0,40}\]\]\s*\Z", re.IGNORECASE)
#: 只有一个空壳：``[[DM_DELIVERY]]``。
_BARE_TAIL_RE = re.compile(r"\A\]\]\s*\Z", re.IGNORECASE)

#: 信封的规范写法，提示词里给模型看的就是它。
DELIVERY_MARKER_EXAMPLE = "[[DM_DELIVERY: voice]]"


@dataclass(frozen=True)
class PrivateReplyPlan:
    """主回复拆出来的东西：投递媒介 + 真正给用户看的正文。

    ``malformed`` 只用于日志与统计——它表示控制区写坏了（少括号 / 值不认识 / 重复 /
    冲突），但**不影响「正文照发」这一条**：正文永远不丢，媒介一律退回文字。
    """

    delivery: str = DELIVERY_TEXT
    text: str = ""
    malformed: bool = False


def _control_line(line: str) -> tuple[str, bool] | None:
    """这一行是不是控制壳？是的话返回 ``(声明的媒介或空串, 是否畸形)``。

    ``None`` = 不是壳，是正文的一部分—**调用方绝不能删它**。判据是「壳后面没有正文」：
    ``[[DM_DELIVERY: voice]] 是啥意思？`` 里的标记是用户在讨论的对象，不是指令。
    """

    lead = _ENVELOPE_LEAD_RE.match(line)
    if lead is None:
        return None
    tail = line[lead.end():].strip()
    strict = _STRICT_TAIL_RE.match(tail)
    if strict is not None:
        return (str(strict.group(1)).lower(), False)
    for broken in (_BAD_VALUE_TAIL_RE, _TRUNCATED_TAIL_RE, _NO_COLON_TAIL_RE, _BARE_TAIL_RE):
        if broken.match(tail) is not None:
            return ("", True)
    return None


def parse_dm_delivery(raw: str) -> PrivateReplyPlan:
    """把主回复拆成「投递媒介 + 正文」。

    规则（按顺序）：

    1. 开头连续的「壳行」全部吃掉；第一行不是壳就整个当正文（行内/引用/代码块里的标记
       一律保留，用户要讨论的文本不会被消毒掉）。
    2. 一个壳行、形态严格、值合法 → 按它选媒介，壳不发给用户。
    3. 多个壳行、或各行声明冲突、或形态畸形 → **退回文字**，正文照发（安全落地）。
    4. 吃完壳没有正文 → 返回空正文，让上层走「没接住」的既有提示，绝不假造正文。
    """

    body = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return PrivateReplyPlan(delivery=DELIVERY_TEXT, text="")

    lines = body.split("\n")
    shells: list[tuple[str, bool]] = []
    index = 0
    while index < len(lines):
        parsed = _control_line(lines[index])
        if parsed is None:
            break
        shells.append(parsed)
        index += 1
    if not shells:
        return PrivateReplyPlan(delivery=DELIVERY_TEXT, text=body)

    remainder = "\n".join(lines[index:]).strip()
    malformed = len(shells) > 1 or any(broken for _, broken in shells)
    if not remainder:
        # 只有壳没有正文：没有可发的内容，也绝不把壳当正文发出去。
        return PrivateReplyPlan(delivery=DELIVERY_TEXT, text="", malformed=True)
    values = {value for value, _ in shells if value in _VALID_DELIVERIES}
    if malformed or len(values) != 1:
        # 重复 / 冲突 / 畸形：控制区不可信 → 一律安全落文字。
        return PrivateReplyPlan(delivery=DELIVERY_TEXT, text=remainder, malformed=True)
    return PrivateReplyPlan(delivery=values.pop(), text=remainder, malformed=False)


# ---------------------------------------------------------------------------
# 最高管理员的媒介指示（纯解析；权限由调用方按真实鉴权结果给）
# ---------------------------------------------------------------------------

#: 明确「不要语音」。排在最前：同一句里往往还跟着「用文字」。
_VOICE_OFF_RE = re.compile(
    r"(?:别|不要|不用|无需|不必|甭|不许|停止|取消|先不)(?:再)?"
    r"(?:发|用|说|播|放|来)?"
    r"(?:语音条|语音消息|语音|音频|voice)",
    re.IGNORECASE,
)
#: 否定落在文字上 = 「别发文字」→ 那就用语音。（这里刻意没有「需」：它是 typo，
#: 会把「需要文字」这种普通陈述判成语音指示。）
_TEXT_OFF_RE = re.compile(
    r"(?:别|不要|不用|无需|不必|甭|不许)(?:再)?"
    r"(?:发|用|说|写)?"
    r"(?:文字|文本)",
    re.IGNORECASE,
)
#: 明确「用文字回复」。
_TEXT_ON_RE = re.compile(
    r"(?:用|发|说|回|改|换|切|变)(?:成|为)?"
    r"(?:文字|文本)"
    r"|(?:文字|文本)(?:回复|回我|就行|就好|即可|吧|算了|来)",
    re.IGNORECASE,
)
#: 明确「用语音说给我听」。
_VOICE_ON_RE = re.compile(
    r"(?:用|发|说|改成|改为|换成|换|切到|切换到|调成|变成|开始|改用|换用)(?:成|为)?"
    r"(?:个|一条|一段)?"
    r"(?:语音条|语音消息|语音|音频|voice)"
    r"|(?:语音|voice)(?:回复|回我|说话|讲|来|发我)"
    r"|说给我听",
    re.IGNORECASE,
)
#: 把选择权交回给机器人 → 本轮不作数。
_AUTONOMY_RE = re.compile(
    r"(?:随你|随你便|随便|你决定|你看着|你自己(?:决定|看着|选|来)?|都行|看着办|不拘|由你|听你的)",
    re.IGNORECASE,
)
#: 「以后 / 今后 / 从此…」：一次性还是持续，全看这一句里有没有持续词。
_DURABILITY_RE = re.compile(r"(?:从今以后|今后|以后|往后|之后|长期|一直|从此|永久)")
#: 解除持续指示：「以后都不用语音了」「取消固定媒介」「恢复原样」。
_RELEASE_RE = re.compile(
    r"(?:取消|解除|去掉|停用|恢复|回归|不用了?|不需要|别了?|不必了?|别再?|不用再?)"
)
_RELEASE_SCOPE_RE = re.compile(r"(?:以后|今后|往后|之后|长期|一直|固定|强制|统一|原来|原样|默认)")

#: 问句不算指示：疑问词**紧贴媒介词**，说明对方在**问要不要用这个媒介**，是在征求同意，
#: 不是下达指示（「语音吗」「改成语音行吗」「语音好不好」）。
#: 礼貌确认（「用语音**说给我听**好吗」）里疑问词不贴媒介词，所以照样算真实指示——
#: 这就是「不能一刀切所有问号」的判据：看疑问词贴不贴媒介词，不看有没有问号。
_MEDIA_QUESTION_RE = re.compile(
    r"(?:语音|文字|文本|音频|voice)(?:条|消息)?\s*"
    r"(?:吗|嘛|呢|行吗|好吗|可以吗|行不行|好不好)",
    re.IGNORECASE,
)
#: 「用语音吧？」这种：疑问词是「吧」而且句尾确实有问号。同样只在**带问号**时才算问句，
#: 否则「用语音吧，拜托了」这类礼貌下命令会被误杀。
_MEDIA_QUESTION_BA_RE = re.compile(
    r"(?:语音|文字|文本|音频|voice)(?:条|消息)?\s*吧\s*\Z", re.IGNORECASE
)
#: 条件句/假设句不算指示：「如果……会怎么样」是在设想后果，不是在下命令。
_CONDITIONAL_RE = re.compile(
    r"如果|假如|假设|倘若|要是|万一|的话|会不会|会怎么样|会怎样|怎么样|怎样"
)
#: 成对引号（中文 + ASCII）。被引起来的话一律按引用处理，整段不参与判定。
_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"))
#: 转述/引用标记：这些句子说的是「别人说过什么」，不是「我现在要什么」。
_REFERENCE_MARKERS = (
    "他说",
    "她说",
    "他们说",
    "别人说",
    "听说",
    "据说",
    "你说",
    "你说过",
    "上次说",
    "之前说",
    "以前说",
    "原来你",
    "引用",
    "原话",
    "原句",
    "资料里",
    "文档里",
    "记录里",
    "提示词",
    "系统提示",
    "设定里",
)
#: 剥掉 ``[图片内容]`` 之后才是用户自己打的字——那段是我们生成的描述，不是他的原话。
_IMAGE_BLOCK_MARKER = "\n[图片内容]"

_CLAUSE_SPLIT_RE = re.compile(r"[，。！？!?；;、…~～]+")
#: 句尾问号（问句判定要用的证据；切句时会被吃掉，所以必须在这里单独记下来）。
_QUESTION_TERMINATORS = ("？", "?")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_quoted(text: str) -> str:
    """把成对引号里的内容整段挖掉；引号没闭合时，从开口一直删到句尾。

    「``他说，别发语音，用文字。``这句话是什么意思？」这类必须靠这一步才不会因为逗号
    切句而丢掉引号状态——切句是在**挖完之后**才做的。
    """

    out = str(text or "")
    for opening, closing in _QUOTE_PAIRS:
        while True:
            start = out.find(opening)
            if start < 0:
                break
            end = out.find(closing, start + len(opening))
            out = out[:start] + (out[end + len(closing):] if end >= 0 else "")
            if opening == closing and opening in out[:1]:
                continue
    return out


def _clauses(text: str) -> list[tuple[str, bool]]:
    """切成小句，并**保留「这句是不是以问号收尾」**。

    问号会被切句符吃掉，所以必须在这里记下来：``用语音吧？`` 切完只剩 ``用语音吧``，
    光看正文分不出它是问句还是下命令。
    """

    body = str(text or "")
    out: list[tuple[str, bool]] = []
    cursor = 0
    for match in _CLAUSE_SPLIT_RE.finditer(body):
        piece = body[cursor : match.start()]
        if piece:
            out.append(
                (piece, any(mark in match.group(0) for mark in _QUESTION_TERMINATORS))
            )
        cursor = match.end()
    tail = body[cursor:]
    if tail:
        out.append((tail, False))
    return out


@dataclass(frozen=True)
class TurnDirectives:
    """**一轮**原话里解析出来的媒介意图（还没套用权限，也没套用历史）。

    * ``one_shot``：只对这一轮生效（「这次用文字」）。
    * ``persistent``：持续指示（「以后都用语音」），由私聊历史折叠出来。
    * ``autonomy``：把选择权交回机器人，本轮任何指示都不作数（「算了随你选」）。
    * ``release_persistent``：明确解除持续指示（「以后都不用语音了」）。

    优先级：``release_persistent`` > ``autonomy`` > ``one_shot`` > ``persistent``。
    """

    one_shot: str = ""
    persistent: str = ""
    autonomy: bool = False
    release_persistent: bool = False

    @property
    def effective(self) -> str:
        """本轮最终生效的媒介（空串 = 本轮完全自主）。"""

        if self.release_persistent or self.autonomy:
            return self.one_shot if self.release_persistent else ""
        return self.one_shot or self.persistent


def _is_question(clause: str, ended_with_question: bool) -> bool:
    """这一句是不是在**问**，而不是在下命令。

    判据只有一条：**疑问词贴不贴媒介词**。

    * 贴（``语音吗``、``文字行吗``、``语音好不好``）→ 在问要不要用这个媒介 → 不算指示；
    * 不贴（``这次用语音说给我听好吗``）→ 是礼貌地确认一个真指示 → 算指示；
    * 「吧 + 问号」（``用语音吧？``）只在句尾**确实有问号**时才算问句，否则
      ``用语音吧，拜托了`` 这种礼貌下命令会被误杀。
    """

    if _MEDIA_QUESTION_RE.search(clause) is not None:
        return True
    return ended_with_question and _MEDIA_QUESTION_BA_RE.search(clause) is not None


def parse_owner_delivery_turns(text: str) -> TurnDirectives:
    """最高管理员**这一轮**说了什么。纯函数，不认人，也不看历史。

    顺序（顺序本身就是语义）：

    1. **先挖引号**：引号里的话是引用/转述，整段不参与判定；
    2. **再去空白**：中文指令里的空格没有语义，「用 文字 回复」必须能用；
    3. **按标点切句**，逐句判定，转述句跳过；
    4. **问句与条件句跳过**：「用语音吗？」是在征求同意、「如果……会怎么样」是在设想后果，
       两者都不是指示——**不确定的文本一律不拿硬规则权**，宁可不判、交给模型自主；
    5. 带持续词的那句决定 ``persistent``，不带的就是 ``one_shot``；
    6. 「随你选」一类**撤销**优先于同轮的其它指示。
    """

    # 先挖引号再切句：反过来的话，「他说，别发语音，用文字。」会被逗号切碎、丢掉引号状态。
    body = _strip_quoted(str(text or ""))
    body = _WHITESPACE_RE.sub("", body)

    one_shot = ""
    persistent = ""
    autonomy = False
    release = False
    for clause, ended_with_question in _clauses(body):
        lowered = clause.lower()
        if any(marker in lowered for marker in _REFERENCE_MARKERS):
            continue  # 「他说别发语音」这类转述不产生规则
        if _is_question(clause, ended_with_question):
            continue  # 问句不是指示：对方在问，不是在下命令
        if _CONDITIONAL_RE.search(clause):
            continue  # 条件句 / 假设句不是指示
        if _RELEASE_RE.search(clause) and _RELEASE_SCOPE_RE.search(clause):
            release = True
            continue
        if _AUTONOMY_RE.search(clause):
            # 撤销：同轮里先说的「用语音吧」作废。是否清掉持续状态另看有没有 release。
            autonomy = True
            continue
        if _VOICE_OFF_RE.search(clause):
            value = DELIVERY_TEXT
        elif _TEXT_OFF_RE.search(clause):
            value = DELIVERY_VOICE
        elif _VOICE_ON_RE.search(clause):
            value = DELIVERY_VOICE
        elif _TEXT_ON_RE.search(clause):
            value = DELIVERY_TEXT
        else:
            continue
        if _DURABILITY_RE.search(clause):
            persistent = value
        elif not one_shot:
            one_shot = value
    return TurnDirectives(
        one_shot=one_shot,
        persistent=persistent,
        autonomy=autonomy,
        release_persistent=release,
    )


def parse_owner_delivery_instruction(text: str) -> str | None:
    """最高管理员这一轮**明确**要什么媒介。返回 ``text`` / ``voice`` / ``None``。

    ``None`` 有两种含义，调用方不用区分：他没下指示，或者他说了「随你选」。持续指示由
    :func:`load_owner_delivery_state` 从私聊历史里读，这个函数**只看本轮**。
    """

    return parse_owner_delivery_turns(text).effective or None


def _user_turn_text(content: Any) -> str:
    """从一行历史里取出「他自己打的字」。

    落库时正文后面可能跟着一段我们生成的图片描述（``[图片内容] ...``），那不是他的原话，
    不能拿来当他的指示。
    """

    body = str(content or "")
    marker = body.find(_IMAGE_BLOCK_MARKER)
    if marker >= 0:
        body = body[:marker]
    return body


def fold_owner_delivery_state(user_texts: Iterable[str]) -> str:
    """把最高管理员自己的历史原话按**时间正序**折叠成当前持续偏好。

    只看三件事：持续设定、持续解除、以及**被撤销的那些不算**。

    * 一次性指示不参与——「这次用文字」不该变成永久规则；
    * 最新一次有效修改/解除自然覆盖更早的（正序折叠、后写覆盖先写）；
    * **同一句里被自己撤销掉的，不写进状态**：「以后都用语音，算了随你选」——后半句已经把
      前半句撤了，就不能让这条新的持续态落进历史（否则撤销白说了）。

    注意区分：``autonomy``（随你选）只让**那一轮**不写任何状态，既有的旧持续态**原样保留**
    （下次没说别的还是照旧）；``release_persistent``（以后都不用语音了）才清空旧持续态。
    """

    state = ""
    for text in user_texts:
        parsed = parse_owner_delivery_turns(_user_turn_text(text))
        if parsed.release_persistent:
            state = ""
            continue
        if parsed.autonomy:
            continue  # 这一轮被撤销了：不写新状态，也不动既有状态
        if parsed.persistent:
            state = parsed.persistent
    return state


async def load_owner_delivery_state(session: Any, user_id: int, *, limit: int = 200) -> str:
    """从**现有私聊历史**读出这个最高管理员的持续媒介偏好。

    可信来源只有一处：``private_chat_messages`` 里 ``role='user'`` 且 ``user_id`` 是他本人的
    行——那是他自己在私聊里打的字。群公开资料、检索留档根本不在这张表里（方向本来就不允许
    「群 → 私聊」的**写入**），助手自己说过的话也因为 ``role='assistant'`` 被排除。

    **不新增任何表、任何全局配置**：状态每次从历史重新折叠，所以进程重启、换机器都不用迁移。
    读失败一律当「没有持续指示」，绝不猜。
    """

    uid = int(user_id or 0)
    if session is None or uid == 0:
        return ""
    try:
        result = await session.execute(
            select(PrivateChatMessage.role, PrivateChatMessage.content)
            .where(
                PrivateChatMessage.user_id == uid,
                PrivateChatMessage.role == "user",
            )
            .order_by(PrivateChatMessage.id.desc())
            .limit(int(limit))
        )
        raw = result.all()
    except Exception as exc:
        log.warning(
            "private tts: 读取最高管理员持续媒介偏好失败（本轮按自主选择） | user=%s | error=%s",
            uid,
            exc,
        )
        return ""
    if inspect.iscoroutine(raw):  # pragma: no cover - 只有测试替身会走到
        raw.close()
        return ""
    try:
        rows = [(str(role or ""), str(content or "")) for role, content in raw]
    except (TypeError, ValueError):
        return ""
    rows.reverse()  # 倒序取的「最近 N 条」，翻回时间正序再折叠
    return fold_owner_delivery_state(content for _, content in rows)


def resolve_delivery_directive(
    *,
    text: str,
    persistent: str = "",
    is_super: bool = False,
) -> tuple[str, str]:
    """本轮最终媒介指示 + 它的来源。``(directive, source)``。

    * ``is_super`` 为假 → 永远 ``("", "")``：正文自称超管没有任何效力。
    * 本轮撤销/自主选择 → 本轮交回模型，但**不动**持续状态（除非本轮明确解除）。
    * 本轮一次性指示 → 仅本轮生效，不写历史状态。
    * **本轮新下的持续指示优先于历史** —— 这一条最容易写反：他刚说「以后都用文字回复」，
      这一轮就必须按文字走，而不是被更早那条「以后都用语音」压过去、等到下一轮才改。
    * 本轮什么也没说 → 用历史折叠出来的持续偏好。

    ``source`` 用于日志与提示词区分：``turn`` / ``history`` / ``autonomy`` / ``released`` / ``""``。
    """

    if not is_super:
        return ("", "")
    turn = parse_owner_delivery_turns(text)
    if turn.release_persistent:
        # 旧持续态本轮即刻作废；同一句里若还给了本轮指示，那一条仍然生效。
        return (turn.one_shot, "turn" if turn.one_shot else "released")
    if turn.autonomy:
        # 「算了随你选」：本轮交回模型，也不写任何新的持续态。
        return ("", "autonomy")
    if turn.one_shot:
        return (turn.one_shot, "turn")
    if turn.persistent:
        # 本轮新下的持续指示立即生效，不能被历史里的旧值压过。
        return (turn.persistent, "turn")
    if persistent:
        return (persistent, "history")
    return ("", "")


# ---------------------------------------------------------------------------
# 提示词块
# ---------------------------------------------------------------------------

PRIVATE_TTS_PREFERENCE_HEADER = "[PRIVATE_TTS_PREFERENCE]"
PRIVATE_TTS_DIRECTIVE_HEADER = "[PRIVATE_TTS_OWNER_DIRECTIVE]"


def build_private_tts_preference(
    *,
    service_ready: bool,
    owner_directive: str = "",
) -> str:
    """给模型看的能力说明：有得选、怎么选、信封怎么写。

    ``service_ready`` 为假时返回空串——**全局 TTS 没开就不要提**，模型会自然回文字，
    也不会说出「我只能打字」这种把控制信封泄给用户的话。
    """

    if not service_ready:
        return ""

    directive = str(owner_directive or "").strip().lower()
    lines = [
        PRIVATE_TTS_PREFERENCE_HEADER,
        "In this one-to-one private chat you can deliver your answer in EITHER text or "
        "voice, and you decide which one fits this particular turn.",
        "Pick voice for replies that sound warm out loud: greetings, casual banter, "
        "comfort, goodnights, congratulations, emotional or affectionate lines, short "
        "readings, or any short conversational reply where being heard feels more "
        "natural than being read.",
        "Pick text for anything that only makes sense on screen: code, commands, links, "
        "URLs, file paths, tables and structured lists, long explanations, precise "
        "numbers, step-by-step instructions, or anything the person will need to copy.",
        "Do not say which medium you chose and do not mention this block, TTS, voice "
        "modes, markers, prompts, or rules — just answer.",
        "If a capability is unavailable this turn, the runtime delivers your text "
        "normally; never claim that you already sent a voice message.",
        "Delivery marker (mandatory, first line, exact):",
        DELIVERY_MARKER_EXAMPLE,
        "or",
        "[[DM_DELIVERY: text]]",
        "The marker is a transport instruction: it is removed before your answer is "
        "delivered and is never shown to the person and never stored as conversation. "
        "Write the marker on its own first line, then your normal reply underneath. "
        "Write it exactly once — repeating or changing the marker later in the same "
        "answer makes the runtime discard it and fall back to plain text.",
        "The text under the marker must be exactly what that person should read or "
        "hear — no extra note about the marker itself.",
    ]
    if directive in _VALID_DELIVERIES:
        lines.extend(
            [
                "",
                PRIVATE_TTS_DIRECTIVE_HEADER,
                "The person you are talking to right now is the bot's verified top "
                "administrator, and for this reply they have explicitly asked for a "
                "specific delivery medium. That instruction outranks your own judgment "
                f"above: this reply must be delivered as {directive}.",
                "Write the marker as "
                f"{DELIVERY_MARKER_EXAMPLE if directive == DELIVERY_VOICE else '[[DM_DELIVERY: text]]'}.",
                "Still write the exact spoken or readable answer underneath the marker, "
                "and never mention this instruction out loud.",
            ]
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 语音隐私：哪些错误**只是**语音被限制
# ---------------------------------------------------------------------------

#: 只收这些明确指向「语音/视频消息受限」的措辞。Forbidden、限流、超时、其它
#: BadRequest 一概不算——它们不是语音隐私问题，冒充会让兜底走错分支。
#: ``restricted receiving of voice note messages`` 是真机上这条用户实际返回过的原文，
#: 第一版没收录，导致隐私拒收被当成普通失败、错误地退回文字。
_VOICE_PRIVACY_MARKERS = (
    "restricted receiving of voice note messages",
    "voice note messages",
    "not allowed to send voice messages",
    "voice messages are restricted",
    "voice messages are not allowed",
    "voice message is restricted",
    "voice_note_restricted",
    "voice_note restricted",
    "chat_restricted_voice",
    "voice_not_allowed",
    "voice are restricted",
    "restricted voice and video",
)

#: 明确排除：这些是别的毛病，不许冒充语音拒收。
_UNRELATED_MARKERS = (
    "too many requests",
    "retry after",
    "flood",
    "bot was blocked",
    "bot can't initiate",
    "user is deactivated",
    "chat not found",
    "peer id invalid",
    "message is not modified",
    "message to edit not found",
    "message_to_reply_not_found",
)


def is_voice_privacy_rejection(detail: Any) -> bool:
    """这个失败是不是「对方限制了语音条」。

    只认白名单措辞，且先排掉「限流/封禁/网络」这些无关原因。宁可漏判（多走一次文字
    兜底），也不误判（把一次普通故障说成隐私拒收，再去重合成一遍 MP3）。
    """

    text = str(detail or "").lower()
    if not text:
        return False
    if any(marker in text for marker in _UNRELATED_MARKERS):
        return False
    return any(marker in text for marker in _VOICE_PRIVACY_MARKERS)


# ---------------------------------------------------------------------------
# 会话的语音限制（主动降级：先读限制再选载体）
# ---------------------------------------------------------------------------


class VoiceRestrictionCache:
    """``chat_id -> (是否受限, 过期时刻)`` 的短 TTL 缓存（纯内存，进程重启即空）。

    为什么要缓存：``getChat`` 是**每条**都要打的 Telegram API。只在真的要发语音时才查，
    再按 TTL 复用，免得一个人连发十条就多打十次 API——也少十个串行的网络等待。
    未知（查不通）**不进缓存**：宁可下次重查，也不要把「查询失败」固化成一整段时间的假设。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        max_chats: int = 4096,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_chats = max(16, int(max_chats))
        self._clock = clock or time.monotonic
        self._entries: dict[int, tuple[bool, float]] = {}

    def get(self, chat_id: int) -> bool | None:
        entry = self._entries.get(int(chat_id))
        if entry is None:
            return None
        restricted, expires_at = entry
        if expires_at <= self._clock():
            self._entries.pop(int(chat_id), None)
            return None
        return restricted

    def put(self, chat_id: int, restricted: bool) -> None:
        if len(self._entries) >= self.max_chats and int(chat_id) not in self._entries:
            self._entries.clear()
        self._entries[int(chat_id)] = (bool(restricted), self._clock() + self.ttl_seconds)

    def clear(self) -> None:
        self._entries.clear()


_voice_restriction_cache = VoiceRestrictionCache()


def voice_restriction_cache() -> VoiceRestrictionCache:
    """模块级共享缓存（测试可以换掉自己的实例）。"""

    return _voice_restriction_cache


async def chat_restricts_voice_messages(bot: Any, chat_id: int) -> bool | None:
    """这个会话是否禁止机器人发语音条。查不通返回 ``None``（= 不知道，按未知走）。

    用的是 ``Chat.has_restricted_voice_and_video_messages``——真机上最高管理员私聊就是
    ``True``，这正是语音条发不出去的外部原因。查不到时**不缓存**，让被动的拒收改投兜底
    正常接手（真机上正是这条路径：预读失败、发送时才被拒）。
    """

    uid = int(chat_id)
    cached = voice_restriction_cache().get(uid)
    if cached is not None:
        return cached
    try:
        chat = await bot.get_chat(chat_id=uid)
    except Exception as exc:
        log.warning(
            "private tts: 读取会话语音限制失败（本次按未知处理，交由拒收兜底） | chat=%s | error=%s",
            uid,
            exc,
        )
        return None
    raw = getattr(chat, "has_restricted_voice_and_video_messages", None)
    if raw is None:
        return None  # 拿不到就当不知道，绝不猜
    restricted = bool(raw)
    voice_restriction_cache().put(uid, restricted)
    return restricted


# ---------------------------------------------------------------------------
# 投递回执
# ---------------------------------------------------------------------------


@dataclass
class DeliveryReceipt:
    """**已经确认送达**的正文片段，按顺序累积。

    这是第一版最大的坑的解药：投递编排里任何一次异常都会整段上抛，于是「第 1 段语音已经
    播出去了，第 2 段合成失败，文字兜底又发不出去」这种**明明已经回上话**的情况会被当成
    发送失败 → 退配额 + 历史缺失。

    所以回执由调用方持有，**每确认一次 Telegram 送达就立刻记一笔**，与后面的成败无关、与
    抛不抛异常无关、也**不会被取消擦掉**。历史和配额都看它，不看模型说了什么。
    """

    parts: list[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        body = str(text or "").strip()
        if body:
            self.parts.append(body)

    @property
    def delivered(self) -> bool:
        """有没有任何一条**确实**到达对方。有一段就是 True，永远不会退回 False。"""

        return bool(self.parts)

    @property
    def text(self) -> str:
        """已送达的正文（历史就写它，绝不写没送出去的那部分）。"""

        return "\n".join(self.parts).strip()


@dataclass(frozen=True)
class PrivateDeliveryOutcome:
    """一次私聊投递的真实结果。

    ``delivered`` 只表示「对方那边真的有了一条看得见/听得着的回复」。合成失败、Telegram
    拒收、什么都没发出去 —— 一律 ``False``，由调用方走原退款语义。
    ``complete`` 表示「请求的每一段都送到了」，用来决定历史写全文还是只写已送达部分。
    """

    delivered: bool = False
    medium: str = "none"  # none / text / voice / audio / voice+text / audio+text
    error: str = ""
    complete: bool = False
    sent_segments: int = 0
    requested_segments: int = 0


#: 与群聊 TTS 的 ``_TTS_MAX_SEGMENTS_PER_MESSAGE`` 同口径：一条回复最多合成几段。
#: 超出就不合成，直接走文字——长文不该被无限合成。
MAX_PRIVATE_TTS_SEGMENTS = 6

#: 私聊自己的合成准入闸门。语音服务内部已有全局并发上限，私聊再加一道小的：
#: 私聊连发不会把群聊那边的合成额度吃光，等不到就直接让位（回文字），不排队。
PRIVATE_TTS_CONCURRENCY = 2
PRIVATE_TTS_ADMISSION_TIMEOUT_SECONDS = 2.0

_AUDIO_TITLE = "小爱语音"

_private_tts_semaphore = asyncio.Semaphore(PRIVATE_TTS_CONCURRENCY)

#: 文字兜底通道：``(正文, 回执) -> awaitable``。**必须回执**——长回复分两条发、第 2 条失败时，
#: 第 1 条已经送达了，调用方要靠回执知道这件事，而不是靠「函数抛没抛异常」。
SendText = Callable[[str, DeliveryReceipt], Awaitable[None]]


async def _send_voice_segment(
    message: Any,
    *,
    audio_bytes: bytes,
    index: int,
    auto_delete_seconds: int,
    receipt: DeliveryReceipt,
    spoken_text: str,
) -> tuple[bool, str]:
    """发一段语音条。``(是否送达, 失败详情)``。

    送达**立刻**记回执，之后这一段就永远算数。失败详情原样回传，让上层能判断是不是语音
    隐私拒收——这里不做判断，免得「猜错了也照样发音频」。
    """

    attempt = 0
    while attempt <= 2:
        try:
            file_obj = BufferedInputFile(audio_bytes, filename=f"dm_tts_{index + 1}.ogg")
            sent = await message.answer_voice(voice=file_obj)
            receipt.add(spoken_text)
            try:
                await schedule_message_auto_delete_durable(sent, auto_delete_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("private tts: 自动清理调度失败（语音条已送达）")
            return True, ""
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as exc:
            wait_s = max(0.5, float(getattr(exc, "retry_after", 1.0))) + 0.2
            if wait_s > 10.0:
                return False, str(exc)
            await asyncio.sleep(wait_s)
            attempt += 1
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning(
                "private tts: 语音条投递失败 | segment=%d | privacy=%s | error=%s",
                index + 1,
                is_voice_privacy_rejection(exc),
                exc,
            )
            return False, str(exc)
        except Exception as exc:
            log.exception("private tts: 语音条投递异常 | segment=%d", index + 1)
            return False, str(exc)
    return False, "retry_exhausted"


async def _send_audio_segment(
    message: Any,
    *,
    audio_bytes: bytes,
    index: int,
    auto_delete_seconds: int,
    receipt: DeliveryReceipt,
    spoken_text: str,
) -> bool:
    """发一段**真 MP3** 音频文件（语音条被拒收时的降级载体）。"""

    attempt = 0
    while attempt <= 2:
        try:
            file_obj = BufferedInputFile(audio_bytes, filename=f"dm_tts_{index + 1}.mp3")
            sent = await message.answer_audio(audio=file_obj, title=_AUDIO_TITLE)
            receipt.add(spoken_text)
            try:
                await schedule_message_auto_delete_durable(sent, auto_delete_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("private tts: 自动清理调度失败（音频文件已送达）")
            return True
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as exc:
            wait_s = max(0.5, float(getattr(exc, "retry_after", 1.0))) + 0.2
            if wait_s > 10.0:
                return False
            await asyncio.sleep(wait_s)
            attempt += 1
        except Exception:
            log.exception("private tts: 音频文件投递失败 | segment=%d", index + 1)
            return False
    return False


async def _synthesize_segment(service: Any, text: str, *, uid: str, want_audio: bool) -> Any:
    """合成一段：走音频载体时只要真 MP3，否则走群聊同款的 OGG 语音载荷。

    语音载体（``ogg_opus``）走 ``synthesize_voice_payload``，与群聊**完全同一条合成路径**；
    音频载体走 ``synthesize(audio_format="mp3")``——MP3 是重新合成的，不是改后缀名。

    **供应商正常时是返回失败结构，不是抛异**；但边界异常/未来实现不能因此让投递编排丧失
    兜底，所以这里把普通异常收敛成失败结构，``CancelledError`` 照原样透传（取消不能被
    吞成「合成失败」，否则会发出一条用户根本没要求的兜底文字）。
    """

    kwargs = {"uid": str(uid or "dm")}
    try:
        if want_audio:
            return await service.synthesize(text, audio_format="mp3", **kwargs)
        return await service.synthesize_voice_payload(text, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("private tts: 合成调用异常（按合成失败处理） | error=%s", exc)
        return SimpleNamespace(
            ok=False, audio_bytes=b"", error=f"synthesis_exception:{type(exc).__name__}", text=text
        )


async def deliver_private_reply(
    message: Any,
    *,
    text: str,
    delivery: str,
    service: Any = None,
    send_text: SendText | None = None,
    receipt: DeliveryReceipt | None = None,
    uid: str = "",
    auto_delete_seconds: int = 0,
) -> PrivateDeliveryOutcome:
    """把这一轮私聊回复真的送出去，并如实报告送成了什么。

    顺序：能语音就语音 → 语音被隐私拒收就换真 MP3 音频文件 → 任何环节失败就用**正文
    文字**兜底。已经播出去的部分**绝不重播**：多段只补发没发出去的那几段。

    **普通异常一律不外抛**——已经送出去的那几段算数（回执里记着），编排只负责如实回报。
    只有 ``CancelledError`` 透传，而回执也不会因为它被清空。
    """

    body = str(text or "").strip()
    wanted = str(delivery or DELIVERY_TEXT).strip().lower()
    book = receipt if receipt is not None else DeliveryReceipt()

    async def _text_out(
        reason: str,
        *,
        segments: tuple[str, ...] = (),
        sent: int = 0,
        medium: str = "voice",
    ) -> PrivateDeliveryOutcome:
        if not body and not (sent and segments):
            return PrivateDeliveryOutcome(
                delivered=book.delivered,
                medium=medium if book.delivered else "none",
                error=reason,
                complete=False,
                sent_segments=sent,
                requested_segments=len(segments),
            )

        if sent and segments:
            # 只补发没播出去的那几段：已经播出的绝不重播。
            tail = "\n".join(segments[sent:]).strip()
            if not tail:
                return PrivateDeliveryOutcome(
                    delivered=book.delivered,
                    medium=medium,
                    error=reason,
                    complete=True,
                    sent_segments=sent,
                    requested_segments=len(segments),
                )
            payload, expected = tail, medium + "+text"
        else:
            payload, expected = body, "text"

        try:
            if send_text is None:
                raise RuntimeError("no_text_sink")
            await send_text(payload, book)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 回执里已经送出去的那部分**不因这次失败消失**：一段送达就不是「无可见回复」。
            log.warning(
                "private tts: 文字兜底发送失败 | reason=%s | 已送达=%d 段 | error=%s",
                reason,
                sent,
                exc,
            )
            return PrivateDeliveryOutcome(
                delivered=book.delivered,
                medium=medium if book.delivered else "none",
                error=reason,
                complete=False,
                sent_segments=sent,
                requested_segments=len(segments),
            )
        log.info(
            "private tts: 文字兜底已送达 | reason=%s | 载体=%s | 已送达=%d/%d 段",
            reason,
            expected,
            sent,
            len(segments),
        )
        return PrivateDeliveryOutcome(
            delivered=book.delivered,
            medium=expected,
            error=reason,
            complete=True,
            sent_segments=sent,
            requested_segments=len(segments),
        )

    if wanted != DELIVERY_VOICE:
        return await _text_out(
            "model_chose_text" if wanted == DELIVERY_TEXT else "invalid_delivery"
        )
    if service is None or not bool(getattr(service, "available", False)):
        return await _text_out("tts_unavailable")
    if not body:
        return PrivateDeliveryOutcome(delivered=book.delivered, medium="none", error="empty_text")

    try:
        segments = tuple(service.split_text(body))
    except Exception as exc:
        log.warning("private tts: 分段失败 | error=%s", exc)
        return await _text_out("split_failed")
    if not segments:
        return await _text_out("empty_segments")
    if len(segments) > MAX_PRIVATE_TTS_SEGMENTS:
        # 长文不该被无限合成：超上限直接走文字。
        log.info("private tts: 段数超上限，转文字 | segments=%d", len(segments))
        return await _text_out("too_many_segments", segments=segments)

    acquired = False
    try:
        try:
            async with asyncio.timeout(PRIVATE_TTS_ADMISSION_TIMEOUT_SECONDS):
                await _private_tts_semaphore.acquire()
                acquired = True
        except TimeoutError:
            log.info("private tts: 合成准入排队超时，本轮让位给群聊")
            return await _text_out("tts_busy", segments=segments)

        # 先读限制：命中就直接走音频文件，不浪费一次注定失败的语音条。
        restricted = await chat_restricts_voice_messages(message.bot, message.chat.id)
        mode = "audio" if restricted else "voice"
        if restricted:
            log.info("private tts: 会话限制语音条，本轮直接用音频文件 | chat=%s", message.chat.id)

        sent = 0
        last_error = ""
        for index, segment in enumerate(segments):
            result = await _synthesize_segment(
                service, segment, uid=uid, want_audio=mode == "audio"
            )
            if not result.ok or not result.audio_bytes:
                last_error = result.error or "synthesis_failed"
                log.warning("private tts: 合成失败 | segment=%d | error=%s", index + 1, last_error)
                break
            if mode == "voice":
                ok, detail = await _send_voice_segment(
                    message,
                    audio_bytes=result.audio_bytes,
                    index=index,
                    auto_delete_seconds=auto_delete_seconds,
                    receipt=book,
                    spoken_text=segment,
                )
                if not ok and is_voice_privacy_rejection(detail):
                    # 只有明确是语音隐私拒收才当场改投音频文件，而且**只补还没发出去
                    # 的那几段**——已经播出去的不重播。载体换成真 MP3。
                    log.info(
                        "private tts: 语音条被隐私设置拒收，改投音频文件 | 已送达=%d | 剩余=%d",
                        sent,
                        len(segments) - sent,
                    )
                    mode = "audio"
                    result = await _synthesize_segment(
                        service, segment, uid=uid, want_audio=True
                    )
                    if not result.ok or not result.audio_bytes:
                        last_error = result.error or "synthesis_failed"
                        break
                    ok = await _send_audio_segment(
                        message,
                        audio_bytes=result.audio_bytes,
                        index=index,
                        auto_delete_seconds=auto_delete_seconds,
                        receipt=book,
                        spoken_text=segment,
                    )
            else:
                ok = await _send_audio_segment(
                    message,
                    audio_bytes=result.audio_bytes,
                    index=index,
                    auto_delete_seconds=auto_delete_seconds,
                    receipt=book,
                    spoken_text=segment,
                )
            if not ok:
                last_error = last_error or "telegram_send_failed"
                break
            sent += 1

        if sent >= len(segments):
            log.info(
                "private tts: 投递完成 | medium=%s | segments=%d | chars=%d",
                mode,
                sent,
                len(body),
            )
            return PrivateDeliveryOutcome(
                delivered=book.delivered,
                medium=mode,
                complete=True,
                sent_segments=sent,
                requested_segments=len(segments),
            )
        return await _text_out(
            last_error or "delivery_incomplete", segments=segments, sent=sent, medium=mode
        )
    finally:
        if acquired:
            _private_tts_semaphore.release()


__all__ = [
    "DELIVERY_MARKER_EXAMPLE",
    "DELIVERY_TEXT",
    "DELIVERY_VOICE",
    "MAX_PRIVATE_TTS_SEGMENTS",
    "PRIVATE_TTS_DIRECTIVE_HEADER",
    "PRIVATE_TTS_PREFERENCE_HEADER",
    "DeliveryReceipt",
    "PrivateDeliveryOutcome",
    "PrivateReplyPlan",
    "TurnDirectives",
    "VoiceRestrictionCache",
    "build_private_tts_preference",
    "chat_restricts_voice_messages",
    "deliver_private_reply",
    "fold_owner_delivery_state",
    "is_voice_privacy_rejection",
    "load_owner_delivery_state",
    "parse_dm_delivery",
    "parse_owner_delivery_instruction",
    "parse_owner_delivery_turns",
    "resolve_delivery_directive",
    "voice_restriction_cache",
]