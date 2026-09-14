"""Crawlee 统一执行底座（渐进式迁移）。

对外入口：
    from crawler_engine import CrawlerRunner, EngineConfig

    runner = CrawlerRunner(EngineConfig.from_env(cdp_url="http://127.0.0.1:9222"))
    result = await runner.run_platform("globalsources", keyword="phone", max_count=30)   # legacy 或 crawlee，自动判定
    result = await runner.run(my_handler, ["https://example.com"], mode="http")          # 直接用底座
    await runner.discover_api("https://www.globalsources.com/searchList/suppliers?keyWord=phone")

模块职责：
    config.py     配置中心（含环境变量覆盖）
    runner.py     统一入口 / legacy 桥接 / 默认 handler
    fetcher.py    httpx 抓取封装（限速、robots、退避、会话、代理）
    browser.py    浏览器管理（托管浏览器 + CDP 附着，保护用户 Chrome 不被关闭）
    network.py    API 接口发现（从真实流量归纳 JSON 接口模板）
    middleware.py 重试策略 / 会话池 / 代理
"""

from crawler_engine.architecture import (
    LAYER_RESPONSIBILITIES,
    report as architecture_report,
    run_checks as check_architecture,
)
from crawler_engine.browser import (
    CdpBrowserPlugin,
    build_browser_pool,
    cdp_is_reachable,
    ensure_cdp_chrome,
)
from crawler_engine.capabilities import audit as audit_capabilities
from crawler_engine.capabilities import report as capability_report
from crawler_engine.config import EngineConfig
from crawler_engine.fetcher import AsyncFetcher, FetchResponse
from crawler_engine.middleware import (
    HttpSessionState,
    RetryPolicy,
    RetryableError,
    UARotator,
    is_blocked,
    with_retry,
)
from crawler_engine.network import ApiDiscovery, ApiEndpoint
from crawler_engine.runner import CrawlerRunner, CrawlRunner, RunResult
from crawler_engine.templates import (
    ApiTemplate,
    ApiTemplateRunner,
    RenderedRequest,
    ReplayReport,
    load_templates,
    save_templates,
)

__all__ = [
    "ApiDiscovery",
    "ApiEndpoint",
    "ApiTemplate",
    "ApiTemplateRunner",
    "AsyncFetcher",
    "CdpBrowserPlugin",
    "CrawlRunner",
    "CrawlerRunner",
    "EngineConfig",
    "FetchResponse",
    "HttpSessionState",
    "LAYER_RESPONSIBILITIES",
    "RenderedRequest",
    "ReplayReport",
    "RetryPolicy",
    "RetryableError",
    "RunResult",
    "UARotator",
    "architecture_report",
    "audit_capabilities",
    "build_browser_pool",
    "capability_report",
    "cdp_is_reachable",
    "check_architecture",
    "ensure_cdp_chrome",
    "is_blocked",
    "load_templates",
    "save_templates",
    "with_retry",
]
