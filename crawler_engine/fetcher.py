"""HTTP / API 请求封装：限速 + robots 合规 + 指数退避重试 + 会话轮换 + 代理。

定位：把项目里散落在各爬虫内部的 httpx 逻辑（限速、429 退避、robots 检查、UA/Cookie 管理）
收敛成一层可复用的抓取器，供：
- Crawlee 的 http / soup 模式在需要额外接口调用时复用；
- 现有 legacy 爬虫按需逐步替换自研请求代码（渐进式迁移，不必一次性重写）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import httpx

from crawler_engine.config import EngineConfig
from crawler_engine.middleware import (
    HttpSessionState,
    RetryPolicy,
    UARotator,
    blocked_reason,
    is_blocked,
    parse_retry_after,
    proxy_for,
)
from utils.logger import get_logger
from utils.ratelimit import AsyncRateLimiter
from utils.robots import RobotsChecker

logger = get_logger("engine.fetcher")


@dataclass
class FetchResponse:
    """结构化抓取结果：失败不抛异常，而是带上 error 字段，由调用方决定如何处理。"""

    url: str
    status: int = 0
    text: str = ""
    final_url: str = ""
    headers: dict = field(default_factory=dict)
    elapsed_ms: float = 0.0
    error: str = ""
    attempts: int = 0
    blocked: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error and not self.blocked

    def json(self) -> Any:
        return json.loads(self.text)

    def brief(self) -> str:
        if self.ok:
            return f"200 OK ({self.elapsed_ms:.0f}ms, {len(self.text)}B, {self.attempts} 次尝试)"
        return f"失败 status={self.status} error={self.error} blocked={self.blocked} ({self.attempts} 次尝试)"


class AsyncFetcher:
    """统一的异步 HTTP 抓取器。可注入外部 client / 限速器，便于与老代码共享节奏控制。"""

    def __init__(
        self,
        config: EngineConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        rate_limiter: AsyncRateLimiter | None = None,
        robots_checker: RobotsChecker | None = None,
        session: HttpSessionState | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.config = config or EngineConfig.from_env()
        self.retry_policy = retry_policy or RetryPolicy(
            max_attempts=self.config.max_request_retries,
            base_delay=2.0,
        )
        self._external_client = client
        self._client = client
        self._owned_client = False
        self.session = session or HttpSessionState(user_agent=UARotator().next())
        self._ua_rotator = UARotator()
        self.rate_limiter = rate_limiter or AsyncRateLimiter(
            min_interval=self.config.min_request_interval, jitter=self.config.jitter
        )
        self.robots = robots_checker or RobotsChecker()
        self.stats = {"requests": 0, "blocked": 0, "retries": 0, "errors": 0}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "AsyncFetcher":
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=True, timeout=self.config.request_timeout)
            self._owned_client = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owned_client = False

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=True, timeout=self.config.request_timeout)
            self._owned_client = True
        return self._client

    # ------------------------------------------------------------------ #
    # 会话
    # ------------------------------------------------------------------ #
    def rotate_session(self, reason: str = "") -> None:
        old_ua = self.session.user_agent
        self.session = HttpSessionState(
            user_agent=self._ua_rotator.next(),
            cookies={} if self.session.blocked_count else dict(self.session.cookies),
        )
        logger.warning(f"🔄 [会话轮换] {reason or '手动触发'}：已切换会话指纹（UA: ...{old_ua[-24:]}）")

    # ------------------------------------------------------------------ #
    # 核心请求
    # ------------------------------------------------------------------ #
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
        json_body: Any = None,
        check_robots: bool = True,
        max_retries: int | None = None,
    ) -> FetchResponse:
        if check_robots and self.config.respect_robots:
            if not await self.robots.can_fetch(self.client, url):
                logger.warning(f"🤖 [robots] {url} 被 robots.txt 禁止抓取，已跳过。")
                return FetchResponse(url=url, status=0, error="blocked_by_robots")

        attempts_allowed = max_retries if max_retries is not None else self.retry_policy.max_attempts
        last = FetchResponse(url=url)
        block_retries = 0

        for attempt in range(1, attempts_allowed + 1):
            await self.rate_limiter.acquire()
            self.stats["requests"] += 1
            started = time.perf_counter()
            proxy = proxy_for(self.config, self.stats["requests"])

            try:
                resp = await self.client.request(
                    method,
                    url,
                    headers=self.session.headers(dict(headers or {})),
                    params=params,
                    data=data,
                    json=json_body,
                    cookies=self.session.cookies or None,
                    timeout=self.config.request_timeout,
                    **({"proxy": proxy} if proxy else {}),
                )
            except Exception as e:
                self.stats["errors"] += 1
                last = FetchResponse(
                    url=url, status=0, error=f"{e.__class__.__name__}: {e}",
                    elapsed_ms=(time.perf_counter() - started) * 1000, attempts=attempt,
                )
                logger.warning(f"⚠️ [fetch] {url} 请求异常({last.error}) 第 {attempt}/{attempts_allowed} 次")
                if attempt < attempts_allowed:
                    await self._sleep_backoff(attempt, None)
                continue

            elapsed_ms = (time.perf_counter() - started) * 1000
            text = resp.text
            self.session.usage_count += 1
            if resp.cookies:
                self.session.cookies.update(dict(resp.cookies))

            blocked = is_blocked(resp.status_code, text)
            last = FetchResponse(
                url=url, status=resp.status_code, text=text, final_url=str(resp.url),
                headers=dict(resp.headers), elapsed_ms=elapsed_ms, attempts=attempt, blocked=blocked,
            )
            if resp.status_code == 200 and not blocked:
                return last

            if blocked:
                self.stats["blocked"] += 1
                self.session.blocked_count += 1
                reason = blocked_reason(resp.status_code, text)
                logger.warning(f"🚧 [fetch] 触发风控 {reason} -> {url} (第 {attempt}/{attempts_allowed} 次)")
                if block_retries < 1 and attempt < attempts_allowed:
                    block_retries += 1
                    self.rotate_session(reason=reason)
                    continue
                return last

            if resp.status_code in self.retry_policy.retry_statuses and attempt < attempts_allowed:
                last.error = f"HTTP_{resp.status_code}"
                await self._sleep_backoff(attempt, resp.headers.get("retry-after"))
                continue

            last.error = f"HTTP_{resp.status_code}"
            logger.debug(f"[fetch] {url} 非重试状态码 {resp.status_code}")
            return last

        return last

    async def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self.retry_policy.delay_for(attempt, retry_after)
        self.stats["retries"] += 1
        logger.debug(f"[fetch] 退避 {delay}s (第 {attempt} 次失败, Retry-After={parse_retry_after(retry_after)})")
        import asyncio

        await asyncio.sleep(delay)

    # ------------------------------------------------------------------ #
    # 便捷方法
    # ------------------------------------------------------------------ #
    async def get(self, url: str, **kwargs: Any) -> FetchResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> FetchResponse:
        return await self.request("POST", url, **kwargs)

    async def request_json(self, url: str, *, method: str = "GET", **kwargs: Any) -> FetchResponse:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json, text/plain, */*")
        return await self.request(method, url, headers=headers, **kwargs)

    def summary(self) -> str:
        return (
            f"请求 {self.stats['requests']} | 风控命中 {self.stats['blocked']} | "
            f"重试 {self.stats['retries']} | 异常 {self.stats['errors']}"
        )
