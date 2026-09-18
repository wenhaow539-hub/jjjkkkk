import asyncio
import html as html_lib
import random
import re
from urllib.parse import quote_plus, urljoin
import httpx
from playwright.async_api import async_playwright

import config
from core.base_crawler import BaseCrawler
from utils.logger import get_logger

# GS 的输出量很大且与另一条流交错，必须走统一日志器：
# 双流时才会带上 [A]/[B] 前缀（单流输出与改造前逐字节一致），
# 而且会写进 logs/pipeline_YYYYMMDD.log —— print 是拿不到这两样的，
# 上一次排查「浏览器中途退出」时就是因为关键行只在 print 里、事后查不到。
logger = get_logger("globalsources")
from core.factory import CrawlerFactory
from models import RawSupplierLead
from utils.dedup import dedup
from utils.logger import get_logger

logger = get_logger("crawler.gs")

# Cookie 头的安全上限（字节）。GS 在请求头过大时直接返回 400 Request Header Or Cookie Too Large。
# 实测：把浏览器所有域的 cookie 全带上 = 16KB → 400；只带 GS 自己的 = 2.4KB → 200。
COOKIE_HEADER_LIMIT = 6000

# 复核历史行时没有浏览器可拿 UA，用这个兜底（与常见 Chrome 一致）。
# 实测详情页不带 cookie 也返回 200，所以复核请求可以不开浏览器。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# 「无中文工商名 → 重爬一次」前的冷却（秒）。
# 空值的主因是 403/429 限流与超时；立刻连发第二个请求等于再撞一次墙，
# 给它一点恢复时间才有意义。
NAME_RETRY_COOLDOWN = (2.0, 4.0)


