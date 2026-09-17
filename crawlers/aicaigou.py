import asyncio
import random
import re
from urllib.parse import quote_plus
from playwright.async_api import async_playwright

from core.base_crawler import BaseCrawler
from core.factory import CrawlerFactory
from models import RawSupplierLead
from utils.dedup import dedup


@CrawlerFactory.register("aicaigou")
class AiCaiGouCrawler(BaseCrawler):
    platform_name = "爱采购"
    platform_id = "aicaigou"

    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )

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

    def _clean_chinese_company_name(self, raw_name: str) -> str:
        if not raw_name:
            return ""
        clean = raw_name.strip()
        suffix_match = re.search(
            r'^([^\d\n\r]+?(?:有限责任公司|股份有限公司|科技有限公司|电子有限公司|商贸有限公司|实业有限公司|有限公司|加工厂|制造厂|五金厂|模具厂|鞋厂|服装厂|皮具厂|塑料厂|工厂|厂|经营部|商行|总汇))',
            clean
        )
        if suffix_match:
            return suffix_match.group(1).strip()
        return clean

    async def scrape(
        self,
        keyword: str,
        max_count: int,
        year_in_business: str = "",
        supplier_location: str = ""
    ) -> list[RawSupplierLead]:
        self.ensure_chrome_running()
        clean_kw = keyword.strip()

        candidate_leads: list[RawSupplierLead] = []
        seen_companies = set()

        async with async_playwright() as p:
            print(f"🔌 [{self.platform_name}] 接入 Chrome (CDP 端口: {self.cdp_port})...")
            try:
                browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()
            except Exception as e:
                print(f"❌ [{self.platform_name}] 无法连接到 Chrome 端口 9222: {e}")
                return []

            try:
                await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
                # 🌟 关键：不拦截静态资源，维持真实浏览器指纹完整度

                page_num = 1
                max_search_pages = 25

                while len(candidate_leads) < max_count and page_num <= max_search_pages:
                    search_url = (
                        f"https://b2b.baidu.com/c?q={quote_plus(clean_kw)}"
                        f"&category=%E5%8E%82%E5%AE%B6&p={page_num}"
                    )
                    print(f"📑 [{self.platform_name}] 检索第 {page_num} 页: {search_url}")

                    try:
                        await page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
                        await self.human_delay(2.5, 4.0, desc=f"第 {page_num} 页就绪")
                    except Exception as e:
                        print(f"⚠️ 第 {page_num} 页加载中断: {e}")
                        break

                    # 模拟平滑滚动，加载完整卡片
                    for _ in range(3):
                        await page.mouse.wheel(0, random.randint(600, 900))
                        await self.human_delay(0.6, 1.2)

                    # 检查是否遇到滑块/图形验证
                    has_captcha = await page.evaluate("""
                        () => !!(document.querySelector('.vcode-wrap, #captcha, [class*="vcode"], [class*="security"]') || 
                                 document.title.includes('验证') || document.body.innerText.includes('安全验证'))
                    """)
                    if has_captcha:
                        print(f"🚨 [{self.platform_name}] 检测到页面弹出安全验证！请在打开的 Chrome 窗口中手动完成验证...")
                        for _ in range(30):
                            await asyncio.sleep(2.0)
                            still_blocked = await page.evaluate("""
                                () => !!(document.querySelector('.vcode-wrap, #captcha, [class*="vcode"]') || 
                                         document.body.innerText.includes('安全验证'))
                            """)
                            if not still_blocked:
                                print(f"✅ [{self.platform_name}] 验证通过，继续流水线！")
                                break

                    raw_cards = await page.evaluate(r"""
                        () => {
                            const results = [];
                            const h2Elements = document.querySelectorAll('h2.title-shop-name-inner, [class*="shop-name-inner"]');

                            for (const h2 of h2Elements) {
                                let compName = h2.getAttribute('title') || h2.innerText || '';
                                compName = compName.trim();

                                const card = h2.closest('div[class*="item"], div[class*="card"], li, div.c-result') || 
                                             h2.parentElement?.parentElement?.parentElement?.parentElement;
                                if (!card) continue;

                                const shopLink = h2.closest('a') || card.querySelector('a[href*="/shop/"], a.link');
                                let storeUrl = shopLink ? (shopLink.getAttribute('href') || '') : '';

                                const fullText = card.innerText || '';

                                // 提取主营文本
                                let products = '';
                                const prodMatch = fullText.match(/(?:主要经营|主营业务|主营产品|店主营|现主营|主营品牌|主营)\s*[:：]\s*([^\n\r]+)/);
                                if (prodMatch) {
                                    products = prodMatch[1].trim();
                                }

                                // 提取真实商品标题以辅助定性
                                const itemTitles = Array.from(card.querySelectorAll('div.title, p.title, [class*="product-name"], [class*="item-title"], a[title]'))
                                    .map(el => el.getAttribute('title') || el.innerText || '')
                                    .map(t => t.trim())
                                    .filter(t => t.length > 4 && !t.includes('咨询') && !t.includes('店铺') && !t.includes(compName));

                                if (itemTitles.length > 0) {
                                    const sampleTitles = Array.from(new Set(itemTitles)).slice(0, 3).join('; ');
                                    products = products ? `${products} | 示例: ${sampleTitles}` : `示例: ${sampleTitles}`;
                                }

                                // 提取年限
                                let years = '';
                                const estMatch = fullText.match(/成立时间\s*[:：]\s*(\d{4})[-/.]/);
                                if (estMatch) {
                                    const estYear = parseInt(estMatch[1]);
                                    const currentYear = 2026;
                                    if (estYear > 1980 && estYear <= currentYear) {
                                        years = `${currentYear - estYear}年`;
                                    }
                                }
                                const badgeMatch = fullText.match(/(\d+)\s*年(?:真实工厂|实力工厂|老店|综合体验)/);
                                if (badgeMatch && !years) {
                                    years = `${badgeMatch[1]}年`;
                                }

                                // 提取卡片上的地区信息
                                let location = '';
                                const locMatch = fullText.match(/(?:广东|浙江|江苏|山东|河北|福建|河南|上海|北京|四川|湖北|湖南|安徽|江西)[^\s\n\r]{1,10}/);
                                if (locMatch) {
                                    location = locMatch[0].trim();
                                }

                                if (compName && storeUrl) {
                                    results.push({
                                        company: compName,
                                        store_url: storeUrl,
                                        platform_years: years,
                                        raw_products: products,
                                        registered_address: location
                                    });
                                }
                            }
                            return results;
                        }
                    """)

                    if not raw_cards:
                        print(f"[-] 第 {page_num} 页未提取到商家卡片。")
                        break

                    page_added = 0
                    for item in raw_cards:
                        if len(candidate_leads) >= max_count:
                            break

                        pure_comp = self._clean_chinese_company_name(item["company"])
                        clean_url = self._format_clean_url(item["store_url"]).rstrip('/')

                        if not pure_comp or "共" in pure_comp or len(pure_comp) < 4:
                            continue
                        if not any(pure_comp.endswith(sfx) for sfx in ["公司", "厂", "店", "部", "行", "中心"]):
                            continue

                        if dedup.is_seen(pure_comp) or pure_comp in seen_companies:
                            continue

                        seen_companies.add(pure_comp)
                        # ⚠️ 不在候选阶段写指纹库：这里只是"看到了这家"，
                        # 离"进了报表"还差详情、独立站、工商补全等好几道门槛。
                        # 统一由流水线在落盘成功后调用 `dedup.commit_lead_fingerprints()`。

                        candidate_leads.append(RawSupplierLead(
                            company=pure_comp,
                            platform=self.platform_name,
                            store_url=clean_url,
                            registered_company=pure_comp,
                            registered_address=item.get("registered_address", ""),
                            official_website="",  # 独立站交由后续 enrichment 或天眼查模块补全
                            card_product=clean_kw,
                            platform_years=item.get("platform_years", ""),
                            raw_products=item.get("raw_products", "")
                        ))
                        page_added += 1

                    print(f"✅ [{self.platform_name}] 候选入库: +{page_added} 家 (当前累计: {len(candidate_leads)}/{max_count})")

                    if len(candidate_leads) < max_count:
                        page_num += 1
                        await self.human_delay(2.5, 4.0, desc="翻页冷却")

            finally:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass

        return candidate_leads