"""平台适配器集合（Adapter 模式）。

统一接口见 base.BaseAdapter：
    start_urls()     起始 URL
    handle(context)  请求处理器（默认实现已把上下文归一为 AdapterResponse 并调用 parse）
    parse(response)  解析逻辑（只依赖 AdapterResponse，可离线单测）

用法：
    from adapters import GlobalSourcesAdapter
    from crawler_engine import CrawlRunner, EngineConfig

    runner = CrawlRunner(EngineConfig.from_env(cdp_url="http://127.0.0.1:9222"))
    result = await runner.run(GlobalSourcesAdapter(keyword="phone", pages=2))
"""

from adapters.alibaba import AlibabaAdapter
from adapters.base import AdapterResponse, BaseAdapter
from adapters.globalsources import GlobalSourcesAdapter
from adapters.madeinchina import MadeInChinaAdapter

__all__ = [
    "AdapterResponse",
    "AlibabaAdapter",
    "BaseAdapter",
    "GlobalSourcesAdapter",
    "MadeInChinaAdapter",
]