@CrawlerFactory.register("globalsources")
class GlobalSources(BaseCrawler):
    platform_name = "Global Sources"
    platform_id = "globalsources"

    # 广告、探针、公共社媒及平台自身域名黑名单
    DOMAIN_BLACKLIST = {
        "globalsources.com", "licdn.com", "linkedin.com", "facebook.com",
        "google.com", "googletagmanager.com", "google-analytics.com",
        "doubleclick.net", "twitter.com", "x.com", "youtube.com",
        "instagram.com", "tiktok.com", "clarity.ms", "trustpilot.com",
        "alibaba.com", "aliexpress.com", "amazon.com", "baidu.com"
    }

    # 静态资源后缀：绝不可能是"独立站"
    # 实测教训：contact 页标签后的 700 字符窗口里先出现的是 /favicon.ico，
    # 而 normalize_website 又把开头的 "/" 剥掉 -> 提取结果变成 http://favicon.ico（全量污染）。
    STATIC_FILE_SUFFIXES = (
        ".ico", ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
        ".woff", ".woff2", ".ttf", ".eot", ".map", ".json", ".xml",
        ".php", ".asp", ".aspx", ".jsp", ".html", ".htm",
    )

    # 搜索列表最多翻到第几页（原先是 scrape() 里的局部变量，提到类常量做单一出处）。
    # ⚠️ 走 config（`.env` 的 `GS_MAX_SEARCH_PAGES`，默认 100）。
    #    曾经写死 25：实测 `phone + 广东` 在 GS 上有 **62 页**，于是 26 页往后**永远扫不到**，
    #    表现为"候选入库 +0 家"→ 被判「连续零新增、池子枯竭」→ 提前退出。
    #    详见 config.py 里 `GS_MAX_SEARCH_PAGES` 的说明。
    MAX_SEARCH_PAGES = config.GS_MAX_SEARCH_PAGES

    # ---- 跨批翻页进度 ----
    # 分批跑时每批都从 pageNum=1 重扫：已进指纹库的家会被 `dedup.is_seen` 跳过，
    # 但**页面本身仍要重新加载 + 滚动 3 次**（每页约 2~3s），4 批下来纯属白烧。
    # 记住页码后，第 2 批直接从上次停下的页继续。
    #
    # ⚠️ 代价（用户 2026-09-17 知情选择）：被剔除的家（无中文名 / 工商查空）**不进指纹库**，
    #    而它们通常排在靠前的页 —— 跳过这些页就等于放弃了对它们的重试机会。
    #
    # ⚠️ 只在**进程内**记忆（实例属性），不落盘。跨运行"静默从中间开始采"很难解释，
    #    真需要重扫全部时用 `--reset-pages`。
    def __init__(self, cdp_port: int = 9222, concurrency: int = 4,
                 profile_dir: str = "./chrome_debug_profile",
                 proxy: str | None = None,
                 reuse_nearby: bool = True,
                 stream_tag: str = "A",
                 lane_offset: int = 1,
                 lane_stride: int = 1):
        super().__init__(cdp_port=cdp_port, concurrency=concurrency,
                         profile_dir=profile_dir, proxy=proxy,
                         reuse_nearby=reuse_nearby, stream_tag=stream_tag,
                         lane_offset=lane_offset, lane_stride=lane_stride)
        self._page_cursor: dict[str, int] = {}

    def _cursor_key(self, clean_kw: str, year_in_business: str, supplier_location: str) -> str:
        """进度 key。带上 关键词/年限/地区 —— 换词或换地区不会串用别人的进度。"""
        return (f"{self.platform_id}|{clean_kw}|"
                f"{year_in_business or '-'}|{supplier_location or '-'}")

    def _cursor_get(self, key: str) -> int:
        """本批的起始页。

        默认从 `lane_offset` 起步（单流 = 1，与改造前一致）。
        双浏览器时两条流各自一套 crawler 实例：A 的 offset=1/stride=2 → 扫 1,3,5…，
        B 的 offset=2/stride=2 → 扫 2,4,6…，**同一关键词下两条流扫的页不相交**，
        否则两个浏览器会把同样的列表页各扫一遍（指纹去重能挡住重复入库，但白跑一遍）。
        """
        try:
            return max(self.lane_offset,
                       int(self._page_cursor.get(key, self.lane_offset) or self.lane_offset))
        except (TypeError, ValueError):
            return self.lane_offset

    def _cursor_put(self, key: str, page_num: int) -> None:
        """记录进度。

        ⚠️ 传的是**当前页**，不是下一页。因为命中 max_count 时内层 `break`
        （见 scrape 里 `if len(candidate_sellers) >= max_count: break`）会提前退出，
        **本页并没被吃干净** —— 若记成下一页，本页剩余的候选就永久漏掉了。
        所以下一批会重扫这一页：靠指纹去重跳掉已入库的，把剩下的接上。
        """
        try:
            self._page_cursor[key] = min(max(1, int(page_num)), self.MAX_SEARCH_PAGES + 1)
        except (TypeError, ValueError):
            pass

    def reset_page_cursor(self, keyword: str | None = None) -> int:
        """清掉翻页进度。给了 keyword 就只清该关键词的。返回清掉的条数。"""
        if not keyword:
            n = len(self._page_cursor)
            self._page_cursor.clear()
            return n
        kw = keyword.lower().replace("manufacturer", "").strip()
        hit = [k for k in self._page_cursor if f"|{kw}|" in k]
        for k in hit:
            del self._page_cursor[k]
        return len(hit)

    def _format_clean_url(self, href: str) -> str:
        if not href:
            return ""
        href = href.strip()
        if href.startswith("//"):
            return f"https:{href}"
        elif href.startswith("/"):
            return f"https://www.globalsources.com{href}"
        elif not href.startswith("http"):
            return f"https://www.globalsources.com/{href}"
        return href

    def _is_valid_official_website(self, url: str) -> bool:
        """严格校验提取的独立站是否合法：过滤平台自身、站内相对路径、静态资源。

        与旧实现的关键差异（旧实现会把 /favicon.ico 判为合法并剥掉前导 "/"）：
        1. 站内相对路径（/x、./x、../x）直接判否——外部独立站必须是绝对地址或裸域名；
        2. 静态资源后缀（.ico/.js/.css/.png...）判否；
        3. 主机名必须是 `a.b` 形式且顶级域为纯字母（把 `favicon.ico` 这类"看起来像域名"的文件名挡住）。
        """
        if not url:
            return False
        clean = url.strip().strip("'\"<>").rstrip('.,;:')
        low = clean.lower()

        if "@" in low or low.startswith(("javascript:", "mailto:", "tel:", "#")):
            return False
        if low.startswith(("/", "./", "../")):
            return False

        match = re.match(r'^(?:https?://)?([a-z0-9.\-_]+)(?:[/?].*)?$', low)
        if not match:
            return False
        host = match.group(1).strip(".")
        if not host:
            return False
        if any(host.endswith(suffix) for suffix in self.STATIC_FILE_SUFFIXES):
            return False
        if any(bad in host for bad in self.DOMAIN_BLACKLIST):
            return False

        labels = [part for part in host.split(".") if part]
        if len(labels) < 2 or not re.fullmatch(r'[a-z]{2,}', labels[-1]):
            return False
        return True

    def normalize_website(self, url: str) -> str:
        """规范化网址，清洗前后标点并补全 http 协议。

        注意：**不要**剥掉前导 "/"。旧实现把 `/favicon.ico` 变成 `favicon.ico` 再补成
        `http://favicon.ico`，把站内相对资源路径伪造出了一个域名。
        """
        if not url:
            return ""
        clean = url.strip().strip("'\"<>").rstrip('.,;:')
        clean = re.sub(r'^[:：\s]+', '', clean)
        # 相对路径直接判空：补协议会得到 http:///a 这种无效结果
        if clean.startswith(("/", "./", "../")):
            return ""
        if clean.startswith("//"):
            clean = f"https:{clean}"
        if not clean.startswith("http://") and not clean.startswith("https://"):
            clean = f"http://{clean}"
        return clean

    def _resolve_target_urls(self, store_url: str) -> tuple[str, str]:
        if re.search(r'/(?:homepage|contact-us|company-profile|showroom)_(\d+)\.htm', store_url, re.I):
            profile_url = re.sub(r'/(?:homepage|contact-us|company-profile|showroom)_', '/company-profile_', store_url, flags=re.I)
            contact_url = re.sub(r'/(?:homepage|contact-us|company-profile|showroom)_', '/contact-us_', store_url, flags=re.I)
            return profile_url, contact_url

        si_match = re.search(r'/si/(\d+)', store_url, re.I)
        if si_match:
            supplier_id = si_match.group(1)
            base_site = store_url.split('/si/')[0]
            return f"{base_site}/company-profile_{supplier_id}.htm", f"{base_site}/contact-us_{supplier_id}.htm"

        clean_base = store_url.rstrip('/')
        return f"{clean_base}/company-profile.htm", f"{clean_base}/contact-us.htm"

    async def _fetch_html(self, client: httpx.AsyncClient, url: str, retries: int = 2) -> str:
        """抓取单个详情页。

        与旧实现的关键差异：**不再静默吞掉失败**。
        旧实现在非 200（尤其 403）时循环几次后直接返回 ""，既不重试也不告警——
        表现为"独立站/注册地址/工商全称整列空白"，而日志里什么都看不到，
        根本无法判断是"页面没有"还是"我们被拒了"（实测踩过：一次运行 5 家详情全部为空，
        而同一批页面几分钟后重抓完全正常，说明当时是瞬时限流被静默吞了）。

        现在：403/429/503 一律退避重试；最终失败打 WARNING（进 logs/ 文件日志）。
        """
        if not url:
            return ""
        last_error = ""
        for attempt in range(retries + 1):
            try:
                await self._rate_limiter.acquire()   # 恢复请求节奏（重写时被漏掉的限速）
                resp = await client.get(url, timeout=15.0)
                if resp.status_code == 200:
                    return resp.text
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code == 400:
                    # 实测：cookie 头过大时 GS 直接 400。把服务端的原话带出来，避免又花时间猜。
                    if "Too Large" in (resp.text or ""):
                        last_error = "HTTP 400(Request Header Or Cookie Too Large → Cookie 头过大)"
                    else:
                        last_error = "HTTP 400(请求被拒，常见原因：Cookie 头过大或请求头异常)"
                    break
                if resp.status_code in (403, 429, 503):
                    await asyncio.sleep(max(1.5, 1.5 * (attempt + 1)))
                    continue
                break   # 404 等确定性失败：不浪费重试
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt == retries:
                    break
                await asyncio.sleep(0.8 * (attempt + 1))
        logger.warning(f"⚠️ [GS] 详情页抓取失败({last_error})，该页字段将为空: {url}")
        return ""

    def _extract_field_from_text(self, text: str, label_patterns: list[str], stop_words: list[str]) -> str:
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            for pat in label_patterns:
                m = re.match(rf'^{pat}\s*[:：]\s*(.+)$', line, re.I)
                if m:
                    cand = m.group(1).strip()
                    if cand and not any(sw.lower() in cand.lower() for sw in stop_words):
                        return self.clean_token(cand)

                if re.match(rf'^{pat}\s*[:：]?$', line, re.I):
                    if i + 1 < len(lines):
                        next_line = lines[i + 1].strip()
                        if (
                            next_line
                            and not any(sw.lower() in next_line.lower() for sw in stop_words)
                            and not re.search(r'[:：]$', next_line)
                        ):
                            return self.clean_token(next_line)
        return ""

    def _extract_official_website(self, texts: list[str], htmls: list[str]) -> str:
        """区块式独立站提取：以"标签紧邻的值"为第一优先，href 仅作兜底。

        页面真实结构（contact 页）：
            <div class="contact-label">Other homepage website:</div>
            <div class="contact-value">www.gasolutions.cn</div>

        旧实现是"先在标签后 700 字符里找 href，且取第一个通过校验的"，而窗口里先出现的
        href 是 `/favicon.ico` —— 于是每一家的独立站都变成 http://favicon.ico。
        现在改为：① 标签紧邻文本里的第一个 URL 形态 token；② 窗口内绝对外链 href；
        ③ 窗口内裸 URL。三步都过 `_is_valid_official_website`。
        """
        label_pattern = re.compile(
            r'(?:Other\s+Homepage\s+(?:Address|Website)|Other\s+(?:homepage\s+)?website|'
            r'Homepage\s+(?:Address|Website)|Company\s+Website|Official\s+Website|Website)'
            r'(?=\s*[:：]|\s*</)',   # 必须是"标签"，不是 JSON-LD 里的 "Website": 字段
            re.I
        )
        url_regex = re.compile(
            r'(?:https?://|www\.)[a-zA-Z0-9\-_]+(?:\.[a-zA-Z0-9\-_]+)+(?:/[^\s"\'<>]*)?',
            re.I
        )
        next_label_re = re.compile(r'class="(?:contact|profile|company)-label"', re.I)
        # 先剥掉 script/style：页面里嵌的 JSON-LD 含 `"Website": "https://schema.org"`，
        # 不剥掉的话标签正则会命中它并把 schema.org 当成独立站（实测踩到）。
        script_re = re.compile(r'<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>', re.I | re.S)

        for source in htmls:
            if not source:
                continue
            source = script_re.sub(' ', source)
            for match in label_pattern.finditer(source):
                window = source[match.end(): match.end() + 400]
                cut = next_label_re.search(window)
                if cut:  # 不越界到下一个字段
                    window = window[: cut.start()]

                # ① 标签紧邻的值（去标签后取第一个 URL 形态 token）—— 主路径
                text_only = html_lib.unescape(re.sub(r'<[^>]+>', ' ', window))
                for cand in url_regex.findall(text_only):
                    if self._is_valid_official_website(cand):
                        return self.normalize_website(cand)

                # ② 绝对外链 href（相对路径/平台域/静态资源会被校验挡掉）
                for href in re.findall(r'href=["\']([^"\']+)["\']', window, re.I):
                    if self._is_valid_official_website(href):
                        return self.normalize_website(href)

                # ③ 兜底：窗口内的裸 URL（可能被标签切碎）
                for raw_u in url_regex.findall(window):
                    if self._is_valid_official_website(raw_u):
                        return self.normalize_website(raw_u)

        # 纯文本按行滑窗兜底（标签在上一行、值在下一行的情况）
        for text in texts:
            if not text:
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            for i, line in enumerate(lines):
                if label_pattern.search(line):
                    window_text = " ".join(lines[i: i + 5])
                    for cand in url_regex.findall(window_text):
                        if self._is_valid_official_website(cand):
                            return self.normalize_website(cand)

        return ""

    async def _parse_detail(self, client: httpx.AsyncClient, store_url: str) -> dict:
        info = {"registered_company": "", "registered_address": "", "official_website": "", "raw_products": ""}
        if not store_url or not store_url.startswith("http"):
            return info

        profile_url, contact_url = self._resolve_target_urls(store_url)
        profile_html, contact_html = await asyncio.gather(
            self._fetch_html(client, profile_url),
            self._fetch_html(client, contact_url)
        )

        # 抓取店铺首页作为保底
        home_html = ""
        if not profile_html or not contact_html:
            home_html = await self._fetch_html(client, store_url)

        # 核心容错：若未带 ID 导致联系页 404，从首页导航栏动态搜寻真实的 contact-us 链接
        if (not contact_html or len(contact_html) < 300) and home_html:
            m_contact = re.search(r'href=["\']([^"\']*contact-us[^"\']*)["\']', home_html, re.I)
            if m_contact:
                real_contact_url = urljoin(store_url, m_contact.group(1))
                contact_html = await self._fetch_html(client, real_contact_url)

        profile_text = self.html_to_clean_text(profile_html)
        contact_text = self.html_to_clean_text(contact_html)
        home_text = self.html_to_clean_text(home_html) if home_html else ""

        # 1. 提取法定名称
        comp_labels = [r'Registered\s*Company(?:\s*Name)?', r'Company\s*Name', r'Legal\s*Business\s*Name', r'Business\s*Name']
        stop_words = ["registration number", "business type", "year established", "country", "view more", "undefined", "null"]
        info["registered_company"] = self._extract_field_from_text(profile_text, comp_labels, stop_words)
        if not info["registered_company"]:
            info["registered_company"] = self._extract_field_from_text(contact_text, comp_labels, stop_words)

        # 兜底：**直接用平台网址本身再解析一次**。
        # store_url 是列表页 <a href> 直接给的，一定可靠；而 profile/contact 是我们
        # 按规则拼出来的（`company-profile_<id>.htm` / `.../company-profile.htm`），
        # 站点改版或 URL 形态没覆盖到时就是 404/403 —— 这两页一空，中文名就没了。
        # 中文工商名是整条记录的命脉（缺了整行作废），所以值得为它多抓一次原始页。
        if not info["registered_company"]:
            if not home_html:
                home_html = await self._fetch_html(client, store_url)
                home_text = self.html_to_clean_text(home_html) if home_html else ""
            if home_text:
                info["registered_company"] = self._extract_field_from_text(home_text, comp_labels, stop_words)

        # 2. 提取注册地址
        addr_labels = [
            r'Company\s*Registration\s*Address', r'Registered\s*Address', r'Registration\s*Address',
            r'Operational\s*Address', r'Factory\s*Address', r'Business\s*Address', r'Office\s*Address', r'Address'
        ]
        addr_stops = ["zip code", "country/region", "view more", "view less", "null", "production capacity", "* in china"]
        info["registered_address"] = self._extract_field_from_text(profile_text, addr_labels, addr_stops)
        if not info["registered_address"]:
            info["registered_address"] = self._extract_field_from_text(contact_text, addr_labels, addr_stops)

        # 3. 提取主营产品 (Main Products)
        prod_m = re.search(r'Main\s*Products?\s*[:：]\s*([^\r\n]+)', f"{profile_text}\n{contact_text}\n{home_text}", re.I)
        if prod_m:
            info["raw_products"] = prod_m.group(1).strip()

        # 4. 穿透提取独立站官网
        info["official_website"] = self._extract_official_website(
            texts=[contact_text, profile_text, home_text],
            htmls=[contact_html, profile_html, home_html]
        )

        # 详情页全部抓取失败时明确说出来，避免"字段空白但日志无声"的排查地狱
        if not (info["registered_company"] or info["registered_address"] or info["official_website"]):
            logger.warning(
                f"⚠️ [GS] 详情页未取到任何字段（工商名/地址/独立站均为空）: {store_url}；"
                f"profile={len(profile_html)}B contact={len(contact_html)}B home={len(home_html)}B"
                f"（字节数为 0 = 请求被拒/超时；字节数正常 = 页面本身没有这些字段，或站点改版导致标签变化）"
            )

        return info

    async def refetch_company_names(self, store_urls: list[str]) -> dict[str, str]:
        """对指定店铺 URL 重抓一次详情页，只取中文工商名（URL → 名称，取不到为空串）。

        存在的理由：**历史遗留的「无中文名」行没法靠正常采集挽救** ——
        这些公司的指纹早就写进 seen_hashes.txt，`is_seen()` 恒真，采集阶段永远跳过它们。
        唯一能做的，就是拿着 Excel 里的「平台网址」直接回锅重抓。

        不用浏览器：实测详情页不带 cookie 同样返回 200，所以这里自建 httpx 客户端即可，
        省掉一次 CDP 附着（附着还可能被卡死标签页挡住）。
        """
        urls = []
        for u in (store_urls or []):
            u = (u or "").strip()
            if u.startswith("http") and u not in urls:
                urls.append(u)
        if not urls:
            return {}

        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
        }
        semaphore = asyncio.Semaphore(self.concurrency)
        result: dict[str, str] = {}

        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15.0) as client:
            async def one(u: str):
                async with semaphore:
                    try:
                        detail = await self._parse_detail(client, u)
                    except Exception as e:
                        logger.warning(f"⚠️ [GS] 复核抓取异常: {u} ({e!r})")
                        return u, ""
                    return u, (detail.get("registered_company") or "").strip()

            for u, name in await asyncio.gather(*[one(u) for u in urls]):
                result[u] = name
        return result

    async def scrape(
        self,
        keyword: str,
        max_count: int,
        year_in_business: str = "-5",
        supplier_location: str = "China-Guangdong"
    ) -> list[RawSupplierLead]:
        self.ensure_chrome_running()
        clean_kw = keyword.lower().replace("manufacturer", "").strip()

        candidate_sellers = []
        seen_companies = set()

        async with async_playwright() as p:
            logger.info(f"🔌 [{self.platform_name}] 接入 Chrome (CDP 端口: {self.cdp_port})...")
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}")
            context = browser.contexts[0] if browser.contexts else await browser.new_context()

            # ⚠️ **必须自建专用标签页，不能用 `context.pages[0]` 复用已有页面**：
            #   ① 那个"第一个页面"可能是用户自己开着的标签页 —— 一次 goto 就把它导航走了；
            #   ② 现在启用了「异步预取」：GS 采集与两家工商补全会**同时**跑，
            #      补全用它自己的标签页。若采集去复用 pages[0]，两者会抢同一个 page，
            #      互相导航/关页 → 直接崩。
            # 自建页面不影响登录态（cookie 来自 context），代价只是多一个标签；
            # 所以本批跑完要**关掉它**（见下面 unroute 之后），否则一批积一个标签。
            page = await context.new_page()

            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
            await page.route("**/*", self.block_resources)

            # —— 承接上批的翻页进度 ——
            ckey = self._cursor_key(clean_kw, year_in_business, supplier_location)
            _has_cursor = ckey in self._page_cursor
            page_num = self._cursor_get(ckey)
            max_search_pages = self.MAX_SEARCH_PAGES
            if page_num > max_search_pages:
                logger.warning(f"⚠️ [{self.platform_name}] 该关键词/地区下已翻到翻页上限"                      f"（第 {max_search_pages} 页），没有更多可扫的页 → 本批 0 家")
                # ⚠️ 不在这里 `return []`：那会跳过下面的收尾（关标签页）。
                # 把上界压到当前页之下，让 while 条件直接不成立，统一走收尾流程。
                # 游标仍会被记成 MAX+1，下次进来还是走这个分支，行为不变。
                max_search_pages = page_num - 1
            if self.lane_stride > 1:
                # 双浏览器：本实例只负责其中一条车道。
                # ⚠️ 措辞要区分「承接上批进度」与「本车道的第一页」——
                #    后者（B 首次跑、offset=2）若也写成"承接进度：第 1~1 页已扫过"，
                #    会让人以为漏扫了第 1 页；其实第 1 页归 A 的车道，由 A 去扫。
                if _has_cursor:
                    logger.info(f"📑 [{self.platform_name}] 承接上批翻页进度：本车道（步长 {self.lane_stride}）"                          f"从第 {page_num} 页继续")
                else:
                    logger.info(f"📑 [{self.platform_name}] 本车道起始第 {page_num} 页"                          f"（步长 {self.lane_stride}：扫第 {page_num}、"
                          f"{page_num + self.lane_stride}、{page_num + self.lane_stride * 2}… 页；"
                          f"其余页由另一台浏览器负责）")
            elif _has_cursor and page_num > 1:
                logger.info(f"📑 [{self.platform_name}] 承接上批翻页进度：从第 {page_num} 页继续"                      f"（第 1~{page_num - 1} 页已扫过，不再重扫；要重扫请加 --reset-pages）")

            while len(candidate_sellers) < max_count and page_num <= max_search_pages:
                query_parts = [
                    f"keyWord={quote_plus(clean_kw)}",
                    f"pageNum={page_num}"
                ]
                if year_in_business:
                    query_parts.append(f"yearInBusiness={year_in_business}")
                if supplier_location:
                    query_parts.append(f"sls={supplier_location}")

                search_url = f"https://www.globalsources.com/searchList/suppliers?{'&'.join(query_parts)}"
                logger.info(f"📑 [{self.platform_name}] 检索第 {page_num} 页: {search_url}")
                try:
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
                    await self.human_delay(2.0, 3.0, desc=f"第 {page_num} 页就绪")
                except Exception as e:
                    logger.warning(f"⚠️ 第 {page_num} 页加载超时: {e}")
                    break

                for _ in range(3):
                    await page.mouse.wheel(0, random.randint(650, 950))
                    await self.human_delay(0.5, 1.0)

                candidate_elements = await page.query_selector_all(
                    'a[href*="manufacturer.globalsources.com/homepage_"], a[href*="/si/"], a.company-name, a.supplier-name'
                )
                if not candidate_elements:
                    # 空页 = 这一页一张候选卡片都没有。可能是"真到底了"，**也可能是渲染失败**。
                    # ⚠️ 所以游标**不前进**（见循环后的 `_cursor_put(ckey, page_num)`）：
                    #    下一批会重试这一页。宁可多扫一页，也不赌"到底了"而永久漏掉它。
                    #    注意区分：卡片都在、只是被判为"已见过"时不会走到这里
                    #    （那种情况会打 `候选入库: +0 家` 但卡片仍在）。
                    logger.info(f"📄 [{self.platform_name}] 第 {page_num} 页没有候选卡片"
                                f"（可能已翻到底，也可能是渲染失败）→ 停在本页，下一批重试")
                    break

                page_added = 0
                for el in candidate_elements:
                    if len(candidate_sellers) >= max_count:
                        break

                    href = await el.get_attribute("href") or ""
                    if any(pk in href.lower() for pk in ["/pdtl/", "/product_", "productdetail", "/product/"]):
                        continue

                    text = (await el.inner_text()).strip()
                    title_attr = (await el.get_attribute("title") or "").strip()
                    comp_name = title_attr if self.is_valid_company_name(title_attr) else text
                    clean_url = self._format_clean_url(href).rstrip('/')

                    if dedup.is_seen(comp_name):
                        continue

                    if self.is_valid_company_name(comp_name) and comp_name not in seen_companies and href:
                        seen_companies.add(comp_name)
                        # —— 候选即写指纹（2026-09-17 用户口径）——
                        # 一进来就登记，同一轮/后续批次不会再采到同一家，
                        # 省掉白跑的详情页 + 独立站 + 两家工商补全。
                        # ⚠️ 配套回滚在 pipeline 侧：这家若最终因「无中文名 / 工商查空」
                        #    没入库，会被 `rollback_fingerprints()` 撤掉，下次还能重试。
                        dedup.add(comp_name)

                        card_data = await el.evaluate("""
                            (a) => {
                                const card = a.closest('li.item, div.card-box, div.mod-supp-info, div.right, div.header') || a.parentElement;
                                if (!card) return { years: '', products: '' };

                                let years = '';
                                const yearsP = card.querySelector('p[class*="years"], [class*="years"]');
                                if (yearsP) {
                                    const num = yearsP.querySelector('.num, i')?.innerText?.trim() || '';
                                    const suffix = yearsP.querySelector('.suffix')?.innerText?.trim() || 'year';
                                    years = num ? `${num} ${suffix}`.trim() : yearsP.innerText.trim();
                                }

                                let products = '';
                                const text = card.innerText || '';
                                const m = text.match(/Main\\s*Products\\s*[:：]\\s*([^\\n\\r]+)/i);
                                if (m) {
                                    products = m[1].trim();
                                }

                                return { years, products };
                            }
                        """)

                        candidate_sellers.append({
                            "company": comp_name,
                            "store_url": clean_url,
                            "platform_years": card_data.get("years", ""),
                            "raw_products": card_data.get("products", "")
                        })
                        page_added += 1

                logger.info(f"✅ [{self.platform_name}] 候选入库: +{page_added} 家 (当前累计: {len(candidate_sellers)}/{max_count})")
                if len(candidate_sellers) < max_count:
                    # 按车道步长前进（单流 stride=1 → 与改造前一致；双流 stride=2 → 隔页扫）
                    page_num += self.lane_stride
                    await self.human_delay(1.5, 2.5, desc="翻页冷却")

            # 记录翻页进度：`page_num` 停在「还没吃干净的那一页」
            # （命中 max_count 时内层 break 提前退出，上面的自增不会执行）
            # ⚠️ 扫到空页时也记**当前页**（不记 MAX+1）：空页可能只是渲染失败，
            #    记成"到底了"会让这一页被永久跳过。多扫一次的代价 << 漏采的风险。
            self._cursor_put(ckey, page_num)
            logger.info(f"📑 [{self.platform_name}] 翻页进度已记：下一批从第 "                  f"{self._cursor_get(ckey)} 页继续（本批结束于第 {page_num} 页）")

            # ⚠️ 必须按域名过滤：`context.cookies()` 不带参数会返回**浏览器里所有域**的 cookie。
            # 实测本机该浏览器累积了 276 条（globalsources / alibaba / aiqicha / tianyancha / baidu / qcc …），
            # 全部拼进 Cookie 头达 16KB，GS 直接返回 400 Request Header Or Cookie Too Large，
            # 表现为"独立站/中文工商名/注册地址整列为空"，而 GS 代码本身一行没改也会突然坏。
            raw_cookies = await context.cookies("https://www.globalsources.com")
            session_cookies = {
                c["name"]: c["value"]
                for c in raw_cookies
                if "globalsources.com" in (c.get("domain") or "")
            }
            cookie_size = len("; ".join(f"{k}={v}" for k, v in session_cookies.items()))
            if cookie_size > COOKIE_HEADER_LIMIT:
                # 兜底：即使过滤后仍超限（自己站点 cookie 太多），宁可不带 cookie —— 实测不带也返回 200
                logger.warning(f"⚠️ [GS] globalsources cookie 头达 {cookie_size} 字节，超过 {COOKIE_HEADER_LIMIT} 上限，"
                               f"本次详情请求不带 cookie（实测不影响取数）")
                session_cookies = {}
            else:
                logger.debug(f"[GS] 详情请求将携带 {len(session_cookies)} 条 GS cookie（{cookie_size} 字节）")
            user_agent = await page.evaluate("navigator.userAgent")

            try:
                await page.unroute("**/*")
            except Exception:
                pass

            # 关掉本批的专用标签页（见上面"必须自建"的说明）。
            # 页面上的 cookie 这时已经读进 `session_cookies`，关掉不影响后面的 httpx 详情请求。
            try:
                await page.close()
            except Exception:
                pass

        if not candidate_sellers:
            return []

        # 把限速参数一并打出来。这一阶段看起来"卡住"时，原因几乎总是**全局限速器串行**，
        # 而不是"异步失效"（实测 12 请求 / 并发 4 → 12.6s，均值 1.14s/请求）。
        logger.info(f"🚀 [{self.platform_name}] 启动 HTTPX 异步提取 {len(candidate_sellers)} 家商户工商与独立站...")
        logger.info(f"      ⏳ {config.detail_rate_status()}"              f"；每家约 3~4 个请求 → 本阶段预计 "
              f"{len(candidate_sellers) * 3.5 * config.DETAIL_RATE_MIN_INTERVAL / 60:.1f} 分钟量级")
        semaphore = asyncio.Semaphore(self.concurrency)
        custom_headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
        }

        async with httpx.AsyncClient(cookies=session_cookies, headers=custom_headers, follow_redirects=True, timeout=15.0) as client:
            async def process_item(idx: int, s: dict):
                async with semaphore:
                    detail = await self._parse_detail(client, s["store_url"])
                    # 中文工商名也登记一份（与候选阶段的英文名各占一条哈希）。
                    # 两者哈希不同（实测 `...CO., LIMITED` 与 `...CO.,LTD` 都算不同哈希），
                    # 都记上才挡得住"换个写法又来一遍"。
                    # 回滚同样覆盖它 —— pipeline 撤指纹时会连 `registered_company` 一起撤。
                    if detail.get("registered_company"):
                        dedup.add(detail["registered_company"])

                    final_products = s.get("raw_products") or detail.get("raw_products") or ""

                    return RawSupplierLead(
                        company=s["company"],
                        platform=self.platform_name,
                        store_url=s["store_url"],
                        registered_company=detail.get("registered_company", ""),
                        registered_address=detail.get("registered_address", ""),
                        official_website=detail.get("official_website", ""),
                        card_product=clean_kw,
                        platform_years=s.get("platform_years", ""),
                        raw_products=final_products
                    )

            tasks = [process_item(i, seller) for i, seller in enumerate(candidate_sellers, 1)]
            leads = await asyncio.gather(*tasks)

            # ——「未取到中文工商名 → 同轮内立即重爬一次」——
            # 空值主因是 403/429 限流与超时（页面本身没这个字段的情况也存在，
            # 重抓正好能区分：HTTP 正常但仍为空 ⇒ 页面没有，不是我们被拒）。
            # 复用同一个 client：cookie/UA 都已就绪，不必为了重试再开一次浏览器。
            missing_idx = [i for i, ld in enumerate(leads) if not (ld.registered_company or "").strip()]
            if missing_idx:
                logger.info(f"🔁 [{self.platform_name}] {len(missing_idx)} 家未取到中文工商名，同轮内立即重爬一次...")
                await self.human_delay(*NAME_RETRY_COOLDOWN, desc="重爬冷却")

                async def _retry_one(i: int):
                    async with semaphore:
                        return i, await self._parse_detail(client, leads[i].store_url)

                recovered = 0
                for i, detail in await asyncio.gather(*[_retry_one(i) for i in missing_idx]):
                    name = (detail.get("registered_company") or "").strip()
                    if not name:
                        continue
                    leads[i].registered_company = name
                    # 顺带把同一次重抓里拿到的其它字段补上（仅在原值为空时写，不覆盖已有值）
                    if not leads[i].registered_address:
                        leads[i].registered_address = detail.get("registered_address", "")
                    if not leads[i].official_website:
                        leads[i].official_website = detail.get("official_website", "")
                    # 重爬补到的中文名也登记一份（与上面详情阶段同口径）。
                    # 这家最终若被剔除，pipeline 会在剔除时连它一起撤掉。
                    dedup.add(name)
                    recovered += 1

                logger.info(f"      ↳ 重爬补回中文工商名 {recovered}/{len(missing_idx)} 家")
                if recovered < len(missing_idx):
                    logger.warning(
                        f"⚠️ [GS] 重爬后仍有 {len(missing_idx) - recovered} 家无中文工商名，将按策略剔除（不入库）。"
                        f"判定依据：上方若有『详情页抓取失败』告警 = 被限流/超时；"
                        f"若无告警但 profile/contact 字节数正常 = 页面本身没有该字段。"
                    )

        # 详情阶段小结：把"独立站取到几家/工商名取到几家"显式说出来。
        # 之前这一层完全没有汇总，字段空着也看不出是解析问题还是抓取被拒。
        site_ok = sum(1 for lead in leads if (lead.official_website or "").strip())
        name_ok = sum(1 for lead in leads if (lead.registered_company or "").strip())
        logger.info(f"📊 [{self.platform_name}] 详情提取完成: {len(leads)} 家 | "              f"工商全称 {name_ok}/{len(leads)} | 独立站 {site_ok}/{len(leads)}")
        if name_ok == 0 and leads:
            logger.warning(
                "⚠️ [GS] 本批详情页全部没有取到工商全称，通常意味着详情请求被拒（403/429）或超时；"
                "请查看上面的『详情页抓取失败』告警确认状态码。"
            )
        return leads