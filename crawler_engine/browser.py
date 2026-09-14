"""浏览器管理：托管浏览器（Playwright + 本机 Chrome）与 CDP 附着（复用已登录的可见 Chrome）。

为什么需要这个模块：
- Crawlee 原生的 PlaywrightBrowserPlugin 只会 launch 新浏览器，无法附着到"用户手动登录 / 手动过验证码"
  的现有 Chrome（crawlee 1.10 仅在 Stagehand 里用到 connect_over_cdp）；
- 现有项目的 GlobalSources / 天眼查流程依赖可见 Chrome + 人工过验证码，因此这里提供 CdpBrowserPlugin，
  让 Crawlee 的调度、重试、会话能力可以跑在"已登录的真实浏览器"上，属于渐进式迁移的关键衔接件。

安全约束：CdpBrowserController.close() 只关闭本次爬取打开的页面，绝不调用 browser.close()，
因此不会杀掉用户的 Chrome 进程、不会丢失登录态。
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from crawlee.browsers import BrowserPool, PlaywrightBrowserController, PlaywrightBrowserPlugin

from utils.logger import get_logger

logger = get_logger("engine.browser")

DEFAULT_CDP_PORT = 9222


def cdp_is_reachable(cdp_url: str, timeout: float = 1.5) -> bool:
    """TCP 探测 CDP 调试端口是否已就绪。"""
    parsed = urlparse(cdp_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or DEFAULT_CDP_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def ensure_cdp_chrome(port: int = DEFAULT_CDP_PORT, profile_dir: str | None = None,
                      platform_name: str = "CrawlerEngine") -> str:
    """确保本机存在可调试的 Chrome 并返回其 CDP 地址（复用项目原有的 core.browser 逻辑）。"""
    from core.browser import ensure_chrome_running, is_port_open

    if not is_port_open(port=port):
        kwargs = {"port": port, "platform_name": platform_name}
        if profile_dir:
            kwargs["profile_dir"] = profile_dir
        ensure_chrome_running(**kwargs)
    return f"http://127.0.0.1:{port}"


class CdpBrowserController(PlaywrightBrowserController):
    """附着在已有 Chrome 上的控制器：默认复用其默认上下文（即用户的登录态）。"""

    def __init__(self, browser, *, use_default_context: bool = True,
                 max_open_pages_per_browser: int = 20) -> None:
        super().__init__(
            browser,
            max_open_pages_per_browser=max_open_pages_per_browser,
            use_incognito_pages=False,
            fingerprint_generator=None,  # 真实浏览器保留原生指纹，不注入合成指纹
        )
        self._use_default_context = use_default_context

    async def new_page(self, browser_new_context_options=None, proxy_info=None):
        """默认上下文路径：新页签直接开在用户已登录的 Chrome 上下文里。"""
        if not (self._use_default_context and browser_new_context_options is None and proxy_info is None):
            return await super().new_page(browser_new_context_options, proxy_info)

        if not self.has_free_capacity:
            raise ValueError("Cannot open more pages in this browser.")

        self._opening_pages_count += 1
        try:
            if self._browser.contexts:
                context = self._browser.contexts[0]
            else:
                context = await self._browser.new_context()
            page = await context.new_page()
            page.on(event="close", f=self._on_page_close)
            self._pages.append(page)
            self._last_page_opened_at = datetime.now(timezone.utc)
            self._total_opened_pages += 1
        finally:
            self._opening_pages_count -= 1
        return page

    async def close(self, *, force: bool = False) -> None:
        """断开调试连接：只关本次打开的页签，绝不关闭用户的 Chrome。"""
        if self.pages_count > 0 and not force:
            raise ValueError("Cannot close the browser while there are open pages.")

        for page in list(self._pages):
            try:
                await page.close()
            except Exception as e:  # 页面可能已被用户手动关闭
                logger.debug(f"[CDP] 关闭页签失败(忽略): {e!r}")
        self._pages.clear()
        logger.info("🔌 [CDP] 已断开调试连接（用户的 Chrome 进程与登录态保持不变）。")


class CdpBrowserPlugin(PlaywrightBrowserPlugin):
    """让 Crawlee 使用已存在的 Chrome（connect_over_cdp）而非新起一个浏览器。"""

    AUTOMATION_LIBRARY = "playwright"

    def __init__(self, cdp_url: str, *, use_default_context: bool = True,
                 max_open_pages_per_browser: int = 20) -> None:
        super().__init__(
            browser_type="chromium",
            max_open_pages_per_browser=max_open_pages_per_browser,
            use_incognito_pages=False,
            fingerprint_generator=None,
        )
        self._cdp_url = cdp_url
        self._use_default_context = use_default_context

    async def new_browser(self) -> CdpBrowserController:
        if not self._playwright:
            raise RuntimeError("CdpBrowserPlugin 尚未初始化，请通过 async with 使用。")
        logger.info(f"🔌 [CDP] 附着到已有浏览器: {self._cdp_url}")
        browser = await self._playwright.chromium.connect_over_cdp(self._cdp_url)
        return CdpBrowserController(
            browser,
            use_default_context=self._use_default_context,
            max_open_pages_per_browser=self._max_open_pages_per_browser,
        )


def build_browser_pool(config, *, force_cdp: bool = False) -> BrowserPool:
    """按配置构建浏览器池：CDP 附着 或 托管浏览器（本机 Chrome / 内置 Chromium）。"""
    mode = config.resolved_mode()
    if force_cdp or mode == "browser-cdp":
        cdp_url = config.cdp_url or f"http://127.0.0.1:{DEFAULT_CDP_PORT}"
        plugin = CdpBrowserPlugin(
            cdp_url,
            use_default_context=True,
            max_open_pages_per_browser=config.max_open_pages_per_browser,
        )
    else:
        # 显式传 launch options，避免依赖 service_locator 的全局 configuration 时序
        plugin = PlaywrightBrowserPlugin(
            browser_type=config.browser_type,
            user_data_dir=config.user_data_dir,
            browser_launch_options={"headless": config.headless},
            max_open_pages_per_browser=config.max_open_pages_per_browser,
        )
    return BrowserPool(plugins=[plugin])


def build_crawler_browser_kwargs(config) -> dict:
    """生成 PlaywrightCrawler 的浏览器相关参数（提供 browser_pool 时不应再传 browser_type）。"""
    return {
        "browser_pool": build_browser_pool(config),
        "navigation_timeout": timedelta(seconds=config.navigation_timeout),
    }


STEALTH_INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
"""隐藏 webdriver 特征（原 legacy 爬虫写在 GlobalSources.scrape 里，现上移到引擎层）。"""

BLOCK_EXTRA_URL_PATTERNS = ("google-analytics", "doubleclick", "sensorsdata")
"""在 Crawlee 默认拦截（图片/字体/媒体）之外，额外拦截统计与广告脚本。"""


def build_pre_navigation_hook(config, limiter=None):
    """返回页面导航前的处理钩子：请求节奏控制 + 反侦测补丁 + 资源拦截。

    归属说明：这些是**浏览器层**职责，必须由引擎负责，Adapter 内不得出现
    （Adapter 只提供 URL、解析页面、返回结构化数据）。

    limiter：`utils.ratelimit.AsyncRateLimiter` 实例，由引擎按 robots.txt 的
    Crawl-delay 构造。在这里 acquire() 的意义是——**相邻请求间隔成为硬约束**，
    并且对 429 之后的重试同样生效（重试会重新创建页面并再次走这个钩子），
    从而避免"被限流 → 立刻重试 → 更狠地被限流"的放大效应。
    """

    async def _hook(context) -> None:
        if limiter is not None:
            try:
                await limiter.acquire()
            except Exception as e:  # 限速器本身故障不应阻断抓取
                logger.debug(f"[pacing] 限速等待失败(忽略): {e!r}")

        page = getattr(context, "page", None)
        if page is None:
            return

        if config.stealth_patch:
            try:
                await page.add_init_script(STEALTH_INIT_SCRIPT)
            except Exception as e:
                logger.debug(f"[stealth] 注入初始化脚本失败(忽略): {e!r}")

        if config.block_resources:
            block = getattr(context, "block_requests", None)
            if block is None:
                logger.debug("[block] 当前上下文不支持 block_requests，跳过资源拦截。")
                return
            try:
                extra = list(config.block_extra_patterns) or list(BLOCK_EXTRA_URL_PATTERNS)
                await block(extra_url_patterns=extra)
            except Exception as e:
                logger.warning(f"⚠️ [block] 资源拦截设置失败(忽略): {e!r}")

    return _hook
