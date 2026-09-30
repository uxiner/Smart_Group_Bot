"""成本与健康看板：把"这几天花了多少 token、有没有吃到缓存、异常多少"变成一张表。

为什么要有它：过去这些数字只能靠翻 7 天日志现算（``docker logs | grep|awk``），
既慢又容易漏。现在 LLM 客户端每次调用都把计数交给 ``bot.services.llm_metrics``，
由它攒够一批写进 ``llm_usage_daily``，报表只是把这张表加起来。

口径（三条，写死了就别再猜）：

- ``usage_date`` 是 **Asia/Shanghai 的自然日**（和 ``member_checkins.checkin_date``
  同口径），所以窗口直接按日期字符串比较——注意这跟 ``violations.created_at``
  （UTC 朴素时间）不是一套。
- ``calls`` 只统计**成功返回**的调用；``timeouts`` / ``empty_responses`` /
  ``failures`` 是**按尝试次数**计的（一次超时后重试成功会各记一笔），
  这样才能和日志里的 "LLM timeout" 行数对上。
- ``cached_tokens`` = 命中前缀缓存的 token；``cache_write_tokens`` = 写进缓存的
  token（网关的 cache_creation_input_tokens）；``thinking_tokens`` 在关闭思考时
  应当恒为 0，不为 0 就说明"关思考"没生效，是在白烧钱。

全部只读，不参与任何主链路。
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import LlmUsageDaily
from bot.services.llm_metrics import COUNTER_FIELDS, window_start
from bot.utils.timezone import now_shanghai_naive

#: 缓存命中率低于这个值就在报表里点一句（目前是 0，即完全没复用前缀）
CACHE_TARGET_RATE = 0.30


def _esc(text: object) -> str:
    """报表正文是 Telegram HTML：任何透传字段都要转义。

    上次就是漏了这一步——「置信<0.9」里的裸尖括号让整条消息解析失败，
    报表于是"发不出去"。凡是插进 HTML 的字符串都过这个函数。
    """

    return html.escape(str(text), quote=False)


def _fmt_tokens(value: int) -> str:
    """把 token 数写成人看的：9.1M / 1.5K / 480。"""

    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value}"


@dataclass(frozen=True, slots=True)
class StageUsage:
    stage: str
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0
    empty_responses: int = 0
    timeouts: int = 0
    failures: int = 0
    parse_errors: int = 0

    @property
    def cache_rate(self) -> float:
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_tokens / self.prompt_tokens


@dataclass(frozen=True, slots=True)
class CostReport:
    days: int
    since: str
    stages: tuple[StageUsage, ...] = ()
    _totals: StageUsage | None = field(default=None, repr=False, compare=False)

    @property
    def totals(self) -> StageUsage:
        if self._totals is not None:
            return self._totals
        summed = {name: 0 for name in COUNTER_FIELDS}
        for item in self.stages:
            for name in COUNTER_FIELDS:
                summed[name] = summed.get(name, 0) + int(getattr(item, name, 0) or 0)
        return StageUsage(stage="__total__", **summed)

    @property
    def has_data(self) -> bool:
        return bool(self.stages)

    @property
    def cache_rate(self) -> float:
        total = self.totals
        if total.prompt_tokens <= 0:
            return 0.0
        return total.cached_tokens / total.prompt_tokens


async def collect_cost(
    session: AsyncSession, *, days: int = 7, today: date | None = None
) -> CostReport:
    """把窗口内的用量按阶段汇总（按 prompt token 降序）。"""

    days = max(1, int(days))
    since = window_start(days, today=today or now_shanghai_naive().date())
    rows = (
        await session.execute(
            select(
                LlmUsageDaily.stage,
                func.sum(LlmUsageDaily.calls),
                func.sum(LlmUsageDaily.prompt_tokens),
                func.sum(LlmUsageDaily.output_tokens),
                func.sum(LlmUsageDaily.cached_tokens),
                func.sum(LlmUsageDaily.cache_write_tokens),
                func.sum(LlmUsageDaily.thinking_tokens),
                func.sum(LlmUsageDaily.empty_responses),
                func.sum(LlmUsageDaily.timeouts),
                func.sum(LlmUsageDaily.failures),
                func.sum(LlmUsageDaily.parse_errors),
            )
            .where(LlmUsageDaily.usage_date >= since)
            .group_by(LlmUsageDaily.stage)
        )
    ).all()
    stages = tuple(
        sorted(
            (
                StageUsage(
                    stage=str(stage or ""),
                    calls=int(calls or 0),
                    prompt_tokens=int(prompt or 0),
                    output_tokens=int(output or 0),
                    cached_tokens=int(cached or 0),
                    cache_write_tokens=int(write or 0),
                    thinking_tokens=int(thinking or 0),
                    empty_responses=int(empty or 0),
                    timeouts=int(timeouts or 0),
                    failures=int(failures or 0),
                    parse_errors=int(parse_errors or 0),
                )
                for (
                    stage,
                    calls,
                    prompt,
                    output,
                    cached,
                    write,
                    thinking,
                    empty,
                    timeouts,
                    failures,
                    parse_errors,
                ) in rows
            ),
            key=lambda item: item.prompt_tokens,
            reverse=True,
        )
    )
    return CostReport(days=days, since=since, stages=stages)


def _share(value: int, total: int) -> str:
    if total <= 0:
        return ""
    return f"（{int(round(value * 100 / total))}%）"


def render_cost_report(report: CostReport) -> str:
    """成本报表（Telegram HTML，标题 + 可展开明细）。"""

    total = report.totals
    title = f"成本与健康 · 近 {report.days} 天"
    lines: list[str] = []
    if not report.has_data:
        lines.append("本期还没有用量记录。")
        lines.append(
            "（用量表是本次上线后才开始落的，跑过一轮 LLM 调用后再看这张表。）"
        )
        return f"<b>{title}</b>\n<blockquote expandable>{chr(10).join(lines)}</blockquote>"

    lines.append(
        f"调用 <b>{total.calls}</b> 次｜prompt <b>{_fmt_tokens(total.prompt_tokens)}</b>"
        f"｜输出 <b>{_fmt_tokens(total.output_tokens)}</b>"
    )
    lines.append(
        f"缓存命中 <b>{report.cache_rate * 100:.0f}%</b>"
        f"（{_fmt_tokens(total.cached_tokens)} / {_fmt_tokens(total.prompt_tokens)}）"
        f"｜写入缓存 {_fmt_tokens(total.cache_write_tokens)}"
    )
    thinking = total.thinking_tokens
    if thinking:
        lines.append(
            f"⚠ 思考 token <b>{_fmt_tokens(thinking)}</b> —— 关闭思考的配置没生效，在烧钱"
        )
    else:
        lines.append("思考 token 0（= 关闭思考已生效）")
    lines.append(
        f"异常：超时 <b>{total.timeouts}</b>｜空响应 <b>{total.empty_responses}</b>"
        f"｜解析失败 <b>{total.parse_errors}</b>｜请求失败 <b>{total.failures}</b>"
    )
    if report.cache_rate < CACHE_TARGET_RATE and total.prompt_tokens >= 100_000:
        lines.append(
            f"提示：缓存命中低于 {CACHE_TARGET_RATE * 100:.0f}%，"
            "前缀没有被复用；稳定系统提示可以吃到缓存价。"
        )
    if report.stages:
        lines.append("按阶段（按 prompt token 排序）：")
        for item in report.stages:
            detail = (
                f"· {_esc(item.stage or '未知')} {_fmt_tokens(item.prompt_tokens)}"
                f"{_share(item.prompt_tokens, total.prompt_tokens)}"
                f"｜调用 {item.calls}"
                f"｜缓存 {item.cache_rate * 100:.0f}%"
            )
            if item.timeouts:
                detail += f"｜超时 {item.timeouts}"
            if item.empty_responses:
                detail += f"｜空响应 {item.empty_responses}"
            if item.parse_errors:
                detail += f"｜解析失败 {item.parse_errors}"
            if item.failures:
                detail += f"｜失败 {item.failures}"
            lines.append(detail)
    lines.append("")
    lines.append(f"窗口：{report.since} 起（含当天，Asia/Shanghai 自然日）")
    return f"<b>{title}</b>\n<blockquote expandable>{chr(10).join(lines)}</blockquote>"


def cost_digest_text(report: CostReport) -> str:
    """纯文本摘要（私发给超管用；群里不发成本数据）。"""

    total = report.totals
    head = f"【成本与健康 · 近 {report.days} 天】"
    if not report.has_data:
        return head + "\n本期还没有用量记录。"
    lines = [
        head,
        f"调用 {total.calls} 次｜prompt {_fmt_tokens(total.prompt_tokens)}"
        f"｜输出 {_fmt_tokens(total.output_tokens)}",
        f"缓存命中 {report.cache_rate * 100:.0f}%｜写入缓存 {_fmt_tokens(total.cache_write_tokens)}"
        f"｜思考 token {_fmt_tokens(total.thinking_tokens)}",
        f"异常：超时 {total.timeouts}｜空响应 {total.empty_responses}"
        f"｜解析失败 {total.parse_errors}｜失败 {total.failures}",
    ]
    for item in report.stages[:5]:
        lines.append(
            f"· {_esc(item.stage or '未知')} {_fmt_tokens(item.prompt_tokens)}"
            f"（{item.calls} 次｜缓存 {item.cache_rate * 100:.0f}%）"
        )
    return "\n".join(lines)


async def render_group_cost(session: AsyncSession, *, days: int = 7) -> str:
    """一次取齐并渲染（``/cost`` 命令用）。

    命名跟 ``quality_report.render_group_quality`` 对齐：同步的 ``render_*``
    只做排版，异步的 ``render_group_*`` 负责取数——两个同名函数会互相覆盖，
    测试第一次跑就撞上了。
    """

    return render_cost_report(await collect_cost(session, days=days))


async def render_cost_digest(session: AsyncSession, *, days: int = 7) -> str:
    """一次取齐并渲染成纯文本（周报私发用）。"""

    return cost_digest_text(await collect_cost(session, days=days))
