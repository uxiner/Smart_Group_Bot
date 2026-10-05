"""``/av`` 查询的私聊限流（纯逻辑，不碰 Telegram 路由）。

历史：这里原本还带着「发图识图反查番号」的挑图/下载/抽编号工具。该功能
2026-10-03 整块作废并清理（用户口径：反查识图先作废；群内不允许任何 NSFW 图/视频，
与是否开 /av 无关），所以本模块只剩还在用的两件事：

1. :class:`AVPrivateRateLimiter` —— 私聊 ``/av`` 文字查询的内存滑窗限流；
2. :func:`rate_limit_minutes` —— 把剩余秒数换算成回复里的分钟数。

刻意不含任何图片处理、也不含任何外部服务调用。
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable


class AVPrivateRateLimiter:
    """内存滑窗限流：每个用户每小时最多 ``limit`` 次私聊 ``/av`` 查询。

    取舍：进程重启清零（可接受）。只用单调时钟，不依赖系统时间；全部操作都是
    同步的，单事件循环里不会出现 await 断层。
    """

    def __init__(
        self,
        *,
        limit: int = 10,
        window_seconds: float = 3600.0,
        max_users: int = 4096,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_users = max(16, int(max_users))
        self._clock = clock or time.monotonic
        self._hits: dict[int, deque[float]] = {}

    def _prune(self, user_id: int, now: float) -> deque[float] | None:
        """返回该用户的滑窗；字典已达**硬上限**且无法腾出位置时返回 ``None``。

        B-05：原来这里「满了先清一次，然后无论清没清出位置都插入新 key」，
        于是 ``max_users`` 只是「触发一次清理的机会」，不是内存上界。现在：
        清理后仍达上限就**不建新桶**，调用方按「不计数」处理（放行、不限流），
        既守住内存上界，又不因限流器自身而拒绝用户请求。
        """

        hits = self._hits.get(user_id)
        if hits is None:
            self._drop_idle_users()
            if len(self._hits) >= self.max_users:
                return None
            hits = deque()
            self._hits[user_id] = hits
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def _drop_idle_users(self) -> None:
        """字典只增不减会随用户数慢慢涨；满了先清掉已经过期的空队列。

        注意这只在「腾得动」时有效：全是活跃用户时一个也删不掉，所以
        :meth:`_prune` 之后仍要复查 ``len(self._hits) >= self.max_users``。
        """

        if len(self._hits) < self.max_users:
            return
        cutoff = self._clock() - self.window_seconds
        stale = [
            uid for uid, hits in self._hits.items() if not hits or hits[-1] <= cutoff
        ]
        for uid in stale:
            self._hits.pop(uid, None)

    def _retry_after(self, hits: deque[float], now: float) -> int:
        if not hits:
            return 0
        remaining = self.window_seconds - (now - hits[0])
        return max(1, int(math.ceil(remaining)))

    def blocked(self, user_id: int) -> tuple[bool, int]:
        """只查询、不计数（给「点按钮触发外部抓取」这类路径用）。"""

        uid = int(user_id or 0)
        if uid <= 0:
            return False, 0
        now = self._clock()
        hits = self._prune(uid, now)
        if hits is None:
            # 容量已满且腾不出位置：无法判断这个用户的额度 → 不限流。
            return False, 0
        if len(hits) < self.limit:
            return False, 0
        return True, self._retry_after(hits, now)

    def allow(self, user_id: int) -> tuple[bool, int]:
        """查询并在放行时计一次数；被拒时返回建议等待秒数。"""

        uid = int(user_id or 0)
        if uid <= 0:
            return True, 0
        now = self._clock()
        hits = self._prune(uid, now)
        if hits is None:
            return True, 0
        if len(hits) >= self.limit:
            return False, self._retry_after(hits, now)
        hits.append(now)
        return True, 0


def rate_limit_minutes(retry_after_seconds: int) -> int:
    """把剩余秒数换算成回复里的分钟数（至少 1 分钟）。"""

    return max(1, int(math.ceil(max(0, int(retry_after_seconds)) / 60.0)))
