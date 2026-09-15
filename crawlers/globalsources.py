import asyncio
import html as html_lib
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

logger = get_logger("crawler.gs")


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
            print(f"🔌 [{self.platform_name}] 接入 Chrome (CDP 端口: {self.cdp_port})...")
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}")
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()

            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
            await page.route("**/*", self.block_resources)

            page_num = 1
            max_search_pages = 25

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
                print(f"📑 [{self.platform_name}] 检索第 {page_num} 页: {search_url}")

                try:
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
                    await self.human_delay(2.0, 3.0, desc=f"第 {page_num} 页就绪")
                except Exception as e:
                    print(f"⚠️ 第 {page_num} 页加载超时: {e}")
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

                print(f"✅ [{self.platform_name}] 候选入库: +{page_added} 家 (当前累计: {len(candidate_sellers)}/{max_count})")
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

        print(f"🚀 [{self.platform_name}] 启动 HTTPX 异步提取 {len(candidate_sellers)} 家商户工商与独立站...")
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
                    dedup.add(s["company"])
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

        # 详情阶段小结：把"独立站取到几家/工商名取到几家"显式说出来。
        # 之前这一层完全没有汇总，字段空着也看不出是解析问题还是抓取被拒。
        site_ok = sum(1 for lead in leads if (lead.official_website or "").strip())
        name_ok = sum(1 for lead in leads if (lead.registered_company or "").strip())
        print(f"📊 [{self.platform_name}] 详情提取完成: {len(leads)} 家 | "
              f"工商全称 {name_ok}/{len(leads)} | 独立站 {site_ok}/{len(leads)}")
        if name_ok == 0 and leads:
            logger.warning(
                "⚠️ [GS] 本批详情页全部没有取到工商全称，通常意味着详情请求被拒（403/429）或超时；"
                "请查看上面的『详情页抓取失败』告警确认状态码。"
            )
        return leads