from adapters import BaseCrawler, CrawlerFactory
from models import RawSupplierLead


@CrawlerFactory.register("made-in-china")
class MadeInChina(BaseCrawler):
    platform_name = "Made-in-China"
    platform_id = "made-in-china"

    async def scrape(self, keyword: str, max_count: int) -> list[RawSupplierLead]:
        """
        Made-in-China 爬虫实现步骤：
        1. 访问 https://www.made-in-china.com/multi-search/{keyword}/F1/
        2. 检索店铺卡片并进入 about 页面提取独立官网与工商名
        3. 返回 list[RawSupplierLead]
        """
        print(f"🛠️ [{self.platform_name}] 正在检索品类: {keyword} (开发中骨架)...")
        return []