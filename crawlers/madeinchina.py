from core.base_crawler import BaseCrawler
from core.factory import CrawlerFactory
from models import RawSupplierLead

@CrawlerFactory.register("made-in-china")
class MadeInChina(BaseCrawler):
    platform_name = "Made-in-China"
    platform_id = "made-in-china"

    async def scrape(self, keyword: str, max_count: int) -> list[RawSupplierLead]:
        print(f"🛠️ [{self.platform_name}] 正在检索品类: {keyword} (开发中骨架)...")
        return []