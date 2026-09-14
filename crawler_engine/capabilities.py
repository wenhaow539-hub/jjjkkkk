"""Crawlee 统一接管的能力清单与自检。

对应约定「Crawlee 负责的能力」共 9 项，逐项记录"接在哪、怎么核对"，
并提供源码级 + 配置级双重核对，避免出现"以为交给 Crawlee 了、其实没接线"的情况。

    python -m crawler_engine --selfcheck
    from crawler_engine.capabilities import report
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crawler_engine.config import EngineConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class CapabilityCheck:
    key: str
    label: str
    owner: str
    wired: bool
    enabled: bool
    evidence: str

    def line(self) -> str:
        if not self.wired:
            mark = "❌"
        else:
            mark = "✅" if self.enabled else "➖"
        return f"{mark} {self.label:<14} {self.owner}\n        证据: {self.evidence}"


CAPABILITIES = [
    ("browser_lifecycle", "浏览器生命周期", "crawler_engine/browser.BrowserPool（PlaywrightBrowserPlugin / CdpBrowserPlugin）"),
    ("request_queue", "请求队列", "crawlee RequestQueue（CrawlRunner 显式 open / add_requests）"),
    ("concurrency", "并发控制", "crawlee ConcurrencySettings（config.to_crawlee_kwargs）"),
    ("retry", "Retry", "crawlee max_request_retries + middleware.RetryPolicy"),
    ("session", "Session", "crawlee SessionPool（middleware.build_session_pool）"),
    ("cookie", "Cookie", "SessionPool 会话 Cookie / CDP 复用已登录上下文"),
    ("proxy", "Proxy", "crawlee ProxyConfiguration（middleware.build_proxy_configuration）"),
    ("http", "HTTP 请求", "crawlee HttpCrawler / BeautifulSoupCrawler"),
    ("playwright", "Playwright", "crawlee PlaywrightCrawler + pre_navigation_hook"),
]


def _read(rel_path: str) -> str:
    path = PROJECT_ROOT / rel_path
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _markers(rel_path: str, *needles: str) -> tuple[bool, str]:
    text = _read(rel_path)
    missing = [n for n in needles if n not in text]
    if missing:
        return False, f"{rel_path} 缺少标记 {missing}"
    return True, f"{rel_path} 含 {list(needles)}"


def audit(config: EngineConfig | None = None) -> list[CapabilityCheck]:
    """逐项核对能力是否真的接线（源码级 + 配置级）。"""
    config = config or EngineConfig.from_env()
    from crawler_engine.middleware import build_crawlee_middleware_kwargs

    base_kwargs = config.to_crawlee_kwargs()
    middleware_kwargs = build_crawlee_middleware_kwargs(config)
    results: list[CapabilityCheck] = []

    def add(key: str, wired: bool, enabled: bool, evidence: str) -> None:
        label, owner = next((lab, own) for k, lab, own in CAPABILITIES if k == key)
        results.append(CapabilityCheck(key, label, owner, wired, enabled, evidence))

    ok, ev = _markers("crawler_engine/browser.py", "BrowserPool(", "browser_pool", "CdpBrowserPlugin")
    add("browser_lifecycle", ok, True, ev if ok else ev)

    ok, ev = _markers("crawler_engine/runner.py", "RequestQueue.open", "add_requests", "purge_request_queue=False")
    add("request_queue", ok, True, ev)

    settings = base_kwargs.get("concurrency_settings")
    wired = settings is not None
    detail = (
        f"ConcurrencySettings(min={getattr(settings, 'min_concurrency', '?')}, "
        f"max={getattr(settings, 'max_concurrency', '?')}, "
        f"速率上限={getattr(settings, 'max_tasks_per_minute', '?')}/min)"
    )
    add("concurrency", wired, wired, detail)

    wired = "max_request_retries" in base_kwargs
    ok2, ev2 = _markers("crawler_engine/middleware.py", "class RetryPolicy", "def delay_for")
    add("retry", wired and ok2, wired,
        f"max_request_retries={base_kwargs.get('max_request_retries')}；{ev2}")

    session_enabled = config.use_session_pool
    wired = ("session_pool" in middleware_kwargs) if session_enabled else True
    add("session", wired, session_enabled,
        f"SessionPool(max_pool_size={config.session_pool_size})" if session_enabled
        else "配置已关闭（use_session_pool=False）")

    ok3, ev3 = _markers("crawler_engine/browser.py", "connect_over_cdp")
    add("cookie", session_enabled or ok3, session_enabled,
        "会话 Cookie 由 SessionPool 持有；CDP 模式复用用户已登录上下文的 Cookie"
        + (f"；{ev3}" if ok3 else ""))

    proxy_enabled = bool(config.proxy_urls or config.tiered_proxy_urls)
    wired = ("proxy_configuration" in middleware_kwargs) if proxy_enabled else True
    add("proxy", wired, proxy_enabled,
        f"ProxyConfiguration({len(config.proxy_urls)} 个代理)" if proxy_enabled
        else "未配置代理：build_proxy_configuration 返回 None，不产生隐式代理")

    ok4, ev4 = _markers("crawler_engine/runner.py", "HttpCrawler(", "BeautifulSoupCrawler(")
    add("http", ok4, True, f"{ev4}；AsyncFetcher 仅用于队列外场景（接口重放/离线校验）")

    ok5, ev5 = _markers("crawler_engine/runner.py", "PlaywrightCrawler(", "pre_navigation_hook")
    add("playwright", ok5, True, ev5)

    return results


def report(config: EngineConfig | None = None) -> str:
    config = config or EngineConfig.from_env()
    checks = audit(config)
    lines = ["Crawlee 统一接管的能力（9 项）："]
    for check in checks:
        lines.append("    " + check.line())
    wired = sum(1 for c in checks if c.wired)
    enabled = sum(1 for c in checks if c.wired and c.enabled)
    lines.append("")
    lines.append(f"    接线 {wired}/{len(checks)} 项；启用 {enabled} 项（➖ 表示能力已接线但当前配置未启用）")
    lines.append("")
    lines.extend(_extras(config))
    return "\n".join(lines)


def _extras(config: EngineConfig) -> list[str]:
    """基于 Crawlee 之上的引擎附加能力（接口发现 / 模板重放），同样逐项核对接线。"""
    lines = ["引擎附加能力（建立在 Crawlee 之上）："]

    ok, ev = _markers("crawler_engine/network.py", "attach_hook", "CrawlerRunner", "pre_navigation_hooks")
    lines.append(f"    {'✅' if ok else '❌'} {'接口发现':<14} 监听 xhr/fetch/graphql/json（Crawlee 打开页面 + pre_navigation_hook 挂监听）")
    lines.append(f"        证据: {ev}")

    ok2, ev2 = _markers("crawler_engine/templates.py", "class ApiTemplate", "def render", "class ApiTemplateRunner")
    lines.append(f"    {'✅' if ok2 else '❌'} {'模板与重放':<12} 模板持久化（脱敏、版本化）+ 批量重放（crawlee / fetcher 双通路）")
    lines.append(f"        证据: {ev2}")

    ok3, ev3 = _markers("crawler_engine/templates.py", "mode=\"http\"", "CrawlerRunner")
    lines.append(f"    {'✅' if ok3 else '❌'} {'重放走 Crawlee':<11} 默认通路把模板转成 Request 交给 HttpCrawler（队列/重试/会话/并发托管）")
    lines.append(f"        证据: {ev3}")

    lines.append(f"    模板默认路径: {config.api_templates_path()}（响应样本落盘: {'开' if config.store_response_sample else '关'}）")
    return lines
