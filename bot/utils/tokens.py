"""不用模型分词器的 token 粗估（CJK 感知）。

这个口径原来是 ``bot/services/memory.py`` 里的模块私有函数，群聊的上下文预算、
自动压缩触发、历史裁剪全都用它。私聊历史现在也要按 token 预算装配，所以把同一套
换算抽到这里当**唯一实现**：两个通道必须用同一个口径，否则「同一个 272K 预算」
在群里和私聊里会装出长度差好几倍的历史。

取舍（照原样保留，不引入新依赖、不打真实分词器）：

* CJK 字符按 **1 token/字** 估；
* 其它字符按 **~3 字符/token** 估。

中文实测约 0.49 token/字（136036 token ÷ 28 万字符），这里按 1 token/字 是**故意
高估**：预算是硬闸门，宁可早停也不能让请求真的超出模型窗口。
"""

from __future__ import annotations

import re

# CJK-family codepoints tokenize near one token per character, unlike the
# ~3 chars/token of ASCII prose. The rough prefilter must not underestimate
# Chinese chat or proactive compaction never fires before the hard budget.
_CJK_CHAR_RE = re.compile(
    "["
    "\u3000-\u30ff"  # CJK punctuation, hiragana, katakana
    "\u3400-\u4dbf"  # CJK extension A
    "\u4e00-\u9fff"  # CJK unified ideographs
    "\uac00-\ud7af"  # Hangul syllables
    "\uf900-\ufaff"  # CJK compatibility ideographs
    "\uff00-\uffef"  # full-width forms
    "]"
)


def estimate_text_tokens(text: str) -> int:
    """粗估一段文本的 prompt token 数（CJK 1 token/字，其它 ~3 字符/token）。"""

    body = str(text or "")
    cjk_chars = len(_CJK_CHAR_RE.findall(body))
    other_chars = len(body) - cjk_chars
    return cjk_chars + (other_chars + 2) // 3


def cut_text_to_tokens(text: str, limit_tokens: int) -> str:
    """把文本**从尾部**硬切到 token 上限内（保留开头）。

    历史装配、统一闸门与最终载荷裁剪都要"截断一条超长消息"，三处必须同口径，否则
    同一个 limit 切出来的长度不同。二分在 :func:`estimate_text_tokens` 上做，非 CJK
    字符约 3 字符/token，所以先在 ``3 * limit`` 字符处开窗再收敛。
    """

    if limit_tokens <= 0 or not text:
        return ""
    if estimate_text_tokens(text) <= limit_tokens:
        return text
    candidate = text[: min(len(text), limit_tokens * 3 + 3)]
    low, high = 0, len(candidate)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_text_tokens(candidate[:mid]) <= limit_tokens:
            low = mid
        else:
            high = mid - 1
    return candidate[:low]
