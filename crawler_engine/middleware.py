"""中间件层：重试策略、会话池、代理配置。

三件事：
1. 重试（RetryPolicy / delay_for / 通用 with_retry）：统一 429/5xx 的指数退避与 Retry-After 处理；
2. 会话（SessionPool / UARotator / HttpSessionState）：Crawlee 侧用其原生 SessionPool，httpx 侧用轻量会话状态；
3. 代理（ProxyConfiguration / proxy_for）：无代理时为 None，不产生任何隐式行为。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

from utils.logger import get_logger

logger = get_logger("engine.middleware")

DEFAULT_RETRY_STATUSES = (408, 425, 429, 500, 502, 503, 504)

BLOCK_MARKERS = (
    "captcha",
    "geetest",
    "sec-captcha",
    "滑块",
    "滑动验证",
    "verify you are human",
    "unusual traffic",
    "access denied",
    "are you a robot",
    "请求过于频繁",
    "访问受限",
    "安全验证",
)

DEFAULT_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)


# --------------------------------------------------------------------------- #
# 重试
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RetryPolicy:
    """指数退避重试策略，支持服务端 Retry-After 优先。"""

    max_attempts: int = 3
    base_delay: float = 2.0
    factor: float = 2.0
    max_delay: float = 60.0
    jitter: float = 0.3
    retry_statuses: tuple[int, ...] = DEFAULT_RETRY_STATUSES

    def delay_for(self, attempt: int, retry_after: str | None = None) -> float:
        """attempt 从 1 开始；retry_after 为响应头原值。"""
        server_hint = parse_retry_after(retry_after)
        backoff = min(self.base_delay * (self.factor ** max(attempt - 1, 0)), self.max_delay)
        delay = max(server_hint or 0.0, backoff)
        return round(delay * random.uniform(1 - self.jitter, 1 + self.jitter), 2)

    def should_retry(self, attempt: int, status: int | None = None, exc: BaseException | None = None) -> bool:
        if attempt >= self.max_attempts:
            return False
        if status is not None:
            return status in self.retry_statuses
        return exc is not None


def parse_retry_after(value: str | None) -> Optional[float]:
    if not value:
        return None
    try:
        return max(float(value.strip()), 0.0)
    except ValueError:
        return None


def is_blocked(status: int, body: str | None = None) -> bool:
    """判定响应是否属于风控拦截（状态码 + 页面特征双判定）。"""
    if status in (403, 429, 503):
        return True
    if not body:
        return False
    low = body[:20000].lower()
    return any(marker in low for marker in BLOCK_MARKERS)


def blocked_reason(status: int, body: str | None = None) -> str:
    if status in (403, 429, 503):
        return f"HTTP_{status}"
    if body:
        low = body[:20000].lower()
        for marker in BLOCK_MARKERS:
            if marker in low:
                return f"page_marker:{marker}"
    return "unknown"


async def with_retry(
    func: Callable[[int], Awaitable[Any]],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, float, str], None] | None = None,
) -> Any:
    """按 RetryPolicy 重试异步调用；func 接收 attempt(从 1 开始)。

    约定：func 内部遇到可重试情况时抛出 RetryableError，其它异常直接向上抛。
    """
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await func(attempt)
        except RetryableError as e:
            last_exc = e
            if not policy.should_retry(attempt, status=e.status):
                raise
            delay = policy.delay_for(attempt, e.retry_after)
            if on_retry:
                on_retry(attempt, delay, str(e))
            await asyncio.sleep(delay)
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry 未执行任何尝试")


class RetryableError(Exception):
    """标记"值得重试"的异常，携带可选状态码与 Retry-After。"""

    def __init__(self, message: str, *, status: int | None = None, retry_after: str | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
@dataclass
class HttpSessionState:
    """httpx 抓取用的轻量会话状态：UA + Cookie + 使用计数 + 冷却。"""

    user_agent: str
    cookies: dict[str, str] = field(default_factory=dict)
    usage_count: int = 0
    max_usage: int = 50
    created_at: float = field(default_factory=time.time)
    blocked_count: int = 0

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        base = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
        }
        if extra:
            base.update(extra)
        return base

    @property
    def needs_rotation(self) -> bool:
        return self.usage_count >= self.max_usage or self.blocked_count >= 3


class UARotator:
    """User-Agent 轮换器（默认仅在会话轮换时更换，保持同一 IP 下指纹稳定）。"""

    def __init__(self, user_agents: Iterable[str] | None = None):
        self._uas = tuple(user_agents) if user_agents else DEFAULT_USER_AGENTS
        self._index = 0

    def next(self) -> str:
        ua = self._uas[self._index % len(self._uas)]
        self._index += 1
        return ua


def build_session_pool(config):
    """构建 Crawlee 原生 SessionPool（供 PlaywrightCrawler / HttpCrawler 使用）。"""
    if not config.use_session_pool:
        return None
    try:
        from crawlee.sessions import SessionPool
    except ImportError:  # pragma: no cover - crawlee 未安装时的降级
        logger.warning("⚠️ [session] crawlee.sessions 不可用，跳过会话池配置。")
        return None
    return SessionPool(max_pool_size=config.session_pool_size)


# --------------------------------------------------------------------------- #
# 代理
# --------------------------------------------------------------------------- #
def build_proxy_configuration(config):
    """构建 Crawlee ProxyConfiguration；未配置代理时返回 None（不产生隐式代理行为）。"""
    if not config.proxy_urls and not config.tiered_proxy_urls:
        return None
    from crawlee.proxy_configuration import ProxyConfiguration

    kwargs: dict[str, Any] = {}
    if config.proxy_urls:
        kwargs["proxy_urls"] = list(config.proxy_urls)
    if config.tiered_proxy_urls:
        kwargs["tiered_proxy_urls"] = dict(config.tiered_proxy_urls)
    return ProxyConfiguration(**kwargs)


def proxy_for(config, index: int = 0) -> Optional[str]:
    """为 httpx 请求挑选代理 URL（按顺序轮换；未配置返回 None）。"""
    if config.proxy_urls:
        return config.proxy_urls[index % len(config.proxy_urls)]
    return None


def build_crawlee_middleware_kwargs(config) -> dict[str, Any]:
    """把会话池 / 代理中间件合并成可传给 crawlee 构造器的 kwargs。"""
    kwargs: dict[str, Any] = {}
    session_pool = build_session_pool(config)
    if session_pool is not None:
        kwargs["session_pool"] = session_pool
        kwargs["use_session_pool"] = True
    proxy_configuration = build_proxy_configuration(config)
    if proxy_configuration is not None:
        kwargs["proxy_configuration"] = proxy_configuration
    return kwargs
