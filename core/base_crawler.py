from abc import ABC, abstractmethod
import asyncio
import random
import config
from core.browser import ensure_chrome_running, is_port_open
from models import RawSupplierLead
from utils.ratelimit import AsyncRateLimiter
from utils.logger import get_logger
from utils.text import clean_token, html_to_clean_text, is_valid_company_name, normalize_website

logger = get_logger("crawler")

class BaseCrawler(ABC):
    platform_name: str = "BasePlatform"
    platform_id: str = "base"

    def __init__(self, cdp_port: int = 9222, concurrency: int = 4,
                 profile_dir: str = "./chrome_debug_profile",
                 proxy: str | None = None,
                 reuse_nearby: bool = True,
                 stream_tag: str = "A",
                 lane_offset: int = 1,
                 lane_stride: int = 1):
        self.cdp_port = cdp_port
        self.concurrency = concurrency
        self.profile_dir = profile_dir
        self.proxy = proxy
        # ⚠️ 双浏览器时第二个实例必须传 False：
        # ensure_chrome_running 在目标端口不是 DevTools 时会扫邻近端口复用"已在跑的调试浏览器"，
        # 允许复用会让第二台被错认成第一台（代理白配、登录态共用），且**不会报任何错**。
        self.reuse_nearby = reuse_nearby
        self.stream_tag = stream_tag
        # 翻页车道：A 扫 1/3/5…、B 扫 2/4/6…，两条流扫不相交的列表页。
        # 单流时 (1, 1)，与改造前完全一致。
        self.lane_offset = max(1, int(lane_offset or 1))
        self.lane_stride = max(1, int(lane_stride or 1))
        # 全站请求节奏：详情页是 httpx 并发抓取，必须限速。
        # 说明：legacy 早期版本有这个限速器，重写时被漏掉了 —— 于是 concurrency=4、
        # 每个商户内部再并发抓 profile+contact，瞬时最多 8 个请求零间隔打向同一站点，
        # 会被限流/拒绝；而抓取失败又被静默吞掉，表现为"整列字段空白且看不出原因"。
        #
        # ⚠️ 这个限速器是**全局串行**的（单实例共享，`_next_slot` 累加），所以它才是详情阶段的
        #    真正瓶颈：总耗时 ≈ 请求数 × min_interval。实测 12 请求 / 并发 4 → 12.6s，
        #    均值 1.14s/请求 —— 调大 `concurrency` 对吞吐**毫无帮助**。
        #    数值统一走 config（可用 .env 覆盖），默认 2026-09-17 由 1.2 下调到 0.9。
        self._rate_limiter = AsyncRateLimiter(
            min_interval=config.DETAIL_RATE_MIN_INTERVAL,
            jitter=config.DETAIL_RATE_JITTER,
        )

    def is_port_open(self, host: str = "127.0.0.1") -> bool:
        return is_port_open(host=host, port=self.cdp_port)

    def ensure_chrome_running(self, profile_dir: str | None = None):
        # 必须把实际端口写回 self.cdp_port：当 9222 被别的程序占用时，
        # ensure_chrome_running 会自动改用其它端口，scrape() 里的 connect_over_cdp
        # 以及 pipeline 的天眼查阶段都依赖 self.cdp_port，不回写就会连错端口。
        #
        # profile_dir / proxy / reuse_nearby 全部从实例属性取（而非硬编码默认值）：
        # 双浏览器时两台必须各自一套 profile + 端口，漏传任何一个都会让两台撞在一起。
        #
        # owner=stream_tag：给端口登记归属，**禁止这条流连到另一条流的浏览器上**。
        # 实测 2026-09-18：A 的浏览器中途退出后，A 回退时抓走了 B 的浏览器，
        # 两条流共用一台（代理/账号/IP 全废），日志里只有一行不显眼的 ♻️。
        before = self.cdp_port
        self.cdp_port = ensure_chrome_running(
            port=self.cdp_port,
            profile_dir=profile_dir or self.profile_dir,
            platform_name=self.platform_name,
            proxy=self.proxy,
            reuse_nearby=self.reuse_nearby,
            owner=self.stream_tag or None,
        )
        if self.cdp_port != before:
            # 端口变了 = 原来那台的浏览器已经没了（窗口被关 / 崩溃 / 端口被抢）。
            # 这必须**大声报出来**：它是"浏览器中途死掉"的唯一现场信号，
            # 上一次就是因为只在 print 里留了一行、没进日志，事后完全查不出发生了什么。
            logger.warning(
                f"🚨 [{self.platform_name}] 调试端口由 {before} 变为 {self.cdp_port} —— "
                f"原来那台浏览器可能已被关闭/崩溃，已自动切换到 {self.cdp_port}。"
                f"若这不是你干的，请检查内存占用或 Chrome 崩溃记录。"
            )
        return self.cdp_port

    async def human_delay(self, min_sec: float = 1.5, max_sec: float = 3.0, desc: str = ""):
        sleep_time = round(random.uniform(min_sec, max_sec), 2)
        if desc:
            print(f"      ⏱️ [{desc}] 模拟停顿 {sleep_time} 秒...")
        await asyncio.sleep(sleep_time)

    async def block_resources(self, route):
        if route.request.resource_type in ["image", "media", "font"]:
            await route.abort()
        elif any(b in route.request.url.lower() for b in ["google-analytics", "doubleclick", "sensorsdata"]):
            await route.abort()
        else:
            await route.continue_()

    # 挂载工具函数供子类调用
    is_valid_company_name = staticmethod(is_valid_company_name)
    clean_token = staticmethod(clean_token)
    normalize_website = staticmethod(normalize_website)
    html_to_clean_text = staticmethod(html_to_clean_text)

    @abstractmethod
    async def scrape(self, keyword: str, max_count: int) -> list[RawSupplierLead]:
        pass