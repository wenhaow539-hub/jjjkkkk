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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

from crawler_engine.config import EngineConfig
from crawler_engine.middleware import build_crawlee_middleware_kwargs
from utils.logger import get_logger

logger = get_logger("engine.runner")

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


class CrawlerRunner:
    """Crawlee 统一执行入口。"""

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig.from_env()

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
        crawler = await self._build_crawler(resolved, wrapped, config, payload,
                                            pre_navigation_hooks=pre_navigation_hooks)
        logger.info(f"🧭 [runner] 启动 Crawlee 模式={resolved} | {config.describe(resolved)}")
        logger.info(f"🧭 [runner] 起始请求数: {len(payload)}")

        started = time.perf_counter()
        stats = await crawler.run(payload)
        duration = time.perf_counter() - started

        result = RunResult(
            mode=resolved,
            status="ok" if not errors else "partial",
            requests_total=_stat(stats, "requests_total", "requests_total_count", default=len(payload)),
            requests_finished=_stat(stats, "requests_finished", "requests_finished_count"),
            requests_failed=_stat(stats, "requests_failed", "requests_failed_count"),
            duration_s=duration,
            errors=errors,
        )
        logger.info(f"✅ [runner] Crawlee 执行完成: {result.summary()}")
        return result

    async def _build_crawler(self, mode: str, handler: Callable, config: EngineConfig,
                             start_urls: Sequence[Any] | None = None, queue: Any = None,
                             pre_navigation_hooks: Sequence[Callable] | None = None):
        from crawlee import service_locator

        config.validate()  # 组合校验：拦下实测会挂死/丢数据的配置组合
        kwargs = config.to_crawlee_kwargs()
        configuration = kwargs.pop("configuration")
        # 显式注入配置与事件管理器：避免 crawlee 隐式创建带来的副作用与告警。
        # 注意：crawlee 的 service_locator 只允许设置一次全局 Configuration，
        # 同进程内第二次构建（例如批处理多个 runner）会冲突，此时复用已有全局配置，
        # 本次执行仍以显式传入的 configuration= 为准，行为不受影响。
        try:
            service_locator.set_configuration(configuration)
        except Exception as e:
            logger.debug(f"[runner] 全局 Configuration 已存在({e.__class__.__name__})，本次沿用显式 configuration 参数。")
        kwargs["configuration"] = configuration
        kwargs["event_manager"] = service_locator.get_event_manager()
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

            kwargs.update(build_crawler_browser_kwargs(config))
            crawler = PlaywrightCrawler(request_handler=handler, **kwargs)
            # 浏览器层处理（反侦测补丁 / 资源拦截）由引擎统一注入，Adapter 内不得出现
            crawler.pre_navigation_hook(build_pre_navigation_hook(config))
            # 调用方附加的钩子（例如接口发现的响应监听器：导航前挂上才能捕获首屏请求）
            for hook in (pre_navigation_hooks or ()):
                crawler.pre_navigation_hook(hook)
            return crawler

        raise ValueError(f"未知执行模式: {mode}（可选: {', '.join(ENGINE_MODES)}）")

    @staticmethod
    async def open_queue(config: EngineConfig):
        """打开（或创建）Request Queue；配置了 queue_name 时为命名队列，可跨运行保留。"""
        from crawlee.storages import RequestQueue

        return await RequestQueue.open(name=config.queue_name, configuration=config.to_configuration())

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
        resolved = mode or getattr(adapter, "engine_mode", None) or self.config.resolved_mode()

        # 契约校验：声明 requires_browser=True 的 adapter 依赖 context.page，
        # 在 http / soup 模式下必然 AttributeError，这里提前纠正模式而不是让每个请求都失败。
        if getattr(adapter, "requires_browser", None) is True and resolved in ("http", "soup"):
            fallback = self.config.resolved_mode()
            logger.warning(
                f"⚠️ [CrawlRunner] {adapter.__class__.__name__} 声明 requires_browser=True，"
                f"与模式 {resolved} 冲突，已自动切换到 {fallback}。"
            )
            resolved = fallback
        if resolved == "legacy":
            raise ValueError("adapter 模式不支持 legacy；请改用 CrawlerRunner.run_platform()")

        logger.info(
            f"🔧 [CrawlRunner] adapter={adapter.__class__.__name__} | {self.config.describe(resolved)}"
        )

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

        started = time.perf_counter()
        stats = await crawler.run(purge_request_queue=False)
        duration = time.perf_counter() - started

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

        result = RunResult(
            mode=resolved,
            platform=getattr(adapter, "platform_name", adapter.__class__.__name__),
            status="ok" if not errors else "partial",
            requests_total=_stat(stats, "requests_total", default=len(urls)),
            requests_finished=_stat(stats, "requests_finished"),
            requests_failed=_stat(stats, "requests_failed"),
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
            adapter = load_adapter(args.adapter)
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
