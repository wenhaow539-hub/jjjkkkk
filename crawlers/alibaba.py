from adapters import BaseCrawler, CrawlerFactory
from models import RawSupplierLead


@CrawlerFactory.register("alibaba")
class Alibaba(BaseCrawler):
    platform_name = "Alibaba"
    platform_id = "alibaba"

    async def scrape(self, keyword: str, max_count: int) -> list[RawSupplierLead]:
        """
        Alibaba 爬虫实现步骤：
        1. self.ensure_chrome_running() 保证 CDP 连接
        2. 访问 https://www.alibaba.com/trade/search?SearchText={keyword}&tab=supplier
        3. 提取店铺列表并进入 Company Profile 提取独立官网与工商执照
        4. 返回 list[RawSupplierLead]
        """
        print(f"🛠️ [{self.platform_name}] 正在检索品类: {keyword} (开发中骨架)...")
        return []