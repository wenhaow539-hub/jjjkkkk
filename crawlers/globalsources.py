import asyncio
import random
import re
from urllib.parse import quote_plus, urljoin

import httpx
from playwright.async_api import async_playwright

from core.base_crawler import BaseCrawler
from core.factory import CrawlerFactory
from models import RawSupplierLead
from utils.dedup import dedup
from utils.logger import get_logger
from utils.parsing import find_labeled_value, soup_from_html
from utils.ratelimit import AsyncRateLimiter
from utils.robots import RobotsChecker

logger = get_logger("globalsources")


@CrawlerFactory.register("globalsources")
class GlobalSources(BaseCrawler):
    platform_name = "Global Sources"
    platform_id = "globalsources"

    def __init__(self, cdp_port: int = 9222, concurrency: int = 4):
        super().__init__(cdp_port=cdp_port, concurrency=concurrency)
        # 全局请求限速：所有详情页请求共享节奏，避免请求风暴（P1 反爬项）
        self._rate_limiter = AsyncRateLimiter(min_interval=1.2, jitter=0.35)
        self._robots = RobotsChecker()

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

    def _resolve_target_urls(self, store_url: str) -> tuple:
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

    async def _fetch_html(self, client: httpx.AsyncClient, url: str, retries: int = 3) -> str:
        """带 robots 合规检查、全局限速与指数退避的 HTML 抓取（失败必有日志，不再静默）。"""
        if not url:
            return ""
        if not await self._robots.can_fetch(client, url):
            logger.warning(f"🤖 [robots] {url} 被 robots.txt 禁止抓取，已跳过。")
            return ""
        for attempt in range(retries + 1):
            await self._rate_limiter.acquire()
            try:
                resp = await client.get(url, timeout=15.0)
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code in (429, 503):
                    retry_after = resp.headers.get("retry-after", "")
                    try:
                        base_delay = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        base_delay = 0.0
                    # 指数退避(2/4/8s) + 随机抖动，尊重服务端 Retry-After（P1：原为 1-2s 线性硬重试）
                    delay = max(base_delay, 2.0 * (2 ** attempt)) * random.uniform(0.8, 1.3)
                    logger.warning(f"⏳ [{resp.status_code}] {url} 被限流，指数退避 {delay:.1f}s (第 {attempt + 1} 次重试)")
                    await asyncio.sleep(delay)
                    continue
                if 400 <= resp.status_code < 500:
                    logger.debug(f"[HTTP {resp.status_code}] {url}")
                    return ""
                logger.warning(f"[HTTP {resp.status_code}] {url} (第 {attempt + 1} 次尝试)")
            except Exception as e:
                logger.warning(f"⚠️ 请求异常 {url}: {e.__class__.__name__}: {e} (第 {attempt + 1} 次尝试)")
            if attempt < retries:
                await asyncio.sleep(0.8 * (attempt + 1))
        logger.error(f"❌ 抓取失败(重试耗尽): {url}")
        return ""

    def _extract_field_from_text(self, text: str, label_patterns: list, stop_words: list) -> str:
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

    async def _parse_detail(self, client: httpx.AsyncClient, store_url: str) -> dict:
        info = {"registered_company": "", "registered_address": "", "official_website": ""}
        if not store_url or not store_url.startswith("http"):
            return info

        profile_url, contact_url = self._resolve_target_urls(store_url)
        profile_html, contact_html = await asyncio.gather(
            self._fetch_html(client, profile_url),
            self._fetch_html(client, contact_url)
        )

        home_html = ""
        if not profile_html or not contact_html:
            home_html = await self._fetch_html(client, store_url)
            if home_html:
                if not profile_html:
                    p_links = re.findall(r'href=["\']([^"\']*(?:company-profile|about-us)[^"\']*)["\']', home_html, re.I)
                    if p_links:
                        profile_html = await self._fetch_html(client, self._format_clean_url(urljoin(store_url, p_links[0])))
                if not contact_html:
                    c_links = re.findall(r'href=["\']([^"\']*(?:contact-us|contact)[^"\']*)["\']', home_html, re.I)
                    if c_links:
                        contact_html = await self._fetch_html(client, self._format_clean_url(urljoin(store_url, c_links[0])))

        profile_text = self.html_to_clean_text(profile_html)
        contact_text = self.html_to_clean_text(contact_html)
        home_text = self.html_to_clean_text(home_html) if home_html else ""

        comp_labels = [r'Registered\s*Company(?:\s*Name)?', r'Company\s*Name', r'Legal\s*Business\s*Name', r'Business\s*Name']
        stop_words = ["registration number", "business type", "year established", "country", "view more", "undefined", "null"]

        info["registered_company"] = self._extract_field_from_text(profile_text, comp_labels, stop_words)
        if not info["registered_company"]:
            info["registered_company"] = self._extract_field_from_text(contact_text, comp_labels, stop_words)
        if not info["registered_company"] and home_text:
            info["registered_company"] = self._extract_field_from_text(home_text, comp_labels, stop_words)
        # DOM 结构化兜底（BeautifulSoup）：页面改版导致行式匹配失效时的第二道防线
        if not info["registered_company"]:
            for soup in (soup_from_html(profile_html), soup_from_html(contact_html)):
                value = find_labeled_value(soup, comp_labels, stop_words)
                if value:
                    info["registered_company"] = self.clean_token(value)
                    break

        addr_labels = [
            r'Company\s*Registration\s*Address', r'Registered\s*Address', r'Registration\s*Address',
            r'Operational\s*Address', r'Factory\s*Address', r'Business\s*Address', r'Office\s*Address', r'Address'
        ]
        addr_stops = ["zip code", "country/region", "view more", "view less", "null", "production capacity", "* in china"]

        info["registered_address"] = self._extract_field_from_text(profile_text, addr_labels, addr_stops)
        if not info["registered_address"]:
            info["registered_address"] = self._extract_field_from_text(contact_text, addr_labels, addr_stops)
        if not info["registered_address"] and home_text:
            info["registered_address"] = self._extract_field_from_text(home_text, addr_labels, addr_stops)
        if not info["registered_address"]:
            for soup in (soup_from_html(profile_html), soup_from_html(contact_html)):
                value = find_labeled_value(soup, addr_labels, addr_stops)
                if value:
                    info["registered_address"] = self.clean_token(value)
                    break

        for candidate_text in [contact_text, profile_text, home_text]:
            if info["official_website"]: break
            other_m = re.search(r'Other\s+(?:homepage\s+)?website\s*[:：]?\s*([^\s\r\n<"\'>]+)', candidate_text, re.I)
            if other_m:
                cand = self.normalize_website(other_m.group(1), exclude_domain="globalsources.com")
                if cand: info["official_website"] = cand

        if not info["official_website"]:
            for cand_html in [contact_html, profile_html, home_html]:
                if info["official_website"] or not cand_html: break
                html_m = re.search(r'Other\s+(?:homepage\s+)?website[\s\S]*?(?:href=["\']([^"\']+)["\']|>([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}[^<\s]*))', cand_html, re.I)
                if html_m:
                    cand = self.normalize_website(html_m.group(1) or html_m.group(2), exclude_domain="globalsources.com")
                    if cand: info["official_website"] = cand

        return info

    async def scrape(self, keyword: str, max_count: int) -> list:
        self.ensure_chrome_running()
        clean_kw = keyword.lower().replace("manufacturer", "").strip()

        # robots.txt 合规预检：检索页被禁则直接终止，不做任何抓取（P1 合规项）
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as rc:
            search_probe = f"https://www.globalsources.com/searchList/suppliers?keyWord={quote_plus(clean_kw)}&pageNum=1"
            if not await self._robots.can_fetch(rc, search_probe):
                logger.error("🚫 [robots.txt] Global Sources 检索页被 robots.txt 禁止抓取，本次运行已终止。")
                raise RuntimeError("robots.txt 禁止抓取目标检索页，请检查 https://www.globalsources.com/robots.txt")

        candidate_sellers = []
        seen_companies = set()

        async with async_playwright() as p:
            logger.info(f"🔌 [{self.platform_name}] 接入 Chrome (CDP 端口: {self.cdp_port})...")
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}")
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()

            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
            await page.route("**/*", self.block_resources)

            page_num = 1
            max_search_pages = 25

            while len(candidate_sellers) < max_count and page_num <= max_search_pages:
                search_url = f"https://www.globalsources.com/searchList/suppliers?keyWord={quote_plus(clean_kw)}&pageNum={page_num}"
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
                    break

                page_added = 0
                for el in candidate_elements:
                    if len(candidate_sellers) >= max_count: break

                    href = await el.get_attribute("href") or ""
                    if any(pk in href.lower() for pk in ["/pdtl/", "/product_", "productdetail", "/product/"]):
                        continue

                    text = (await el.inner_text()).strip()
                    title_attr = (await el.get_attribute("title") or "").strip()
                    comp_name = title_attr if self.is_valid_company_name(title_attr) else text
                    clean_url = self._format_clean_url(href).rstrip('/')

                    if dedup.is_seen(comp_name): continue

                    if self.is_valid_company_name(comp_name) and comp_name not in seen_companies and href:
                        seen_companies.add(comp_name)
                        candidate_sellers.append({"company": comp_name, "store_url": clean_url})
                        page_added += 1

                logger.info(f"✅ [{self.platform_name}] 候选入库: +{page_added} 家 (当前累计: {len(candidate_sellers)}/{max_count})")
                if len(candidate_sellers) < max_count:
                    page_num += 1
                    await self.human_delay(1.5, 2.5, desc="翻页冷却")

            raw_cookies = await context.cookies()
            session_cookies = {c['name']: c['value'] for c in raw_cookies}
            user_agent = await page.evaluate("navigator.userAgent")

            try:
                await page.unroute("**/*")
            except Exception:
                pass

        if not candidate_sellers:
            return []

        logger.info(f"🚀 [{self.platform_name}] 启动 HTTPX 异步提取 {len(candidate_sellers)} 家商户工商与独立站...")
        semaphore = asyncio.Semaphore(self.concurrency)
        custom_headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
        }

        async with httpx.AsyncClient(cookies=session_cookies, headers=custom_headers, follow_redirects=True, timeout=15.0) as client:
            async def process_item(idx: int, s: dict):
                # 异常隔离：单商户失败仅返回 None，不再让 gather 炸掉整批（P1 稳定性项）
                async with semaphore:
                    try:
                        detail = await self._parse_detail(client, s["store_url"])
                        dedup.add(s["company"])
                        if detail.get("registered_company"):
                            dedup.add(detail["registered_company"])

                        return RawSupplierLead(
                            company=s["company"],
                            platform=self.platform_name,
                            store_url=s["store_url"],
                            registered_company=detail.get("registered_company", ""),
                            registered_address=detail.get("registered_address", ""),
                            official_website=detail.get("official_website", ""),
                            card_product=clean_kw
                        )
                    except Exception as e:
                        logger.error(f"❌ [详情提取失败] {s.get('company')}: {e!r}")
                        return None

            tasks = [process_item(i, seller) for i, seller in enumerate(candidate_sellers, 1)]
            results = await asyncio.gather(*tasks)

        leads = [r for r in results if r is not None]
        logger.info(f"✅ [{self.platform_name}] 详情提取完成: {len(leads)}/{len(candidate_sellers)} 家成功")

        # 解析质量观测：字段缺失率是页面改版的第一告警信号（原为静默空值，无法察觉失效）
        if leads:
            miss_name = sum(1 for l in leads if not l.registered_company)
            miss_addr = sum(1 for l in leads if not l.registered_address)
            miss_site = sum(1 for l in leads if not l.official_website)
            logger.info(
                f"📊 [解析质量] 公司名缺失 {miss_name}/{len(leads)} | "
                f"地址缺失 {miss_addr}/{len(leads)} | 独立站缺失 {miss_site}/{len(leads)}"
            )
            if miss_name / len(leads) > 0.5:
                logger.warning("⚠️ 超过一半商户的公司名解析失败，页面结构可能已改版，请检查标签规则与选择器！")

        return leads
