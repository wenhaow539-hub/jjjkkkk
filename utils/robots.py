from urllib import robotparser
from urllib.parse import urlparse

import httpx

from utils.logger import get_logger

logger = get_logger("robots")


class RobotsChecker:
    """异步 robots.txt 检查器（按站点缓存解析结果）。

    约定：robots.txt 不存在(404)或网络不可达时默认放行并告警一次，
    明确 Disallow 的路径一律拒绝抓取，由调用方跳过并记录。
    """

    def __init__(self):
        self._cache = {}
        self._warned = set()

    async def _load(self, client: httpx.AsyncClient, site_root: str):
        robots_url = f"{site_root}/robots.txt"
        try:
            resp = await client.get(robots_url, timeout=10.0, follow_redirects=True)
        except Exception as e:
            if site_root not in self._warned:
                logger.warning(f"⚠️ [robots] {robots_url} 获取失败({e.__class__.__name__})，默认放行并继续。")
                self._warned.add(site_root)
            return None
        if resp.status_code != 200:
            if site_root not in self._warned:
                logger.warning(f"⚠️ [robots] {robots_url} 返回 HTTP {resp.status_code}，默认放行并继续。")
                self._warned.add(site_root)
            return None
        rp = robotparser.RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp

    async def can_fetch(self, client: httpx.AsyncClient, url: str, user_agent: str = "*") -> bool:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return True
        site_root = f"{parsed.scheme}://{parsed.netloc}"
        if site_root not in self._cache:
            self._cache[site_root] = await self._load(client, site_root)
        rp = self._cache[site_root]
        if rp is None:
            return True
        return rp.can_fetch(user_agent, url)
