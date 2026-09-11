from abc import ABC, abstractmethod
import asyncio
import html as html_lib
import os
import random
import re
import shutil
import socket
import subprocess
import time
from models import RawSupplierLead


class BaseCrawler(ABC):
    """
    所有平台爬虫的抽象基类：
    封装 CDP 调试浏览器拉取、反爬停顿、文本清洗、企业名校验等通用能力。
    """
    platform_name: str = "BasePlatform"
    platform_id: str = "base"

    def __init__(self, cdp_port: int = 9222, concurrency: int = 4):
        self.cdp_port = cdp_port
        self.concurrency = concurrency

    def is_port_open(self, host: str = "127.0.0.1") -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex((host, self.cdp_port)) == 0

    def ensure_chrome_running(self, profile_dir: str = "./chrome_debug_profile"):
        if self.is_port_open():
            return

        print(f"🚀 [{self.platform_name}] 未检测到 CDP 调试浏览器，拉起 Chrome (端口: {self.cdp_port})...")
        possible_paths = [
            shutil.which("google-chrome"),
            shutil.which("chrome"),
            shutil.which("chromium"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
        chrome_path = next((p for p in possible_paths if p and os.path.exists(p)), None)
        if not chrome_path:
            raise FileNotFoundError("未在系统路径找到 Chrome 可执行文件，请确认是否已安装。")

        os.makedirs(profile_dir, exist_ok=True)
        cmd = [
            chrome_path,
            f"--remote-debugging-port={self.cdp_port}",
            f"--user-data-dir={os.path.abspath(profile_dir)}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu-shader-disk-cache",
        ]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for _ in range(15):
            if self.is_port_open():
                time.sleep(1)
                return
            time.sleep(1)
        raise TimeoutError(f"等待 Chrome 启动超时 (端口: {self.cdp_port})。")

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

    def is_valid_company_name(self, name: str) -> bool:
        r"""企业名称校验：处理末尾标点、大小写及多国法定企业后缀"""
        if not name or not isinstance(name, str):
            return False
        clean_name = re.sub(r'^[,\.;:\s"\'\(]+|[,\.;:\s"\'\)]+$', '', name.strip())
        if len(clean_name) < 4 or len(clean_name) > 100:
            return False

        name_lower = clean_name.lower()
        company_pattern = (
            r'(\bco\.?,?\s*ltd(?:\.|\b)|'
            r'\bpte\.?\s*ltd(?:\.|\b)|'
            r'\bltd(?:\.|\b)|'
            r'\blimited\b|'
            r'\bllc(?:\.|\b)|'
            r'\binc(?:\.|\b)|'
            r'\bcorp(?:\.|\b)|'
            r'\bcorporation\b|'
            r'\bgmbh(?:\.|\b)|'
            r'\bs\.?r\.?l(?:\.|\b)|'
            r'\bs\.?a(?:\.|\b)|'
            r'\bsdn\.?\s*bhd(?:\.|\b)|'
            r'\bcompany\b|\bgroup\b|\bfactory\b|'
            r'\btechnology\b|\btechnologies\b|'
            r'\benterprise\b|\bindustrial\b)'
        )
        if bool(re.search(company_pattern, name_lower)):
            return True

        pure_products = ["gaming monitor", "wholesale", "factory price", "moq", "pieces", "frameless", "hot sale"]
        return not any(pk in name_lower for pk in pure_products)

    def clean_token(self, val: str) -> str:
        if not val:
            return ""
        val = re.sub(r'[\r\n\t]+', ' ', val).strip()
        for b in ["send inquiry", "inquiry now", "chat now", "inquire", "contact supplier", "verified", "view more"]:
            if b in val.lower():
                return ""
        return val

    def normalize_website(self, url: str, exclude_domain: str = "") -> str:
        if not url:
            return ""
        url = re.sub(r'[,;:\s<>"\'\)]+$', '', url.strip())
        if exclude_domain and exclude_domain.lower() in url.lower():
            return ""
        if any(url.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.css', '.js', '.svg']):
            return ""
        clean_domain = re.sub(r'^https?://', '', url).split('/')[0]
        if '.' not in clean_domain or len(clean_domain) < 4:
            return ""
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"
        return url

    def html_to_clean_text(self, html_content: str) -> str:
        if not html_content:
            return ""
        clean = re.sub(r'<(script|style|head|noscript|svg)[^>]*>.*?</\1>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r'<(?:br|p|div|tr|li|h[1-6]|section|article)[^>]*>', '\n', clean, flags=re.IGNORECASE)
        clean = re.sub(r'<[^>]+>', ' ', clean)
        clean = html_lib.unescape(clean)
        lines = [re.sub(r'[ \t\xa0\u3000]+', ' ', line).strip() for line in clean.splitlines()]
        return '\n'.join([line for line in lines if line])

    @abstractmethod
    async def scrape(self, keyword: str, max_count: int) -> list[RawSupplierLead]:
        """子类需实现该方法，返回规范的 RawSupplierLead 列表"""
        pass


class CrawlerFactory:
    """爬虫注册中心与分发工厂"""
    _registry: dict[str, type[BaseCrawler]] = {}

    @classmethod
    def register(cls, platform_id: str):
        def decorator(subclass: type[BaseCrawler]):
            cls._registry[platform_id.lower().strip()] = subclass
            return subclass
        return decorator

    @classmethod
    def get_crawler(cls, platform_id: str, **kwargs) -> BaseCrawler:
        key = platform_id.lower().strip()
        if key not in cls._registry:
            supported = list(cls._registry.keys())
            raise ValueError(f"❌ 未找到注册的爬虫: '{platform_id}'。已加载爬虫: {supported}")
        return cls._registry[key](**kwargs)