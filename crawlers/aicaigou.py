import asyncio
import html
import random
import re
from urllib.parse import quote_plus, urljoin
import httpx
from playwright.async_api import async_playwright

from core.base_crawler import BaseCrawler
from core.factory import CrawlerFactory
from models import RawSupplierLead
from utils.dedup import dedup


@CrawlerFactory.register("aicaigou")
class AiCaiGouCrawler(BaseCrawler):
    platform_name = "爱采购"
    platform_id = "aicaigou"

    # 外部独立站清洗黑名单（排除百度自营及各大公域平台）
    DOMAIN_BLACKLIST = {
        "baidu.com", "baidubce.com", "b2b.baidu.com", "bdstatic.com",
        "globalsources.com", "alibaba.com", "1688.com", "licdn.com",
        "google.com", "tencent.com", "qq.com"
    }

    def _format_clean_url(self, href: str) -> str:
        if not href:
            return ""
        href = href.strip()
        if href.startswith("//"):
            return f"https:{href}"
        elif href.startswith("/"):
            return f"https://b2b.baidu.com{href}"
        elif not href.startswith("http"):
            return f"https://b2b.baidu.com/{href}"
        return href

    def _is_valid_website(self, url: str) -> bool:
        if not url:
            return False
        clean = re.sub(r'^[:：\s/]+', '', url).strip().strip("'\"<>")
        url_lower = clean.lower()
        if any(bad in url_lower for bad in self.DOMAIN_BLACKLIST):
            return False
        if url_lower.startswith(("javascript:", "mailto:", "tel:", "#")):
            return False
        return bool(re.search(r'(?:https?://)?(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s]*)?', url_lower))

    async def _fetch_html(self, client: httpx.AsyncClient, url: str, retries: int = 2) -> str:
        if not url:
            return ""
        for attempt in range(retries + 1):
            try:
                resp = await client.get(url, timeout=12.0)
                if resp.status_code == 200:
                    return resp.text
                elif resp.status_code in [429, 503]:
                    await asyncio.sleep(1.0 * (attempt + 1))
            except Exception:
                if attempt == retries:
                    return ""
                await asyncio.sleep(0.8)
        return ""

    async def _parse_detail(self, client: httpx.AsyncClient, store_url: str) -> dict:
        """穿透爱采购店铺详情及“联系我们”页面"""
        info = {
            "registered_address": "",
            "official_website": "",
            "email": "",
            "contact_person": ""
        }
        if not store_url or not store_url.startswith("http"):
            return info

        # 拼接“联系我们”子页 URL
        sep = "&" if "?" in store_url else "?"
        contact_url = f"{store_url}{sep}tpath=contact" if "tpath=contact" not in store_url else store_url

        contact_html, home_html = await asyncio.gather(
            self._fetch_html(client, contact_url),
            self._fetch_html(client, store_url)
        )

        contact_text = self.html_to_clean_text(contact_html)
        home_text = self.html_to_clean_text(home_html)
        combined_text = f"{contact_text}\n{home_text}"

        # 1. 提取联系地址 / 工厂地址
        addr_patterns = [
            r'(?:联系地址|公司地址|经营地址|注册地址|地址)\s*[:：]?\s*([^\r\n<]+)',
            r'Add(?:ress)?\s*[:：]?\s*([^\r\n<]+)'
        ]
        for pat in addr_patterns:
            m = re.search(pat, combined_text, re.I)
            if m:
                cand = m.group(1).strip()
                if len(cand) >= 5 and not any(w in cand for w in ["点击查看", "导航", "暂无"]):
                    info["registered_address"] = cand
                    break

        # 2. 提取联系人
        person_m = re.search(r'(?:联系人|联\s*系\s*人)\s*[:：]?\s*([^\r\n<\s]{2,10})', combined_text)
        if person_m:
            info["contact_person"] = person_m.group(1).strip()

        # 3. 提取企业邮箱
        mail_m = re.search(r'(?:电子邮箱|邮箱|E-?mail)\s*[:：]?\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', combined_text, re.I)
        if mail_m:
            info["email"] = mail_m.group(1).strip()

        # 4. 提取独立站官网（若商家在简介或联系页面中填写了外链）
        website_patterns = [
            r'(?:企业官网|官方网站|官网|公司网址|网址)\s*[:：]?\s*([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}(?:/[^\s\r\n<"\'>]*)?)',
            r'(?:Other\s+)?(?:homepage\s+)?website\s*[:：]?\s*(https?://[^\s\r\n<"\'>]+|[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})'
        ]
        for pat in website_patterns:
            m = re.search(pat, combined_text, re.I)
            if m:
                cand = m.group(1).strip()
                if self._is_valid_website(cand):
                    clean_site = cand if cand.startswith("http") else f"http://{cand}"
                    info["official_website"] = clean_site
                    break

        return info

    async def scrape(
        self,
        keyword: str,
        max_count: int,
        year_in_business: str = "",
        supplier_location: str = ""
    ) -> list[RawSupplierLead]:
        self.ensure_chrome_running()
        clean_kw = keyword.strip()

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
            max_search_pages = 20

            while len(candidate_sellers) < max_count and page_num <= max_search_pages:
                # 构造直达“找厂家”模式的爱采购检索参数
                search_url = (
                    f"https://b2b.baidu.com/c?q={quote_plus(clean_kw)}"
                    f"&category=%E5%8E%82%E5%AE%B6&p={page_num}"
                )
                print(f"📑 [{self.platform_name}] 检索第 {page_num} 页: {search_url}")

                try:
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
                    await self.human_delay(2.0, 3.0, desc=f"第 {page_num} 页就绪")
                except Exception as e:
                    print(f"⚠️ 第 {page_num} 页加载超时: {e}")
                    break

                # 模拟滚屏触发列表卡片完全渲染
                for _ in range(3):
                    await page.mouse.wheel(0, random.randint(700, 1000))
                    await self.human_delay(0.5, 0.8)

                # 智能提取爱采购商户卡片要素
                raw_cards = await page.evaluate("""
                    () => {
                        const results = [];
                        // 寻找所有带有“进入店铺”标记的卡片
                        const enterBtns = Array.from(document.querySelectorAll('a, button, div')).filter(
                            el => el.innerText && el.innerText.trim().includes('进入店铺')
                        );

                        for (const btn of enterBtns) {
                            const card = btn.closest('div[class*="item"], div[class*="card"], li, div.c-result') || 
                                         btn.parentElement?.parentElement?.parentElement;
                            if (!card) continue;

                            // 1. 获取店铺链接
                            let storeUrl = '';
                            if (btn.tagName.toLowerCase() === 'a') {
                                storeUrl = btn.getAttribute('href') || '';
                            }
                            if (!storeUrl) {
                                const shopLink = card.querySelector('a[href*="/shop"]');
                                if (shopLink) storeUrl = shopLink.getAttribute('href') || '';
                            }

                            // 2. 获取公司中文名称
                            let compName = '';
                            const titleEl = card.querySelector('h2, h3, [class*="title"], [class*="name"], a[href*="/shop"]');
                            if (titleEl) {
                                compName = titleEl.innerText.trim();
                            }
                            // 滤掉杂质标记词
                            compName = compName.replace(/在线咨询|进入店铺|热搜商家|真实工厂|实体认证|厂家直供|资质保证/g, '').trim();

                            const fullText = card.innerText || '';

                            // 3. 提取主营业务 / 主要经营
                            let products = '';
                            const prodMatch = fullText.match(/(?:主要经营|主营业务|主营产品|主营)\\s*[:：]\\s*([^\\n\\r]+)/);
                            if (prodMatch) {
                                products = prodMatch[1].trim();
                            }

                            // 4. 提取年限或成立年份折算年限
                            let years = '';
                            const estMatch = fullText.match(/成立时间\\s*[:：]\\s*(\\d{4})[-/.]/);
                            if (estMatch) {
                                const estYear = parseInt(estMatch[1]);
                                const currentYear = 2026;
                                if (estYear > 1980 && estYear <= currentYear) {
                                    years = `${currentYear - estYear}年`;
                                }
                            }
                            const badgeMatch = fullText.match(/(\\d+)\\s*年(?:真实工厂|实力工厂|老店)/);
                            if (badgeMatch && !years) {
                                years = `${badgeMatch[1]}年`;
                            }

                            if (compName && compName.length >= 4 && storeUrl) {
                                results.push({
                                    company: compName,
                                    store_url: storeUrl,
                                    platform_years: years,
                                    raw_products: products
                                });
                            }
                        }
                        return results;
                    }
                """)

                if not raw_cards:
                    print(f"[-] 第 {page_num} 页未检测到有效卡片，可能到达末页或触发验证。")
                    break

                page_added = 0
                for item in raw_cards:
                    if len(candidate_sellers) >= max_count:
                        break

                    comp_name = item["company"]
                    clean_url = self._format_clean_url(item["store_url"]).rstrip('/')

                    if dedup.is_seen(comp_name) or comp_name in seen_companies:
                        continue

                    seen_companies.add(comp_name)
                    candidate_sellers.append({
                        "company": comp_name,
                        "store_url": clean_url,
                        "platform_years": item.get("platform_years", ""),
                        "raw_products": item.get("raw_products", "")
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

        print(f"🚀 [{self.platform_name}] 启动 HTTPX 异步下钻 {len(candidate_sellers)} 家商户联系人与官网...")
        semaphore = asyncio.Semaphore(self.concurrency)
        custom_headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
        }

        async with httpx.AsyncClient(cookies=session_cookies, headers=custom_headers, follow_redirects=True, timeout=12.0) as client:
            async def process_item(s: dict):
                async with semaphore:
                    detail = await self._parse_detail(client, s["store_url"])
                    dedup.add(s["company"])

                    return RawSupplierLead(
                        company=s["company"],
                        platform=self.platform_name,
                        store_url=s["store_url"],
                        registered_company=s["company"],
                        registered_address=detail.get("registered_address", ""),
                        official_website=detail.get("official_website", ""),
                        card_product=clean_kw,
                        platform_years=s.get("platform_years", ""),
                        raw_products=s.get("raw_products", "")
                    )

            tasks = [process_item(seller) for seller in candidate_sellers]
            return await asyncio.gather(*tasks)