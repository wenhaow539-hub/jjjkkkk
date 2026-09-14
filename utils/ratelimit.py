import asyncio
import random
import time


class AsyncRateLimiter:
    """全局异步限速器：保证相邻请求的启动间隔 >= min_interval * (1 ± jitter) 秒。

    所有并发任务共享同一实例，形成全站级别的请求节奏控制，
    避免并发窗口内的请求风暴触发目标站点风控。
    """

    def __init__(self, min_interval: float = 1.2, jitter: float = 0.35):
        self._min_interval = max(min_interval, 0.0)
        self._jitter = min(max(jitter, 0.0), 0.9)
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            interval = self._min_interval * random.uniform(1 - self._jitter, 1 + self._jitter)
            slot = max(now, self._next_slot)
            self._next_slot = slot + interval
            wait = slot - now
        if wait > 0:
            await asyncio.sleep(wait)
