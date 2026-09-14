"""Alibaba Adapter（骨架）。

统一接口已就位：start_urls() 可跑通，parse() 待实现。
实现顺序建议：
    1. python -m crawler_engine --discover "<检索页URL>" --cdp-url http://127.0.0.1:9222
       —— 先摸清检索页数据是 SSR(HTML) 还是 XHR(JSON)；
    2. 若为 JSON：把 engine_mode 设为 "http"，在 parse() 中解析 response.json_data；
       若为 SSR：保持 browser 模式，在 parse() 中解析 response.soup / response.html；
    3. 字段对齐 models.RawSupplierLead（company / store_url / registered_company 等）。
合规提醒：注意目标站 robots.txt 与用户协议，底座默认逐请求检查 robots。
"""

from urllib.parse import quote_plus

from adapters.base import AdapterResponse, BaseAdapter
from utils.logger import get_logger

logger = get_logger("adapter.alibaba")


class AlibabaAdapter(BaseAdapter):

    platform_name = "Alibaba"

    SEARCH_URL = "https://www.alibaba.com/trade/search?SearchText={kw}&page={page}"

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
        """TODO(骨架)：解析搜索结果卡片，返回 RawSupplierLead 或 dict 列表。

        解析逻辑建议写成只依赖 response 的纯函数，便于离线用保存的 HTML 单测。
        """
        logger.info(f"      [{self.platform_name}] 骨架适配器：尚未实现 parse()（{response.brief()}）")
        return []
