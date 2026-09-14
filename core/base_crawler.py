from abc import ABC, abstractmethod
import asyncio
import random
from core.browser import ensure_chrome_running, is_port_open
from models import RawSupplierLead
from utils.text import clean_token, html_to_clean_text, is_valid_company_name, normalize_website

class BaseCrawler(ABC):
    platform_name: str = "BasePlatform"
    platform_id: str = "base"

    def __init__(self, cdp_port: int = 9222, concurrency: int = 4):
        self.cdp_port = cdp_port
        self.concurrency = concurrency

    def is_port_open(self, host: str = "127.0.0.1") -> bool:
        return is_port_open(host=host, port=self.cdp_port)

    def ensure_chrome_running(self, profile_dir: str = "./chrome_debug_profile"):
        ensure_chrome_running(port=self.cdp_port, profile_dir=profile_dir, platform_name=self.platform_name)

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