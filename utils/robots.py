import re
from urllib import robotparser
from urllib.parse import urlparse

import httpx

from utils.logger import get_logger

logger = get_logger("robots")

_UA_LINE_RE = re.compile(r"^\s*user-agent\s*:\s*(\S+)", re.I)
_CRAWL_DELAY_RE = re.compile(r"^\s*crawl[-_\s]?delay\s*:\s*([0-9]+(?:\.[0-9]+)?)", re.I)


def parse_crawl_delays(text: str) -> tuple[float | None, float | None]:
    """从 robots.txt 文本解析 Crawl-delay。

    返回 ``(通配组 '*' 声明的值, 文件内声明过的最小值)``，都可能是 None。

    第二个值的用途：不少站点**只在具名爬虫分组里**声明 crawl-delay。
    以 Global Sources 为例（2026-09 实测）：

        User-agent: bingbot / Applebot / GPTBot 等   ->  Crawl-delay: 20
        User-agent: voyager                          ->  Crawl-delay: 10
        User-agent: *                                ->  未声明

    我们不是具名爬虫，通配组又为空，严格按 robots 文本"不需要"遵守任何间隔——
    但站点给自动客户端声明的容忍度，是判断"多快算太快"唯一的客观依据，
    因此把第二个值作为**提示**（配合自身的 min_request_interval 使用），
    而不是直接当成我们必须遵守的间隔。

    解析规则（易错点）：连续多个 `User-agent:` 行属于**同一组**；
    一旦出现第一条指令行，该组的 UA 列表就结束了，之后再出现 `User-agent:` 表示**新组**。
    最初漏了"新组重置"，导致后面所有 crawl-delay 都被误算进 `*` 组
    （表现为 `*` 组凭空多出一个 10s，而标准库 RobotFileParser 返回 None）。
    """
    wildcard: float | None = None
    smallest: float | None = None
    current_agents: list[str] = []
    rules_started = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        ua_match = _UA_LINE_RE.match(line)
        if ua_match:
            if rules_started:  # 上一组的指令已开始 -> 这是新的一组
                current_agents = []
                rules_started = False
            current_agents.append(ua_match.group(1).strip().lower())
            continue

        delay_match = _CRAWL_DELAY_RE.match(line)
        if delay_match:
            try:
                value = float(delay_match.group(1))
            except ValueError:
                value = None
            if value is not None:
                smallest = value if smallest is None else min(smallest, value)
                if "*" in current_agents:
                    wildcard = value if wildcard is None else min(wildcard, value)

        rules_started = True  # 任何非 User-agent 指令都标志着本组规则已开始

    return wildcard, smallest


class RobotsChecker:
    """异步 robots.txt 检查器（按站点缓存解析结果）。

    约定：robots.txt 不存在(404)或网络不可达时默认放行并告警一次，
    明确 Disallow 的路径一律拒绝抓取，由调用方跳过并记录。
    """

    def __init__(self):
        self._cache = {}
        self._delay_cache: dict[str, tuple[float | None, float | None]] = {}
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

        self._delay_cache[site_root] = parse_crawl_delays(resp.text)
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

    async def crawl_delays(self, client: httpx.AsyncClient, url: str, user_agent: str = "*") -> tuple[float | None, float | None]:
        """返回 ``(适用于我们的 crawl-delay, 站点对具名爬虫声明过的最小值)``。

        第一个值是**我们真正需要遵守**的：来自通配组（或匹配我们 UA 的组）；
        第二个值只用于提示——站点对已知爬虫（bingbot / GPTBot 等）声明的容忍度，
        可以据此判断"多快算太快"，但它并不是对我们的约束。
        """
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None, None
        site_root = f"{parsed.scheme}://{parsed.netloc}"
        if site_root not in self._cache:
            self._cache[site_root] = await self._load(client, site_root)
        return self._delay_cache.get(site_root, (None, None))

    async def crawl_delay(self, client: httpx.AsyncClient, url: str, user_agent: str = "*") -> float | None:
        """仅返回适用于我们的 crawl-delay（通配组声明值），没有则 None。"""
        return (await self.crawl_delays(client, url, user_agent))[0]


async def resolve_crawl_delays(
    url: str, user_agent: str = "*", *, timeout: float = 10.0
) -> tuple[float | None, float | None]:
    """独立于 httpx 客户端的便捷入口（引擎侧使用），返回 (适用值, 具名爬虫提示值)。

    自建短生命周期的 HTTP 客户端拉一次 robots.txt；任何异常都返回 (None, None)，
    绝不因为"读不到 robots"而阻断抓取。
    """
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True
        ) as client:
            return await RobotsChecker().crawl_delays(client, url, user_agent)
    except Exception as e:
        logger.warning(f"⚠️ [robots] 解析 crawl-delay 失败({e.__class__.__name__})，沿用自身间隔设置。")
        return None, None


async def resolve_crawl_delay(url: str, user_agent: str = "*", *, timeout: float = 10.0) -> float | None:
    """只取"适用于我们"的 crawl-delay（无则 None）。"""
    return (await resolve_crawl_delays(url, user_agent, timeout=timeout))[0]
