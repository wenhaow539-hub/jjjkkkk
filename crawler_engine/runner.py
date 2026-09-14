"""Crawlee 统一入口。

四种使用方式（渐进式迁移的四档）：
1. Adapter 模式（推荐给现有爬虫）：Adapter 提供 start_urls() + handle(context)，交给 CrawlRunner 执行
       runner = CrawlRunner(config)
       await runner.run(SomeAdapter())
2. 新抓取任务直接用统一底座的 handler 模式：CrawlerRunner().run(handler, start_urls, mode="http"|"soup"|"browser"|"browser-cdp")
3. 现有 legacy 爬虫不改代码，先接入统一入口执行：CrawlerRunner().run_platform(platform, keyword, limit)
4. 单个爬虫迁移就绪后，在类上声明 engine_mode + engine_start_urls（可选 engine_handler），
   即可由同一入口自动切换到 Crawlee 底座，无需改动其它爬虫。

Adapter 契约：
    start_urls()  -> Sequence[str]（同步/异步/属性均可，会在入队前完成解析）
    handle(context) -> Awaitable[None]（需要页面的适配器用 context.page）
    可选：router（crawlee Router）、engine_mode、platform_name
    可选：requires_browser = True（声明依赖浏览器上下文，避免被 http/soup 模式误用）

命令行：
    python -m crawler_engine --adapter adapters.globalsources:GlobalSourcesAdapter --engine browser
    python -m crawler_engine --platform globalsources --keyword phone -n 30 --engine legacy
    python -m crawler_engine --url https://example.com --engine http
    python -m crawler_engine --discover "https://www.globalsources.com/searchList/suppliers?keyWord=phone"
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import re
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

from crawler_engine.config import EngineConfig
from crawler_engine.middleware import build_crawlee_middleware_kwargs
from utils.logger import get_logger

logger = get_logger("engine.runner")

# 这两条是 crawlee 在"没启用 ThrottlingRequestManager"时打的提示。
# 对本项目已经过时甚至会误导读日志的人：
#   1) crawl-delay —— 引擎已经自己实现（见 CrawlerRunner.resolve_pacing），并非"没人执行"；
#   2) 隐式创建 event manager —— 引擎已显式创建并注入（见 _ensure_services），不应再出现。
# 用日志过滤器屏蔽掉，保留其余 crawlee 日志（例如真实的 429 告警）。
_CRAWLEE_NOISE = (
    "Crawl-delay directives from robots.txt will not be enforced",
    "Implicit creation of event manager",
    "Implicit creation of storage client",
)


class _CrawleeNoiseFilter:
    """按消息关键字丢弃 crawlee 的过时提示，避免干扰真正需要关注的日志。"""

    def filter(self, record) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not any(noise in message for noise in _CRAWLEE_NOISE)


def _install_crawlee_noise_filter() -> None:
    """把噪声过滤器挂到所有可能输出 crawlee 记录的地方。

    logging 的语义坑：挂在父 logger 上的 filter **不会**作用于子 logger 产生的记录；
    而本项目自己的日志器是 `jjkk`（propagate=False），crawlee 的记录往上冒泡后
    没有匹配的 handler，最终落到 root 的 `lastResort`（它不在 root.handlers 里）。
    因此三种目标都要挂：logger 本身、其 handler、以及 lastResort。
    """
    import logging as _logging

    targets: list = []
    root = _logging.getLogger()
    targets += [root, *root.handlers]
    last_resort = getattr(_logging, "lastResort", None)
    if last_resort is not None:
        targets.append(last_resort)
    for name, obj in list(_logging.Logger.manager.loggerDict.items()):
        if isinstance(obj, _logging.Logger) and name.startswith("crawlee"):
            targets += [obj, *obj.handlers]

    for target in targets:
        add_filter = getattr(target, "addFilter", None)
        if add_filter is None:
            continue
        if not any(isinstance(f, _CrawleeNoiseFilter) for f in getattr(target, "filters", ())):
            add_filter(_CrawleeNoiseFilter())


_SERVICES_READY = False


def _ensure_services(config: EngineConfig) -> Any:
    """显式初始化 crawlee 的全局 Configuration / EventManager。

    必须在"打开任何存储"之前调用：crawlee 在被首次使用时若发现这些为空会隐式创建并打印告警
    （实测出现在 open_queue 之前）。显式创建后行为一致但没有噪声，也不会因隐式创建
    而隐性覆盖我们传入的 configuration。
    注意 crawlee 只允许设置一次全局 Configuration，重复调用会抛 ServiceConflictError，
    此时复用已有的即可（本次执行仍以显式传入的 configuration= 为准）。
    """
    global _SERVICES_READY

    from crawlee import service_locator
    from crawlee.events import LocalEventManager

    _install_crawlee_noise_filter()

    configuration = config.to_configuration()
    try:
        service_locator.set_configuration(configuration)
    except Exception as e:
        logger.debug(f"[runner] 全局 Configuration 已存在({e.__class__.__name__})，本次沿用显式参数。")

    if not _SERVICES_READY:
        try:
            service_locator.set_event_manager(LocalEventManager())
            _SERVICES_READY = True
        except Exception as e:
            logger.debug(f"[runner] EventManager 初始化跳过: {e.__class__.__name__}")
    return configuration

ENGINE_MODES = ("auto", "http", "soup", "browser", "browser-cdp", "legacy")


@dataclass
class RunResult:
    """统一执行结果：legacy 与 Crawlee 路径返回同一结构，便于上层无差别消费。"""

    mode: str
    status: str = "ok"
    platform: str = ""
    keyword: str = ""
    items: int = 0
    requests_total: int = 0
    requests_finished: int = 0
    requests_failed: int = 0
    duration_s: float = 0.0
    errors: list = field(default_factory=list)
    items_data: list = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"mode={self.mode}",
            f"status={self.status}",
            f"线索={self.items}",
        ]
        if self.requests_total:
            parts.append(f"请求={self.requests_finished}/{self.requests_total} 成功")
        if self.requests_failed:
            parts.append(f"失败={self.requests_failed}")
        parts.append(f"耗时={self.duration_s:.1f}s")
        if self.errors:
            parts.append(f"错误={len(self.errors)}")
        return " | ".join(parts)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("items_data", None)
        return data


def _stat(stats: Any, *names: str, default: int = 0) -> int:
    """兼容不同 crawlee 版本的统计字段命名。"""
    for name in names:
        value = getattr(stats, name, None)
        if isinstance(value, (int, float)):
            return int(value)
    return default


def _first_url(items: Sequence[Any] | None) -> str:
    """从起始请求里取出第一个可用的 URL 字符串（用于 robots/crawl-delay 解析）。"""
    for item in items or ():
        raw = getattr(item, "url", item)
        if isinstance(raw, str) and raw.startswith("http"):
            return raw
    return ""


class CrawlerRunner:
    """Crawlee 统一执行入口。"""

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig.from_env()

    # ------------------------------------------------------------------ #
    # 0) 请求节奏：服从 robots.txt 的 Crawl-delay
    # ------------------------------------------------------------------ #
    async def resolve_pacing(self, config: EngineConfig, urls: Sequence[Any] | None) -> EngineConfig:
        """按 robots.txt 声明的 Crawl-delay 调整请求节奏，返回（可能是新的）配置。

        为什么必须自己做：crawlee 只在启用 `ThrottlingRequestManager` 时才会执行 crawl-delay，
        否则仅打印一条"不会被强制执行"的提示；而该组件在本项目的"处理中动态入队"场景会提前
        结束爬取、与 keep_alive 组合还会挂死，所以保持关闭。结果是**这条保护实际没人执行**：
        实测 Global Sources 的 robots.txt 对具名爬虫声明 Crawl-delay: 10~20 秒，
        而我们跑到约 1.67 请求/秒，随即被 429/403 连续拒绝。

        robots 未声明时不做任何改动（沿用 min_request_interval），失败也绝不阻断抓取。
        """
        if not (config.respect_robots and config.respect_crawl_delay):
            logger.info(
                f"⏳ [pacing] 已关闭 crawl-delay 服从（--no-crawl-delay），请求间隔 {config.effective_interval():.1f}s。"
            )
            return config

        url = _first_url(urls)
        if not url:
            return config

        from utils.robots import resolve_crawl_delays

        from crawler_engine.middleware import DEFAULT_USER_AGENT

        host = urlparse(url).netloc
        declared, bot_hint = await resolve_crawl_delays(url, user_agent=DEFAULT_USER_AGENT)
        base = max(float(config.min_request_interval), 0.05)

        if not declared:
            if bot_hint:
                # 站点只对具名爬虫声明了间隔——那不是对我们的约束，但值得知道，
                # 因为它是判断"多快算太快"唯一的客观依据（GS 的检索页与详情子域都是 10s）。
                logger.info(
                    f"⏳ [pacing] {host} 未对本客户端声明 crawl-delay"
                    f"（仅对具名爬虫声明 {bot_hint:g}s）→ 使用自身间隔 {base:.1f}s"
                    f"（约 {60 / base:.1f} 请求/分钟）。若遇到 429/403，请用 "
                    f"--interval {max(bot_hint, base):g} 放慢到站点对具名爬虫的口径。"
                )
            else:
                logger.info(
                    f"⏳ [pacing] {host} 未声明 crawl-delay，使用自身间隔 {base:.1f}s"
                    f"（约 {60 / base:.1f} 请求/分钟）。"
                )
            # 必须能"重置"：本方法按批次的 URL 主机解析，换到没声明的域时要退回自身间隔，
            # 否则会把上一个域要求的长间隔带到这批 URL 上，白慢好几倍。
            return replace(config, request_interval=None)

        interval = max(base, float(declared))
        if config.max_crawl_delay and interval > float(config.max_crawl_delay):
            logger.warning(
                f"⚠️ [pacing] {host} 声明 Crawl-delay={declared:g}s，"
                f"但被 max_crawl_delay={config.max_crawl_delay:g}s 封顶 → **未完全遵守**，请自行确认合规。"
            )
            interval = float(config.max_crawl_delay)

        logger.warning(
            f"⏳ [pacing] {host} 声明 Crawl-delay={declared:g}s → 请求间隔 {interval:.1f}s"
            f"（约 {60 / interval:.1f} 请求/分钟）。crawl-delay 是按主机声明的约束，必须遵守。"
        )
        return replace(config, request_interval=interval)

    # ------------------------------------------------------------------ #
    # 1) Crawlee handler 模式
    # ------------------------------------------------------------------ #
    async def run(
        self,
        handler: Callable,
        start_urls: Optional[Sequence[str]] = None,
        *,
        mode: str | None = None,
        requests: Optional[Sequence[Any]] = None,
        pre_navigation_hooks: Optional[Sequence[Callable]] = None,
        **config_overrides: Any,
    ) -> RunResult:
        config = self.config.with_(**config_overrides) if config_overrides else self.config
        resolved = mode or config.resolved_mode()
        if resolved == "legacy":
            raise ValueError("mode='legacy' 请使用 run_legacy() / run_platform()")

        errors: list[str] = []

        async def wrapped(context):
            url = getattr(getattr(context, "request", None), "url", "?")
            try:
                return await handler(context)
            except Exception as e:
                errors.append(f"{url}: {e.__class__.__name__}: {e}")
                logger.error(f"❌ [runner] 请求处理失败 {url}: {e!r}")
                raise  # 交回 Crawlee 计数与重试

        payload = list(requests) if requests else list(start_urls or ())
        # 请求节奏：服从 robots.txt 声明的 Crawl-delay（必须在构建 crawler 之前解析）
        config = await self.resolve_pacing(config, payload)
        crawler = await self._build_crawler(resolved, wrapped, config, payload,
                                            pre_navigation_hooks=pre_navigation_hooks)
        _install_crawlee_noise_filter()  # crawler 已建好，其自身 logger 此时才存在于 registry 中
        logger.info(f"🧭 [runner] 启动 Crawlee 模式={resolved} | {config.describe(resolved)}")
        logger.info(f"🧭 [runner] 起始请求数: {len(payload)}")
        memory_hint = config.memory_guard_message()
        if memory_hint:
            logger.warning(f"⚠️ [runner] {memory_hint}")

        started = time.perf_counter()
        run_error: str | None = None
        stats = None
        try:
            stats = await crawler.run(payload)
        except Exception as e:
            # 与 CrawlRunner 一致：浏览器被系统压死 / 站点封禁等不应裸崩，而是记录后正常返回
            run_error = f"{type(e).__name__}: {e}"
            errors.append(run_error)
            logger.error(f"❌ [runner] 爬取过程异常终止: {run_error}")
        duration = time.perf_counter() - started

        result = RunResult(
            mode=resolved,
            status="ok" if not errors else "partial",
            requests_total=_stat(stats, "requests_total", "requests_total_count",
                                 default=max(len(payload), 0)),
            requests_finished=_stat(stats, "requests_finished", "requests_finished_count"),
            requests_failed=_stat(stats, "requests_failed", "requests_failed_count",
                                  default=1 if run_error else 0),
            duration_s=duration,
            errors=errors,
        )
        if run_error and result.requests_finished == 0:
            result.status = "failed"
        logger.info(f"✅ [runner] Crawlee 执行完成: {result.summary()}")
        return result

    async def _build_crawler(self, mode: str, handler: Callable, config: EngineConfig,
                             start_urls: Sequence[Any] | None = None, queue: Any = None,
                             pre_navigation_hooks: Sequence[Callable] | None = None):
        from crawlee import service_locator

        config.validate()  # 组合校验：拦下实测会挂死/丢数据的配置组合
        kwargs = config.to_crawlee_kwargs(mode)  # 传 mode：浏览器模式按 browser_max_concurrency 收敛并发
        kwargs["configuration"] = _ensure_services(config)
        kwargs["event_manager"] = service_locator.get_event_manager()
        # 统计对象按"本次运行"新建且不持久化：
        # crawlee 的统计默认会落到 storage 并在下次运行读回，导致第二次运行显示上一次的成功数（实测误导）。
        from crawlee.statistics import Statistics

        kwargs["statistics"] = Statistics.with_default_state(persistence_enabled=False)
        kwargs.update(build_crawlee_middleware_kwargs(config))

        request_manager = await self._build_request_manager(config, start_urls, queue=queue)
        if request_manager is not None:
            kwargs["request_manager"] = request_manager

        if mode == "http":
            from crawlee.crawlers import HttpCrawler

            return HttpCrawler(request_handler=handler, **kwargs)

        if mode == "soup":
            from crawlee.crawlers import BeautifulSoupCrawler

            return BeautifulSoupCrawler(request_handler=handler, parser="html.parser", **kwargs)

        if mode in ("browser", "browser-cdp"):
            from crawlee.crawlers import PlaywrightCrawler

            from crawler_engine.browser import build_crawler_browser_kwargs, build_pre_navigation_hook
            from crawler_engine.middleware import build_pacing_limiter

            kwargs.update(build_crawler_browser_kwargs(config))
            crawler = PlaywrightCrawler(request_handler=handler, **kwargs)
            # 浏览器层处理（请求节奏 / 反侦测补丁 / 资源拦截）由引擎统一注入，Adapter 内不得出现
            crawler.pre_navigation_hook(build_pre_navigation_hook(config, build_pacing_limiter(config)))
            # 调用方附加的钩子（例如接口发现的响应监听器：导航前挂上才能捕获首屏请求）
            for hook in (pre_navigation_hooks or ()):
                crawler.pre_navigation_hook(hook)
            return crawler

        raise ValueError(f"未知执行模式: {mode}（可选: {', '.join(ENGINE_MODES)}）")

    @staticmethod
    async def open_queue(config: EngineConfig):
        """打开（或创建）Request Queue；配置了 queue_name 时为命名队列，可跨运行保留。

        显式传入 storage_client 与 configuration：否则 crawlee 会"隐式创建存储客户端"，
        触发告警并有覆盖本次 configuration 的副作用。

        注意：Configuration.purge_on_start 只对 crawlee 内部打开默认队列的路径生效，
        我们这里是显式 open，所以要自己按 resolved_purge_on_start() 清空，
        否则匿名队列会带着上一次的"已处理"记录跨运行去重，导致第二次运行空跑。
        """
        from crawlee.storage_clients import FileSystemStorageClient
        from crawlee.storages import RequestQueue

        queue = await RequestQueue.open(
            name=config.queue_name,
            configuration=config.to_configuration(),
            storage_client=FileSystemStorageClient(),
        )
        if config.resolved_purge_on_start():
            try:
                purged = queue.purge()
                if inspect.isawaitable(purged):
                    await purged
                logger.debug("[runner] 已清空队列（匿名队列默认每次运行清空）")
            except Exception as e:
                logger.warning(f"⚠️ [runner] 清空队列失败（忽略，继续）: {e!r}")
        return queue

    async def _build_request_manager(self, config: EngineConfig, start_urls: Sequence[Any] | None,
                                     queue: Any = None):
        """请求管理器：显式队列优先；开启按域限速时用 ThrottlingRequestManager 包装该队列。"""
        if queue is None:
            if not (config.respect_robots and config.enforce_robots_crawl_delay):
                return None
            queue = await self.open_queue(config)

        domains = {d for d in config.throttle_domains if d}
        for item in start_urls or ():
            raw = getattr(item, "url", item)
            host = urlparse(str(raw)).netloc
            if host:
                domains.add(host)

        if not (config.respect_robots and config.enforce_robots_crawl_delay and domains):
            return queue

        from crawlee.request_loaders import ThrottlingRequestManager
        from crawlee.storages import RequestQueue

        if not config.keep_alive_seconds:
            logger.warning(
                "⚠️ [runner] 已启用按域限速（enforce_robots_crawl_delay=True）。"
                "crawlee 在按域冷却期间可能提前结束爬取（实测不稳定），"
                "若站点存在\"列表页→动态入队详情页\"的流程，请谨慎使用并核对是否漏抓。"
            )
        logger.info(f"⏳ [runner] 启用按域限速（robots crawl-delay / 429 退避生效）: {sorted(domains)}")

        # 子队列 opener 必须带上本次运行的 configuration，否则会落到 crawlee 默认的 ./storage，
        # 把队列状态漏到配置的 storage_dir 之外（实测会在项目根目录留下 storage/ 目录）。
        run_configuration = config.to_configuration()

        def _opener(**kwargs):
            kwargs.setdefault("configuration", run_configuration)
            return RequestQueue.open(**kwargs)

        return ThrottlingRequestManager(
            inner=queue,
            domains=sorted(domains),
            request_manager_opener=_opener,
        )

    # ------------------------------------------------------------------ #
    # 2) legacy 桥接模式
    # ------------------------------------------------------------------ #
    async def run_legacy(self, crawler, keyword: str, max_count: int) -> RunResult:
        """把现有 BaseCrawler 子类放到统一入口执行（不改其内部实现）。"""
        started = time.perf_counter()
        logger.info(
            f"🧩 [runner] legacy 模式: {crawler.__class__.__name__}"
            f".scrape(keyword={keyword!r}, max_count={max_count})"
        )
        errors: list[str] = []
        leads: list = []
        try:
            leads = await crawler.scrape(keyword=keyword, max_count=max_count) or []
        except Exception as e:
            errors.append(f"{e.__class__.__name__}: {e}")
            logger.error(f"❌ [runner] legacy 爬虫执行失败: {e!r}")
        duration = time.perf_counter() - started
        result = RunResult(
            mode="legacy",
            status="ok" if not errors else "failed",
            platform=getattr(crawler, "platform_name", ""),
            keyword=keyword,
            items=len(leads),
            duration_s=duration,
            errors=errors,
            items_data=leads,
        )
        logger.info(f"✅ [runner] legacy 执行完成: {result.summary()}")
        return result

    # ------------------------------------------------------------------ #
    # 3) 统一平台入口（自动判定 legacy / Crawlee）
    # ------------------------------------------------------------------ #
    async def run_platform(
        self,
        platform: str,
        keyword: str,
        max_count: int = 5,
        *,
        mode: str | None = None,
    ) -> RunResult:
        from core.factory import CrawlerFactory

        crawler = CrawlerFactory.get_crawler(platform)
        declared = getattr(crawler, "engine_mode", "legacy")
        resolved = mode or declared or "legacy"

        if resolved == "auto":
            resolved = self.config.resolved_mode()

        has_hooks = hasattr(crawler, "engine_start_urls")
        if resolved == "legacy" or not has_hooks:
            if resolved != "legacy" and not has_hooks:
                logger.warning(
                    f"⚠️ [runner] {crawler.__class__.__name__} 声明 engine_mode={resolved} "
                    f"但未实现 engine_start_urls()，已回退到 legacy 模式。"
                )
            result = await self.run_legacy(crawler, keyword, max_count)
            result.platform = result.platform or platform
            return result

        if self.config.respect_robots and not getattr(crawler, "engine_skip_robots_preflight", False):
            logger.debug("[runner] robots 检查由底座在各请求上逐条执行（respect_robots=True）")

        start_urls = crawler.engine_start_urls(keyword=keyword, max_count=max_count) or []
        handler = crawler.engine_handler() if hasattr(crawler, "engine_handler") else self.default_handler()
        result = await self.run(handler, start_urls, mode=resolved)
        result.platform = getattr(crawler, "platform_name", platform)
        result.keyword = keyword
        return result

    # ------------------------------------------------------------------ #
    # 默认 handler：让迁移的第一天就有可用产出（落 HTML + 按需入队链接）
    # ------------------------------------------------------------------ #
    def default_handler(self, *, save_html: bool = True, link_pattern: str | None = None):
        """通用 handler：记录页面概况、可选落盘 HTML、按正则入队后续链接。

        适用于"先把调度搬上底座，解析逻辑稍后再迁移"的中间态。
        """
        storage = Path(self.config.storage_dir or ".") / "raw"
        pattern = re.compile(link_pattern, re.I) if link_pattern else None

        async def _handler(context) -> None:
            url = getattr(context.request, "url", "")
            title = ""
            html = ""

            page = getattr(context, "page", None)
            if page is not None:
                try:
                    title = await page.title()
                    html = await page.content()
                except Exception as e:
                    logger.debug(f"[handler] 读取页面失败 {url}: {e!r}")
            else:
                response = getattr(context, "http_response", None)
                if response is not None:
                    try:
                        body = response.read() if hasattr(response, "read") else b""
                        if asyncio.iscoroutine(body):
                            body = await body
                        html = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else str(body)
                    except Exception as e:
                        logger.debug(f"[handler] 读取响应失败 {url}: {e!r}")

            logger.info(f"   📄 [handler] {title or '(无标题)'} | {len(html)}B | {url}")

            if save_html and html:
                storage.mkdir(parents=True, exist_ok=True)
                digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:12]
                (storage / f"{digest}.html").write_text(html, encoding="utf-8", errors="ignore")

            if pattern:
                try:
                    await context.enqueue_links(strategy="same-domain", include=[pattern])
                except Exception as e:
                    logger.debug(f"[handler] 入队链接失败: {e!r}")

        return _handler

    # ------------------------------------------------------------------ #
    # API 接口发现
    # ------------------------------------------------------------------ #
    async def discover_api(
        self,
        url: str,
        *,
        cdp_url: str | None = None,
        output_path: str | None = None,
        template_path: str | None = None,
        store_sample: bool = False,
        headless: bool | None = None,
        mode: str | None = None,
        **filters: Any,
    ):
        """发现目标页的接口（xhr/fetch/graphql/json），并可选保存可重放模板。

        浏览器由 Crawlee 统一接管：走 browser / browser-cdp 模式打开页面，
        通过 pre_navigation_hook 挂上响应监听器。
        """
        from crawler_engine.network import ApiDiscovery

        discovery = ApiDiscovery(
            include_keywords=filters.get("include_keywords"),
            ignore_keywords=filters.get("ignore_keywords"),
            store_sample=store_sample,
        )
        return await discovery.discover(
            url,
            cdp_url=cdp_url or self.config.cdp_url,
            headless=headless,
            output_path=output_path,
            template_path=template_path,
            mode=mode,
            config=self.config,
        )

    async def replay_templates(
        self,
        template_path: str,
        *,
        via: str = "crawlee",
        limit: int | None = None,
        output_path: str | None = None,
        **config_overrides: Any,
    ):
        """按模板文件批量调用接口（via=crawlee|fetcher）。"""
        from crawler_engine.templates import ApiTemplateRunner, load_templates

        config = self.config.with_(**config_overrides) if config_overrides else self.config
        templates = load_templates(template_path)
        return await ApiTemplateRunner(config).replay(
            templates, via=via, limit=limit, output_path=output_path
        )


class CrawlRunner:
    """Adapter 驱动的统一入口（spec 接口：await CrawlRunner(config).run(adapter)）。

    职责：
        1. 创建 Crawlee crawler —— 按 engine_mode / 配置选择 http / soup / browser / browser-cdp；
        2. 管理 Request Queue —— 显式 open（支持命名队列）+ add_requests 入队 + run(purge_request_queue=False)；
        3. 管理并发 —— EngineConfig.concurrency -> ConcurrencySettings，并带 max_tasks_per_minute 全局限速；
        4. 调用 Adapter —— 把 adapter.handle / adapter.router 作为 request_handler 注入。

    Adapter 契约（见 adapters/base.py 的 BaseAdapter）：
        start_urls()      -> Sequence[str]（同步方法 / 异步方法 / 属性皆可）
        handle(context)   -> Awaitable[None]（默认实现已归一化上下文并调用 parse）
        parse(response)   -> 记录 / 记录列表 / None（只依赖 AdapterResponse，可离线单测）
        可选：router（crawlee Router，优先于 handle）、engine_mode、platform_name、requires_browser
    """

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig.from_env()
        self._runner = CrawlerRunner(self.config)
        self.queue = None

    async def run(self, adapter, *, mode: str | None = None) -> RunResult:
        from crawlee import Request

        urls, handler = await self._prepare_adapter(adapter)
        # 请求节奏：服从 robots.txt 声明的 Crawl-delay（必须在构建 crawler 之前解析，
        # 否则 max_tasks_per_minute 与导航前限速器都会用到旧的间隔）。
        self.config = await self._runner.resolve_pacing(self.config, urls)
        _ensure_services(self.config)  # 必须在 open_queue 之前：避免 crawlee 隐式创建全局配置
        resolved = mode or getattr(adapter, "engine_mode", None) or self.config.resolved_mode()

        # 契约校验：**按这批 URL** 判断是否需要浏览器。
        # 用类级 requires_browser 判断过粗：像 GlobalSourcesAdapter 只有检索页需要 JS，
        # 资料页可以用 HTTP（快 5 倍以上）。按 URL 判断后，"检索页走浏览器、详情页走 HTTP"
        # 这类混合策略才不会被误纠正。
        needs_browser = any(self._needs_browser(adapter, u) for u in urls)
        if needs_browser and resolved in ("http", "soup"):
            fallback = self.config.resolved_mode()
            logger.warning(
                f"⚠️ [CrawlRunner] 这批起始 URL 需要浏览器上下文（{adapter.__class__.__name__}."
                f"requires_browser_for() 为 True），与模式 {resolved} 冲突，已自动切换到 {fallback}。"
            )
            resolved = fallback
        if resolved == "legacy":
            raise ValueError("adapter 模式不支持 legacy；请改用 CrawlerRunner.run_platform()")

        logger.info(
            f"🔧 [CrawlRunner] adapter={adapter.__class__.__name__} | {self.config.describe(resolved)}"
        )

        # 内存预警：浏览器模式在内存紧张时会被系统压死 Chrome，导致整轮抓取失败（实测）。
        # 与其崩溃后排查，不如启动前提示。
        memory_hint = self.config.memory_guard_message()
        if memory_hint:
            logger.warning(f"⚠️ [CrawlRunner] {memory_hint}")

        # —— 职责 2：Request Queue 管理（显式打开 + 入队，不依赖隐式队列）——
        self.queue = await CrawlerRunner.open_queue(self.config)
        await self.queue.add_requests(
            [Request.from_url(u) for u in urls],
            wait_for_all_requests_to_be_added=True,
        )
        logger.info(
            f"🗂️ [CrawlRunner] 队列 '{self.config.queue_name or 'default'}': "
            f"本批入队 {len(urls)} 条，当前待处理 {await self._queue_pending(self.queue)} 条"
        )

        # —— 职责 4：调用 Adapter（统一异常记录，异常仍交回 Crawlee 计数与重试）——
        errors: list[str] = []

        async def wrapped(context):
            url = getattr(getattr(context, "request", None), "url", "?")
            try:
                return await handler(context)
            except AttributeError as e:
                errors.append(f"{url}: {e.__class__.__name__}: {e}")
                if "page" in str(e) and resolved in ("http", "soup"):
                    logger.error(
                        f"❌ [CrawlRunner] adapter 需要浏览器上下文（context.page），"
                        f"但当前模式为 {resolved}；请在 adapter 上声明 requires_browser = True "
                        f"或改用 browser / browser-cdp 模式。"
                    )
                else:
                    logger.error(f"❌ [CrawlRunner] adapter 处理失败 {url}: {e!r}")
                raise
            except Exception as e:
                errors.append(f"{url}: {e.__class__.__name__}: {e}")
                logger.error(f"❌ [CrawlRunner] adapter 处理失败 {url}: {e!r}")
                raise  # 交回 Crawlee 计数与重试

        # —— 职责 1 + 3：创建 crawler（并发/限速/会话/代理/按域限速均由配置统一注入）——
        crawler = await self._runner._build_crawler(resolved, wrapped, self.config, urls, queue=self.queue)
        _install_crawlee_noise_filter()  # crawler 已建好，其自身 logger 此时才存在于 registry 中

        started = time.perf_counter()
        run_error: str | None = None
        stats = None
        try:
            stats = await crawler.run(purge_request_queue=False)
        except Exception as e:
            # 崩溃兜底：浏览器被系统压死 / 驱动连接断开（BrowserContext.close: Connection closed
            # while reading from the driver）、站点长时间 429/403 等，都会在这里抛出。
            # 以前会直接裸崩（traceback + 退出码 1），已经采到的数据全部丢失且看不出原因。
            # 现在只记录错误，继续走完 flush / 结果收集，让已采集的数据正常产出。
            run_error = f"{type(e).__name__}: {e}"
            errors.append(run_error)
            logger.error(f"❌ [CrawlRunner] 爬取过程异常终止: {run_error}")
            logger.error(
                "   常见原因：① 内存不足导致浏览器进程被系统压死（看上面的内存预警，可加 --concurrency 1）；"
                "② 站点限流/封禁（429/403，建议降低并发或换 --cdp-url 附着已登录浏览器）；"
                "③ 页面导航被中断。已采集到的数据仍会正常输出。"
            )
        duration = time.perf_counter() - started
        totals = {
            "total": _stat(stats, "requests_total", default=max(len(urls), int(getattr(adapter, "pages_handled", 0) or 0))),
            "finished": _stat(stats, "requests_finished", default=int(getattr(adapter, "pages_handled", 0) or 0)),
            "failed": _stat(stats, "requests_failed", default=1 if run_error else 0),
        }

        # —— 阶段 2：把"不需要 JS"的 URL 改用 HTTP 抓取 ——
        phase2 = await self._run_http_phase(adapter, wrapped, errors)
        if phase2 is not None:
            stats2, seconds2, error2 = phase2
            duration += seconds2
            run_error = run_error or error2
            totals["total"] += _stat(stats2, "requests_total", default=0)
            totals["finished"] += _stat(stats2, "requests_finished", default=0)
            totals["failed"] += _stat(stats2, "requests_failed", default=0)

        # 给 Adapter 一次补出机会：处理了部分页面但未凑齐的商户记录，在此统一输出
        flush = getattr(adapter, "flush", None)
        if callable(flush):
            try:
                emitted = flush()
                if inspect.isawaitable(emitted):
                    emitted = await emitted
                if emitted:
                    logger.info(f"🧾 [CrawlRunner] flush 补出 {emitted} 条记录")
            except Exception as e:
                logger.warning(f"⚠️ [CrawlRunner] adapter.flush() 异常(忽略): {e!r}")

        remaining = await self._queue_pending(self.queue)

        pages_done = int(getattr(adapter, "pages_handled", 0) or 0)
        result = RunResult(
            mode=resolved,
            platform=getattr(adapter, "platform_name", adapter.__class__.__name__),
            status="ok" if not errors else "partial",
            requests_total=max(totals["total"], pages_done),
            requests_finished=max(totals["finished"], pages_done),
            requests_failed=totals["failed"],
            duration_s=duration,
            errors=errors,
        )

        # 收集 Adapter 侧产出（BaseAdapter.items），使 RunResult 与 legacy 路径结构一致
        items = getattr(adapter, "items", None)
        if items:
            result.items_data = list(items)
            result.items = len(items)
        adapter_summary = getattr(adapter, "summary", None)
        if callable(adapter_summary):
            logger.info(f"📦 [{adapter.__class__.__name__}] {adapter_summary()}")

        # 空跑诊断：请求被队列去重跳过时，Crawlee 会直接判定"完成"而不调用 handler，
        # 表现为"跑了但 0 页 0 记录"，很容易被误读为抓取成功。
        if pages_done == 0 and not run_error:
            logger.warning(
                "⚠️ [CrawlRunner] 本次没有任何页面进入 handler。最常见原因：请求命中了队列去重"
                "（该 URL 在持久化队列中已被处理）。默认队列已配置为每次运行清空；"
                "若你在用 --queue 命名队列，请更换队列名或清空后重试。"
            )
            result.status = "noop"

        # 异常终止时区分两种情况：有产出 -> partial（部分成功，数据仍可用）；无产出 -> failed
        if run_error:
            result.status = "partial" if result.items else "failed"
            if result.items:
                logger.warning(
                    f"⚠️ [CrawlRunner] 爬取中途异常，但已采集的 {result.items} 条记录仍然有效并已输出。"
                )
            else:
                logger.error("❌ [CrawlRunner] 本轮未产出任何记录，请按上面的原因排查后重试。")

        if remaining and remaining > 0:
            logger.info(f"⏸️ [CrawlRunner] 队列仍有 {remaining} 条未处理（命名队列可下次续跑）")
        logger.info(f"✅ [CrawlRunner] 执行完成: {result.summary()}")
        return result

    async def _prepare_adapter(self, adapter) -> tuple:
        """规范化 Adapter：解析起始 URL、取出 handler/router。"""
        if adapter is None:
            raise ValueError("adapter 不能为空")

        raw = getattr(adapter, "start_urls", None)
        if raw is None:
            raise ValueError(f"{adapter.__class__.__name__} 缺少 start_urls()")
        raw = raw() if callable(raw) else raw
        if inspect.isawaitable(raw):
            raw = await raw
        urls = [str(u) for u in (raw or [])]
        if not urls:
            raise ValueError(f"{adapter.__class__.__name__}.start_urls() 返回为空，无请求可执行")

        handler = getattr(adapter, "router", None) or getattr(adapter, "handle", None)
        if handler is None:
            raise ValueError(f"{adapter.__class__.__name__} 必须实现 handle(context) 或提供 router")
        return urls, handler

    @staticmethod
    def _needs_browser(adapter, url: str) -> bool:
        """该 URL 是否必须用浏览器（优先用 adapter 的按 URL 契约，回退到类级声明）。"""
        fn = getattr(adapter, "requires_browser_for", None)
        if callable(fn):
            try:
                return bool(fn(url))
            except Exception:
                pass
        return getattr(adapter, "requires_browser", None) is True

    async def _run_http_phase(self, adapter, wrapped, errors: list):
        """阶段 2：抓取 adapter 声明"可用 HTTP"的 URL（无需浏览器）。

        为什么需要这个阶段：很多站点只有列表/检索页需要 JS 渲染，详情页其实是服务端渲染的。
        把详情页也交给浏览器，单页耗时从约 1 秒涨到约 7 秒，还会把内存拉满（实测 Chrome 被
        系统压死、整轮失败）。legacy 爬虫原本就是"浏览器只管列表页 + httpx 抓详情"，
        迁移时不该丢掉这个优势。

        另外**单独解析**这批 URL 所在主机的 crawl-delay：crawl-delay 是按主机声明的指令，
        用检索页那个域要求的 10 秒去卡详情页子域并不正确（实测详情页子域 robots 未声明
        crawl-delay），那样会白白慢 5 倍以上。
        """
        pop = getattr(adapter, "pop_http_urls", None)
        http_urls = list(pop() or ()) if callable(pop) else []
        if not http_urls:
            return None

        from crawlee import Request

        phase_config = await self._runner.resolve_pacing(self.config, http_urls)
        logger.info(
            f"⚡ [CrawlRunner] 阶段2：{len(http_urls)} 个 URL 改用 HTTP 抓取（无需浏览器）| "
            f"间隔 {phase_config.effective_interval():.1f}s / {phase_config.effective_rate_per_minute()} 请求每分钟"
        )

        await self.queue.add_requests(
            [Request.from_url(u) for u in http_urls],
            wait_for_all_requests_to_be_added=True,
        )
        crawler = await self._runner._build_crawler("http", wrapped, phase_config, http_urls, queue=self.queue)
        _install_crawlee_noise_filter()

        started = time.perf_counter()
        try:
            stats = await crawler.run(purge_request_queue=False)
            return stats, time.perf_counter() - started, None
        except Exception as e:
            error = f"阶段2: {type(e).__name__}: {e}"
            errors.append(error)
            logger.error(f"❌ [CrawlRunner] HTTP 阶段异常终止: {error}")
            return None, time.perf_counter() - started, error

    @staticmethod
    async def _queue_pending(queue) -> int:
        """队列待处理数。

        注意：crawlee 的 get_total_count() 是"累计入队数"而非"待处理数"，
        必须减去 get_handled_count()，否则已跑完的队列会被误报为仍有积压。
        """
        try:
            total = int(await queue.get_total_count())
        except Exception:
            try:
                return 0 if await queue.is_empty() else -1
            except Exception:
                return -1
        try:
            handled = int(await queue.get_handled_count())
        except Exception:
            handled = 0
        return max(total - handled, 0)


def load_adapter(path: str, **kwargs: Any):
    """按 "module.path:ClassName" 或 "module.path.ClassName" 动态加载 Adapter 实例。

    kwargs 会尝试传给构造函数；若适配器不接受（TypeError）则退回无参构造。
    """
    import importlib

    module_path, _, class_name = path.partition(":")
    if not class_name:
        module_path, _, class_name = path.rpartition(".")
    if not module_path or not class_name:
        raise ValueError(f"无法解析 adapter 路径: {path!r}（应形如 package.module:ClassName）")
    module = importlib.import_module(module_path)
    if not hasattr(module, class_name):
        raise ValueError(f"{module_path} 中不存在 {class_name}")
    cls = getattr(module, class_name)
    if kwargs:
        try:
            return cls(**kwargs)
        except TypeError:
            logger.debug(f"[runner] {class_name} 不接受 {list(kwargs)}，改用无参构造。")
    return cls()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crawler_engine.runner", description="Crawlee 统一执行入口")
    target = parser.add_mutually_exclusive_group(required=False)
    target.add_argument("--adapter", help="Adapter 类路径，例如 adapters.globalsources:GlobalSourcesAdapter")
    target.add_argument("--platform", help="已注册平台（走 run_platform，自动判定 legacy / crawlee）")
    target.add_argument("--url", help="直接用统一底座抓取的起始 URL")
    target.add_argument("--discover", help="发现目标页的接口（xhr/fetch/graphql/json）")
    target.add_argument("--replay", help="按模板文件批量调用接口（配合 --via）")
    parser.add_argument("--selfcheck", action="store_true",
                        help="运行自检：职责边界检查 + Crawlee 能力接线核对")

    parser.add_argument("--keyword", default="", help="检索关键词（--platform 模式使用）")
    parser.add_argument("-n", "--limit", type=int, default=10, help="采集上限 / 最大请求数")
    parser.add_argument("--engine", choices=ENGINE_MODES, default="auto", help="执行模式")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--queue", default=None, help="Request Queue 名称（命名队列可跨运行续跑）")
    parser.add_argument("--cdp-url", default=None, help="附着到已登录的 Chrome，例如 http://127.0.0.1:9222")
    parser.add_argument("--headless", action="store_true", help="无头模式（默认有头，便于人工过验证码）")
    parser.add_argument("--no-robots", action="store_true", help="关闭 robots.txt 检查（请自行确认合规）")
    parser.add_argument("--interval", type=float, default=None,
                        help="相邻请求最小间隔（秒）。默认 3.0，并自动服从 robots.txt 的 Crawl-delay")
    parser.add_argument("--no-crawl-delay", action="store_true",
                        help="不按 robots.txt 的 Crawl-delay 放慢（默认服从）")
    parser.add_argument("--proxy", action="append", default=None, help="代理 URL，可重复传入")
    parser.add_argument("--excel", default=None, help="legacy 模式下把线索导出到该 Excel 文件")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    # —— 接口发现 / 模板重放 ——
    parser.add_argument("--save-templates", default=None,
                        help="发现完成后保存可重放模板的路径（默认 <storage_dir>/api_templates.json）")
    parser.add_argument("--store-sample", action="store_true",
                        help="把响应原始样本一并落盘（默认只存结构，避免持久化个人信息）")
    parser.add_argument("--via", choices=["crawlee", "fetcher"], default="crawlee",
                        help="重放通路：crawlee（默认，队列/重试/会话/并发托管）或 fetcher（轻量直连）")
    parser.add_argument("--replay-limit", type=int, default=None, help="重放请求数上限")
    parser.add_argument("--out", default=None, help="重放结果写入的 JSONL 路径")
    parser.add_argument("--refresh", action="store_true",
                        help="adapter 模式：忽略历史指纹库，强制重采已采集过的公司（默认跳过并提示）")
    return parser


def _run_selfcheck(config: EngineConfig) -> int:
    """自检：职责边界（AST 检查）+ Crawlee 能力接线核对。"""
    from crawler_engine.architecture import report as architecture_report, run_checks
    from crawler_engine.capabilities import report as capability_report

    print()
    print(architecture_report())
    print()
    print(capability_report(config))
    print()
    print(f"当前配置: {config.describe()}")
    errors, notes = run_checks()
    if errors:
        print(f"\n自检未通过：{len(errors)} 项越界（note {len(notes)} 项为已登记例外）")
        return 1
    print(f"\n自检通过：无越界（已登记例外 {len(notes)} 项）")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    config = EngineConfig.from_env(
        mode=args.engine,
        concurrency=args.concurrency,
        cdp_url=args.cdp_url,
        headless=True if args.headless else None,
        respect_robots=False if args.no_robots else None,
        proxy_urls=tuple(args.proxy) if args.proxy else None,
        queue_name=args.queue,
        log_level=args.log_level,
        min_request_interval=args.interval,
        respect_crawl_delay=False if args.no_crawl_delay else None,
    )

    if args.selfcheck:
        return _run_selfcheck(config)

    if not any((args.adapter, args.platform, args.url, args.discover, args.replay)):
        parser.error("请指定 --adapter / --platform / --url / --discover / --replay 之一，"
                     "或使用 --selfcheck 运行自检")

    runner = CrawlerRunner(config)

    async def _run() -> int:
        if args.discover:
            template_path = args.save_templates or str(
                Path(config.storage_dir or ".") / "api_templates.json"
            )
            endpoints = await runner.discover_api(
                args.discover,
                output_path=str(Path(config.storage_dir or ".") / "api_endpoints.json"),
                template_path=template_path,
                store_sample=args.store_sample,
            )
            print(f"\n共发现 {len(endpoints)} 个候选接口")
            print(f"可重放模板已保存: {template_path}")
            print(f"提示：批量调用 -> python -m crawler_engine --replay {template_path} --via crawlee")
            return 0

        if args.replay:
            report = await runner.replay_templates(
                args.replay, via=args.via, limit=args.replay_limit, output_path=args.out
            )
            print()
            print(report.report())
            return 1 if report.failed and not report.ok else 0

        if args.adapter:
            adapter = load_adapter(args.adapter, keyword=args.keyword or None,
                                   skip_seen=not args.refresh, max_count=args.limit or None)
            result = await CrawlRunner(config).run(adapter, mode=args.engine)
            print(f"\n{result.summary()}")
            for err in result.errors[:10]:
                print(f"  ⚠️ {err}")
            return 0

        if args.url:
            result = await runner.run(runner.default_handler(), [args.url], mode=args.engine)
            print(f"\n{result.summary()}")
            return 0

        result = await runner.run_platform(args.platform, args.keyword or "supplier", args.limit, mode=args.engine)
        print(f"\n{result.summary()}")
        for err in result.errors[:10]:
            print(f"  ⚠️ {err}")
        if args.excel and result.items_data:
            from exporters.excel import export_leads_to_excel

            export_leads_to_excel(
                leads_data=result.items_data,
                enriched_results=[{"site_phone": "", "tyc_phone": "", "email": "", "icp": "无",
                                   "contact_person": "", "contact_title": ""} for _ in result.items_data],
                eval_results=[None] * len(result.items_data),
                keyword=args.keyword or "supplier",
                output_file=args.excel,
            )
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
