from typing import Type
from core.base_crawler import BaseCrawler

class CrawlerFactory:
    _registry: dict[str, Type[BaseCrawler]] = {}

    @classmethod
    def register(cls, platform_id: str):
        def decorator(subclass: Type[BaseCrawler]):
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