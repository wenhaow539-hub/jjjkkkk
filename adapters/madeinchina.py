"""Made-in-China Adapter（骨架）。

统一接口已就位：start_urls() 可跑通，parse() 待实现。
实现顺序建议与 AlibabaAdapter 相同：
    先接口发现 -> 判断 SSR / XHR -> 决定 http 还是 soup/browser 模式 -> 在 parse() 中解析。
    字段对齐 models.RawSupplierLead，便于直接复用 exporters.excel 与 pipeline 的后续增强环节。
合规提醒：注意目标站 robots.txt 与用户协议，底座默认逐请求检查 robots。
"""

from urllib.parse import quote_plus

from adapters.base import AdapterResponse, BaseAdapter
from utils.logger import get_logger

logger = get_logger("adapter.mic")


class MadeInChinaAdapter(BaseAdapter):

    platform_name = "Made-in-China"

    SEARCH_URL = "https://www.made-in-china.com/products-search/hot-china-products/{kw}.html?page={page}"

    def __init__(self, keyword: str = "phone", pages: int = 1):
        super().__init__()
        self.keyword = keyword
        self.pages = max(int(pages), 1)

    def start_urls(self):
        return [
            self.SEARCH_URL.format(kw=quote_plus(self.keyword), page=page)
            for page in range(1, self.pages + 1)
        ]

    def parse(self, response: AdapterResponse):
        """TODO(骨架)：解析搜索结果卡片，返回 RawSupplierLead 或 dict 列表。"""
        logger.info(f"      [{self.platform_name}] 骨架适配器：尚未实现 parse()（{response.brief()}）")
        return []
