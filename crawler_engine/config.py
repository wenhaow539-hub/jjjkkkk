"""Crawlee 统一执行底座的配置中心。

设计原则（渐进式迁移）：
- 本模块只依赖 crawlee 的 Configuration / ConcurrencySettings，不反向依赖项目其它模块；
- 所有可调参数集中在此，支持环境变量覆盖（前缀 CRAWLER_ENGINE_）；
- 未显式配置时行为尽量贴近项目原状：尊重 robots、限速、非无头、复用本机 Chrome。
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

ENV_PREFIX = "CRAWLER_ENGINE_"

VALID_MODES = ("auto", "http", "soup", "browser", "browser-cdp", "legacy")


def _env(key: str, default: str | None = None) -> str | None:
    return os.getenv(f"{ENV_PREFIX}{key}", default)


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def _env_list(key: str, default: tuple = ()) -> tuple:
    raw = _env(key)
    if not raw:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass
class EngineConfig:
    """执行底座配置。mode 为 auto 时按 cdp_url / browser_type 自动选择运行模式。"""

    mode: str = "auto"

    # —— 调度与重试 ——
    concurrency: int = 4
    # crawlee 的并发是"自适应"的：从 min_concurrency 起步按负载爬升到 max。
    # 想要确定的固定并发（推荐爬虫场景），把 min_concurrency 设为与 concurrency 相同。
    min_concurrency: int = 1
    # 全站任务速率上限（次/分钟）。None = 自动推导：60 / min_request_interval × concurrency，
    # 即"单请求间隔 × 并发数"的礼貌节奏；需要绝对硬上限时再显式设置（设太小会让并发看起来失效）。
    max_tasks_per_minute: float | None = None
    max_request_retries: int = 3
    request_timeout: float = 30.0
    navigation_timeout: float = 35.0
    max_requests_per_crawl: int | None = None

    # —— 请求节奏（与 utils.ratelimit 共用语义）——
    min_request_interval: float = 1.2
    jitter: float = 0.35

    # —— 合规与反封 ——
    respect_robots: bool = True
    retry_on_blocked: bool = True
    # 启用后使用 ThrottlingRequestManager（robots crawl-delay 与按域 429 退避生效）。
    # 注意：crawlee 在按域冷却期间可能把"处理中动态入队"的请求视为暂时不可派发，
    # 从而提前结束爬取（实测不稳定：同一配置有时 3 页、有时 1 页），因此默认关闭。
    # 另：该选项与 keep_alive_seconds > 0 同时启用会导致爬虫不退出（实测挂死），
    # 引擎会在构建时直接报错，避免静默卡住。
    enforce_robots_crawl_delay: bool = False
    throttle_domains: tuple[str, ...] = ()
    # 队列空之后继续等待的秒数（0 = 不等待）。用于"处理中动态入队"场景；
    # 不可与 enforce_robots_crawl_delay 同时启用。
    keep_alive_seconds: float = 0.0

    # —— 浏览器 ——
    browser_type: str = "chrome"
    headless: bool = False
    user_data_dir: str | None = None
    cdp_url: str | None = None
    max_open_pages_per_browser: int = 20
    # 浏览器层处理（原 legacy 爬虫写在各爬虫内部，现统一上移到引擎层）
    stealth_patch: bool = True
    block_resources: bool = True
    block_extra_patterns: tuple[str, ...] = ()

    # —— 会话与代理 ——
    use_session_pool: bool = True
    session_pool_size: int = 10
    proxy_urls: tuple[str, ...] = ()
    tiered_proxy_urls: dict[str, str] = field(default_factory=dict)

    # —— 存储与日志 ——
    storage_dir: str | None = ".crawlee_storage"
    queue_name: str | None = None
    purge_on_start: bool = False
    # 接口发现产出的可重放模板落盘位置
    api_template_path: str | None = None
    # 是否把响应原始样本一并写入清单（默认 False：只保存结构，避免持久化个人信息）
    store_response_sample: bool = False
    log_level: str = "INFO"
    configure_crawlee_logging: bool = False

    @classmethod
    def from_env(cls, **overrides: Any) -> "EngineConfig":
        """从环境变量构建配置，overrides 中的显式参数优先级最高。"""
        cfg = cls(
            mode=_env("MODE", cls.mode) or cls.mode,
            concurrency=_env_int("CONCURRENCY", cls.concurrency),
            min_concurrency=_env_int("MIN_CONCURRENCY", cls.min_concurrency),
            max_tasks_per_minute=_env_float("MAX_TASKS_PER_MINUTE", 0) or None,
            max_request_retries=_env_int("MAX_REQUEST_RETRIES", cls.max_request_retries),
            request_timeout=_env_float("REQUEST_TIMEOUT", cls.request_timeout),
            navigation_timeout=_env_float("NAVIGATION_TIMEOUT", cls.navigation_timeout),
            max_requests_per_crawl=_env_int("MAX_REQUESTS_PER_CRAWL", 0) or None,
            min_request_interval=_env_float("MIN_REQUEST_INTERVAL", cls.min_request_interval),
            jitter=_env_float("JITTER", cls.jitter),
            respect_robots=_env_bool("RESPECT_ROBOTS", cls.respect_robots),
            retry_on_blocked=_env_bool("RETRY_ON_BLOCKED", cls.retry_on_blocked),
            enforce_robots_crawl_delay=_env_bool("ENFORCE_CRAWL_DELAY", cls.enforce_robots_crawl_delay),
            throttle_domains=_env_list("THROTTLE_DOMAINS", cls.throttle_domains),
            keep_alive_seconds=_env_float("KEEP_ALIVE_SECONDS", cls.keep_alive_seconds),
            browser_type=_env("BROWSER_TYPE", cls.browser_type) or cls.browser_type,
            headless=_env_bool("HEADLESS", cls.headless),
            user_data_dir=_env("USER_DATA_DIR", cls.user_data_dir),
            cdp_url=_env("CDP_URL", cls.cdp_url),
            max_open_pages_per_browser=_env_int("MAX_OPEN_PAGES_PER_BROWSER", cls.max_open_pages_per_browser),
            stealth_patch=_env_bool("STEALTH_PATCH", cls.stealth_patch),
            block_resources=_env_bool("BLOCK_RESOURCES", cls.block_resources),
            block_extra_patterns=_env_list("BLOCK_EXTRA_PATTERNS", cls.block_extra_patterns),
            use_session_pool=_env_bool("USE_SESSION_POOL", cls.use_session_pool),
            session_pool_size=_env_int("SESSION_POOL_SIZE", cls.session_pool_size),
            proxy_urls=_env_list("PROXY_URLS", cls.proxy_urls),
            storage_dir=_env("STORAGE_DIR", cls.storage_dir),
            queue_name=_env("QUEUE_NAME", cls.queue_name),
            purge_on_start=_env_bool("PURGE_ON_START", cls.purge_on_start),
            api_template_path=_env("API_TEMPLATE_PATH", cls.api_template_path),
            store_response_sample=_env_bool("STORE_RESPONSE_SAMPLE", cls.store_response_sample),
            log_level=_env("LOG_LEVEL", cls.log_level) or cls.log_level,
            configure_crawlee_logging=_env_bool("CRAWLEE_LOGGING", cls.configure_crawlee_logging),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    def resolved_mode(self) -> str:
        """把 auto 解析为具体模式：有 cdp_url 走 CDP 附着，否则走托管浏览器。"""
        if self.mode != "auto":
            return self.mode
        return "browser-cdp" if self.cdp_url else "browser"

    def validate(self) -> None:
        """组合校验：把实测会挂死/丢数据的配置直接拦在构建阶段，而不是静默出错。"""
        if self.enforce_robots_crawl_delay and self.keep_alive_seconds > 0:
            raise ValueError(
                "enforce_robots_crawl_delay 与 keep_alive_seconds 不能同时启用："
                "实测该组合会让爬虫在队列空后不退出（挂死）。"
                "请二选一（默认建议用项目自带的限速：min_request_interval × concurrency）。"
            )

    def effective_rate_per_minute(self) -> float:
        """实际生效的任务速率上限（次/分钟）。未显式配置时按"间隔 × 并发"推导。"""
        if self.max_tasks_per_minute:
            return float(self.max_tasks_per_minute)
        interval = max(float(self.min_request_interval), 0.05)
        return round(60.0 / interval * max(int(self.concurrency), 1), 2)

    def to_configuration(self):
        """映射到 crawlee 的全局 Configuration（存储目录 / 无头 / 日志级别 / 启动清空策略）。"""
        from crawlee.configuration import Configuration

        kwargs: dict[str, Any] = {}
        if self.storage_dir:
            kwargs["storage_dir"] = self.storage_dir
        kwargs["headless"] = self.headless
        kwargs["log_level"] = self.log_level
        # 默认 False：显式管理 Request Queue 时不静默清空，避免"续跑队列"被意外清空
        kwargs["purge_on_start"] = self.purge_on_start
        return Configuration(**kwargs)

    def to_crawlee_kwargs(self) -> dict[str, Any]:
        """生成可安全传给 HttpCrawler / BeautifulSoupCrawler / PlaywrightCrawler 的公共参数。"""
        from crawlee import ConcurrencySettings

        concurrency = max(int(self.concurrency), 1)
        min_concurrency = min(max(int(self.min_concurrency), 1), concurrency)
        kwargs: dict[str, Any] = {
            # 三个并发字段必须满足 min <= desired <= max，否则 crawlee 会直接抛错
            "concurrency_settings": ConcurrencySettings(
                min_concurrency=min_concurrency,
                desired_concurrency=concurrency,
                max_concurrency=concurrency,
                max_tasks_per_minute=self.effective_rate_per_minute(),
            ),
            "max_request_retries": self.max_request_retries,
            "request_handler_timeout": timedelta(seconds=self.request_timeout),
            "respect_robots_txt_file": self.respect_robots,
            "retry_on_blocked": self.retry_on_blocked,
            # 统一走项目自己的 logging（utils.logger），不重复配置 crawlee 日志
            "configure_logging": self.configure_crawlee_logging,
            "configuration": self.to_configuration(),
        }
        if self.max_requests_per_crawl:
            kwargs["max_requests_per_crawl"] = self.max_requests_per_crawl
        if self.keep_alive_seconds and self.keep_alive_seconds > 0:
            # 队列空后继续等待，兜住"处理中动态入队"的请求（需按域限速时尤其重要）
            kwargs["keep_alive"] = timedelta(seconds=self.keep_alive_seconds)
        return kwargs

    def api_templates_path(self) -> str:
        """接口模板的默认落盘路径。"""
        return self.api_template_path or str(Path(self.storage_dir or ".") / "api_templates.json")

    def with_(self, **overrides: Any) -> "EngineConfig":
        """返回覆盖部分字段后的副本（不改动原配置）。"""
        data = asdict(self)
        data.update({k: v for k, v in overrides.items() if v is not None and k in data})
        return EngineConfig(**data)

    def describe(self, mode: str | None = None) -> str:
        return (
            f"mode={mode or self.resolved_mode()} | concurrency={self.min_concurrency}~{self.concurrency} | "
            f"rate<={self.effective_rate_per_minute()}/min | retries={self.max_request_retries} | "
            f"robots={'on' if self.respect_robots else 'off'}"
            f"{'(crawl-delay on)' if self.enforce_robots_crawl_delay else ''} | "
            f"browser={self.browser_type}{'(cdp)' if self.cdp_url else ''} | "
            f"stealth={'on' if self.stealth_patch else 'off'} | block={'on' if self.block_resources else 'off'} | "
            f"session_pool={'on' if self.use_session_pool else 'off'} | "
            f"queue={self.queue_name or 'default'}{'(purge on start)' if self.purge_on_start else ''} | "
            f"proxy={'on' if (self.proxy_urls or self.tiered_proxy_urls) else 'off'}"
        )
