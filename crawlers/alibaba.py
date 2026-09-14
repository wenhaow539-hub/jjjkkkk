from core.base_crawler import BaseCrawler
from core.factory import CrawlerFactory
from models import RawSupplierLead

@CrawlerFactory.register("alibaba")
class Alibaba(BaseCrawler):
    platform_name = "Alibaba"
    platform_id = "alibaba"

    async def scrape(self, keyword: str, max_count: int) -> list[RawSupplierLead]:
        print(f"🛠️ [{self.platform_name}] 正在检索品类: {keyword} (开发中骨架)...")
        return []