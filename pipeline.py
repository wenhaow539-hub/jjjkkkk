import asyncio
from datetime import datetime
import os
import random
import re
from urllib.parse import quote_plus, urljoin, urlparse
import httpx
from openpyxl.styles import Alignment, PatternFill
import pandas as pd
from playwright.async_api import async_playwright

import config
from adapters import BaseCrawler, CrawlerFactory
import crawlers  # 触发爬虫模块自动注册
from evaluator import evaluate_supplier_icp
from models import RawSupplierLead

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', re.IGNORECASE)
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.css', '.js', '.ico')
JUNK_EMAIL_DOMAINS = ('wixpress.com', 'sentry.io', 'example.com', 'domain.com', 'google.com', 'myshopify.com')
INVALID_PREFIXES = ('noreply', 'no-reply', 'mailer-daemon', 'donotreply')

# 匹配工信部 ICP 备案号及公安网安备案号
ICP_REGEX = re.compile(
    r'([京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Za-z]?ICP备\s*\d+\s*号?(?:-\d+)?|'
    r'[A-Za-z\u4e00-\u9fa5]*ICP[备证]\s*\d+\s*号?(?:-\d+)?|'
    r'[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]\s*公网安备\s*\d+\s*号)',
    re.IGNORECASE
)

TARGET_COLUMNS = [
    "平台名称",
    "公司英文名",
    "平台网址",
    "公司中文名",
    "公司注册地址",
    "天眼查联系人",
    "天眼查联系人职位",
    "独立站",
    "网址是否有备案",
    "官网联系方式",
    "天眼查联系方式",
    "搜索的关键词",
    "email",
    "入库时间"
]


# ==========================================
# 电话归一与智能排重模块
# ==========================================
def get_canonical_phone(raw_phone: str) -> str:
    """提取号码的核心数字指纹，剥离 +86、0086、前导0与标点空格差异"""
    if not raw_phone:
        return ""
    main_part = re.split(r'(?:ext|分机|转)', raw_phone, flags=re.I)[0]
    digits = re.sub(r'\D', '', main_part)

    # 剥离国家代码 0086 / 86
    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) >= 11:
        digits = digits[2:]

    # 剥离国内固话区号前导 0 (例如 0755 -> 755, 0519 -> 519)
    if digits.startswith("0") and len(digits) >= 10:
        digits = digits[1:]

    return digits


def score_phone_format(val: str) -> int:
    """对同一物理号码的不同展示排版打分，优先保留排版规范的版本"""
    score = 0
    if "+" in val:
        score += 5
    if "-" in val:
        score += 3
    if " " in val:
        score += 2
    # 扣分：+86 紧跟 0 是不规范写法 (+86 0755...)
    if re.search(r'(?:\+86|86)[\s\-]*0\d', val):
        score -= 4
    # 纯无分隔符长串减分
    if re.match(r'^\+?\d+$', val.strip()):
        score -= 1
    return score


def deduplicate_phone_list(phones: list[str]) -> list[str]:
    """根据核心数字指纹去重，同号码优选排版质量最高的版本"""
    seen_dict: dict[str, str] = {}

    for p in phones:
        clean_p = re.sub(r'^[^\d+]+|[^\d]+$', '', p.strip())
        fingerprint = get_canonical_phone(clean_p)

        if not fingerprint or len(fingerprint) < 7:
            continue

        if fingerprint not in seen_dict:
            seen_dict[fingerprint] = clean_p
        else:
            existing_val = seen_dict[fingerprint]
            if score_phone_format(clean_p) > score_phone_format(existing_val):
                seen_dict[fingerprint] = clean_p

    return list(seen_dict.values())


# ==========================================
# 独立站深度挖掘与解析器
# ==========================================
class WebsiteEnricher:
    """利用 HTTPX 穿透独立站，提取商业邮箱、官网联系方式（去重规范）与 ICP 备案"""

    def __init__(self, concurrency: int = 5):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.timeout = httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
        }

    def _extract_valid_emails(self, text: str) -> list[str]:
        raw_matches = EMAIL_REGEX.findall(text)
        valid_emails = set()
        for e in raw_matches:
            e_lower = e.lower().strip()
            if any(e_lower.endswith(ext) for ext in IMAGE_EXTS):
                continue
            if any(junk in e_lower for junk in JUNK_EMAIL_DOMAINS):
                continue
            prefix = e_lower.split('@')[0]
            if any(prefix == inv for inv in INVALID_PREFIXES):
                continue
            if len(e_lower) <= 50:
                valid_emails.add(e_lower)
        return list(valid_emails)

    def _extract_valid_phones(self, html: str) -> list[str]:
        found_phones = []

        # 1. 点击拨号链接: a href="tel:..."
        for tel in re.findall(r'href=["\']tel:([^"\'>]+)["\']', html, re.I):
            clean_p = re.sub(r'[^\d+\-\s()]', '', tel.strip())
            if len(re.sub(r'\D', '', clean_p)) >= 7:
                found_phones.append(clean_p)

        # 2. WhatsApp 链接提取
        wa_pattern = r'(?:wa\.me/(?:send\?.*?[?&]phone=)?|api\.whatsapp\.com/send\?.*?[?&]phone=)([+\d]+)'
        for wa in re.findall(wa_pattern, html, re.I):
            clean_wa = wa.strip()
            if not clean_wa.startswith("+") and clean_wa.startswith("86"):
                clean_wa = "+" + clean_wa
            if len(re.sub(r'\D', '', clean_wa)) >= 8:
                found_phones.append(clean_wa)

        # 3. 剥离 script/style 代码块
        clean_visible_text = re.sub(
            r'<(script|style|svg|noscript)[^>]*>.*?</\1>', '', html,
            flags=re.DOTALL | re.IGNORECASE
        )
        clean_visible_text = re.sub(r'<[^>]+>', ' ', clean_visible_text)

        # 4. 正文中带标签的号码
        labeled_matches = re.findall(
            r'(?:phone|telephone|mobile|tel|whatsapp|cell|contact)\s*[:：]?\s*([+\d\s\-\(\)\.]{7,25})',
            clean_visible_text, re.I
        )
        for p in labeled_matches:
            digits = re.sub(r'\D', '', p)
            if 7 <= len(digits) <= 16 and not digits.startswith("202") and not digits.startswith("201"):
                found_phones.append(re.sub(r'\s+', ' ', p).strip())

        # 5. 国际格式号码 (+86 手机及 0755 等座机，如 +86 755-23210872, +86 13632694344)
        for intl in re.findall(r'\+86[\s\-]?(?:1[3-9]\d[\s\-]?\d{4}[\s\-]?\d{4}|[1-9]\d{1,3}[\s\-]?\d{7,8})', clean_visible_text):
            found_phones.append(re.sub(r'\s+', ' ', intl).strip())

        # 6. 国内标准带区号固话格式 (如 0755-23210872)
        for domestic in re.findall(r'(?:0[1-9]\d{1,2}[\s\-])\d{7,8}', clean_visible_text):
            found_phones.append(domestic.strip())

        return [ph for ph in found_phones if not any(b in ph for b in ["123456", "000000", "888888"])]

    async def _fetch_html_resilient(self, client: httpx.AsyncClient, url: str) -> tuple[int, str, str]:
        candidates = [url]
        if url.startswith("https://"):
            candidates.append("http://" + url[8:])
        elif url.startswith("http://"):
            candidates.append("https://" + url[7:])

        parsed = urlparse(url)
        if parsed.netloc:
            if parsed.netloc.startswith("www."):
                no_www = parsed.netloc[4:]
                candidates.append(parsed._replace(netloc=no_www).geturl())
            else:
                www_net = f"www.{parsed.netloc}"
                candidates.append(parsed._replace(netloc=www_net).geturl())

        last_error = "未知错误"
        for cand in dict.fromkeys(candidates):
            try:
                resp = await client.get(cand, headers=self.headers, timeout=self.timeout)
                if resp.status_code == 200:
                    return 200, resp.text, cand
                last_error = f"HTTP_{resp.status_code}"
            except Exception as e:
                last_error = str(e)

        return 0, "", last_error

    async def enrich_lead(self, client: httpx.AsyncClient, website: str) -> dict:
        result = {"email": "", "site_phone": "", "icp": "无"}
        if not website or not website.startswith("http"):
            return result

        async with self.semaphore:
            status_code, home_html, final_err = await self._fetch_html_resilient(client, website)
            if status_code != 200 or not home_html:
                result["icp"] = "网址打不开"
                print(f"      ❌ [独立站打不开] {website} (原因: {final_err})")
                return result

            # 1. 提取 ICP 备案号
            icp_match = ICP_REGEX.search(home_html)
            result["icp"] = re.sub(r'\s+', '', icp_match.group(1)) if icp_match else "无"

            # 2. 挖掘邮箱与官网联系电话
            raw_emails = self._extract_valid_emails(home_html)
            raw_phones = self._extract_valid_phones(home_html)

            # 3. 提取子页面：确保 contact 页面优先级高于 about 页面
            all_links = re.findall(r'href=["\']([^"\']*(?:contact|about)[^"\']*)["\']', home_html, re.I)
            unique_links = list(dict.fromkeys(all_links))
            unique_links.sort(key=lambda x: 0 if "contact" in x.lower() else 1)

            base_domain = urlparse(website).netloc.replace("www.", "")
            sub_pages_to_crawl = []

            for lk in unique_links:
                if lk.startswith("#") or lk.startswith("javascript:") or any(lk.lower().endswith(ext) for ext in IMAGE_EXTS):
                    continue
                full_url = urljoin(website, lk)
                target_domain = urlparse(full_url).netloc.replace("www.", "")

                if target_domain == base_domain and full_url not in sub_pages_to_crawl and full_url != website:
                    sub_pages_to_crawl.append(full_url)
                if len(sub_pages_to_crawl) >= 2:
                    break

            if sub_pages_to_crawl:
                tasks = [self._fetch_html_resilient(client, sub_u) for sub_u in sub_pages_to_crawl]
                sub_res_list = await asyncio.gather(*tasks)
                for code, sub_html, real_u in sub_res_list:
                    if code == 200 and sub_html:
                        raw_emails.extend(self._extract_valid_emails(sub_html))
                        raw_phones.extend(self._extract_valid_phones(sub_html))

            if raw_emails:
                pri = [e for e in raw_emails if any(k in e for k in ["sales", "info", "contact", "export"])]
                result["email"] = pri[0] if pri else raw_emails[0]
                print(f"      📧 [官网邮箱] 提取成功: {result['email']} ({website})")

            clean_phones = deduplicate_phone_list(raw_phones)
            if clean_phones:
                result["site_phone"] = " / ".join(clean_phones[:3])
                print(f"      📞 [官网联系方式] 提取成功: {result['site_phone']} ({website})")

            if result["icp"] != "无":
                print(f"      🛡️ [备案信息] 提取到备案号: {result['icp']} ({website})")

        return result


# ==========================================
# 天眼查自动化触点补全模块 (带反爬冷却与滑块感知)
# ==========================================
class TianyanchaEnricher:
    """复用本地 Chrome CDP 会话，带防风控拟人延时与滑块验证码人工接管"""

    async def _human_rest(self, min_sec: float = 3.0, max_sec: float = 5.0, desc: str = ""):
        rest_time = round(random.uniform(min_sec, max_sec), 2)
        if desc:
            print(f"      ⏱️ [天眼查安全冷却] {desc}，等待 {rest_time} 秒...")
        await asyncio.sleep(rest_time)

    async def _handle_captcha_if_needed(self, page):
        is_blocked = False
        current_url = page.url.lower()

        if "sec.tianyancha.com" in current_url or "verify" in current_url:
            is_blocked = True

        captcha_el = await page.query_selector('.sec-captcha, .geetest_holder, div[class*="captcha"], #nc_1_wrapper')
        if captcha_el:
            is_blocked = True

        if is_blocked:
            print("\n" + "!" * 60)
            print("🚨 [天眼查风控触发] 检测到滑块验证码 / 安全防护拦截！")
            print("👉 请切回已打开的 Chrome 浏览器窗口，【手动滑动完成验证】。")
            print("⏳ 流水线已自动暂停等待，验证通过后会自动恢复运行...")
            print("!" * 60 + "\n")

            for _ in range(120):
                await asyncio.sleep(2)
                cur_url = page.url.lower()
                captcha_now = await page.query_selector('.sec-captcha, .geetest_holder, div[class*="captcha"]')
                if "sec.tianyancha.com" not in cur_url and not captcha_now:
                    print("✅ [人工验证成功] 恢复自动化抓取！\n")
                    await self._human_rest(2.0, 3.5, desc="缓和停顿")
                    return

            print("⚠️ 人工验证等待超时，跳过该商户天眼查查询。")

    async def search_and_enrich(self, page, company_name: str) -> dict:
        info = {
            "phone": "",
            "email": "",
            "contact_person": "",
            "contact_title": "",
            "registered_company": "",
            "registered_address": ""
        }
        if not company_name or len(company_name.strip()) < 3:
            return info

        search_kw = company_name.strip()
        print(f"      🔎 [天眼查检索] 正在检索: {search_kw}")

        try:
            await page.goto(
                f"https://www.tianyancha.com/search?key={quote_plus(search_kw)}",
                wait_until="domcontentloaded",
                timeout=25000
            )
            await self._handle_captcha_if_needed(page)
            await page.mouse.wheel(0, random.randint(200, 450))
            await self._human_rest(1.5, 2.5, desc="搜索结果渲染观察")

            card_el = await page.query_selector('div[class*="search-item"], .search-block, div[class*="result-item"]')
            if not card_el:
                print(f"      ℹ️ [天眼查] 未找到匹配的企业卡片: {search_kw}")
                return info

            card_text = (await card_el.inner_text()).strip()

            title_el = await card_el.query_selector('a.name, .title, h2, a[href*="/company/"]')
            if title_el:
                c_name = (await title_el.inner_text()).strip()
                c_clean = re.sub(r'[\s_]+', '', c_name)
                if len(c_clean) >= 4:
                    info["registered_company"] = c_clean

            addr_m = re.search(r'(?:注册地址|地址)\s*[:：]?\s*([^\n\r]+)', card_text)
            if addr_m:
                a_clean = addr_m.group(1).strip()
                if "暂无" not in a_clean and "登录" not in a_clean and len(a_clean) >= 5:
                    info["registered_address"] = a_clean

            collected_phones = []
            for pm in re.findall(r'电话\s*[:：]?\s*([+\d\s\-]+)', card_text):
                if "暂无" not in pm and "登录" not in pm and len(re.sub(r'\D', '', pm)) >= 7:
                    collected_phones.append(pm.strip())

            em = re.search(r'邮箱\s*[:：]?\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', card_text)
            if em:
                info["email"] = em.group(1).strip()

            lm = re.search(r'法定代表人\s*[:：]?\s*([^\s\n\r]+)', card_text)
            if lm:
                p = lm.group(1).strip()
                if len(p) <= 8 and p not in ["-", "暂无", "登录"]:
                    info["contact_person"] = p
                    info["contact_title"] = "法定代表人"

            if not collected_phones or not info["registered_address"]:
                link_el = await card_el.query_selector('a[href*="/company/"]')
                if link_el:
                    href = await link_el.get_attribute("href")
                    if href:
                        await self._human_rest(1.0, 1.5, desc="点击进详情页")
                        await page.goto(urljoin("https://www.tianyancha.com", href), wait_until="domcontentloaded", timeout=20000)
                        await self._handle_captcha_if_needed(page)

                        detail_text = await page.evaluate("document.body.innerText")

                        if not collected_phones:
                            for dp in re.findall(r'(?:电话|联系方式)\s*[:：]?\s*([+\d\s\-]+)', detail_text):
                                if "暂无" not in dp and "登录" not in dp and len(re.sub(r'\D', '', dp)) >= 7:
                                    collected_phones.append(dp.strip())

                        if not info["registered_address"]:
                            d_addr = re.search(r'(?:注册地址|企业地址|地址)\s*[:：]?\s*([^\n\r<]+)', detail_text)
                            if d_addr:
                                a_clean = d_addr.group(1).strip()
                                if "暂无" not in a_clean and "登录" not in a_clean and len(a_clean) >= 5:
                                    info["registered_address"] = a_clean

            deduped = deduplicate_phone_list(collected_phones)
            if deduped:
                info["phone"] = " / ".join(deduped[:2])

            print(f"      ✅ [天眼查成功] 中文名: {info['registered_company'] or '未查到'} | 地址: {info['registered_address'] or '未公开'} | 电话: {info['phone'] or '未公开'}")

        except Exception as e:
            print(f"      ⚠️ [天眼查检索异常] {search_kw}: {e}")

        await self._human_rest(2.5, 4.0, desc="检索冷却")
        return info


# =====================================================================
# 通用流水线主执行函数
# =====================================================================
async def run_pipeline(
    keyword: str = "monitor",
    platform: str = "globalsources",
    max_count: int = 5,
    output_file: str = "suppliers_leads.xlsx",
    enrich_websites: bool = True,
    enrich_tianyancha: bool = True
):
    print(f"\n=======================================================")
    print(f"🚀 [Pipeline] 启动自动化多平台采集流水线")
    print(f"🌐 目标平台: {platform} | 关键词: {keyword} | 计划采集: {max_count}")
    print(f"=======================================================")

    # 1. 动态获取对应平台的爬虫并抓取
    crawler: BaseCrawler = CrawlerFactory.get_crawler(platform)
    leads_data: list[RawSupplierLead] = await crawler.scrape(keyword=keyword, max_count=max_count)

    if not leads_data:
        print("\n💡 未采集到任何有效商户，流程结束。")
        return

    # 2. 独立站穿透挖掘
    enriched_results = [
        {"site_phone": "", "tyc_phone": "", "email": "", "icp": "无", "contact_person": "", "contact_title": ""}
        for _ in leads_data
    ]

    if enrich_websites:
        print(f"\n🌐 [Enrichment 1/2] 异步穿透独立站探测商业邮箱、官网联系方式与备案...")
        enricher = WebsiteEnricher(concurrency=5)
        async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
            tasks = [enricher.enrich_lead(client, lead.official_website) for lead in leads_data]
            site_results = await asyncio.gather(*tasks)

        for i, s_res in enumerate(site_results):
            enriched_results[i]["email"] = s_res.get("email", "")
            enriched_results[i]["site_phone"] = s_res.get("site_phone", "")
            enriched_results[i]["icp"] = s_res.get("icp", "无")

    # 3. 大模型工商质检与全称规范
    print(f"\n🧠 [LLM 质检] 调用 DeepSeek 模型分析工商全称与工厂画像...")
    eval_results = []
    for idx, lead in enumerate(leads_data, 1):
        try:
            eval_res = await evaluate_supplier_icp(
                lead=lead,
                api_key=config.OPENAI_API_KEY,
                base_url=config.OPENAI_BASE_URL,
                model=config.MODEL_NAME
            )
            display_name = eval_res.clean_company_name or '境外/非标准企业'
            print(f"      ✨ [{idx}/{len(leads_data)}] 质检提纯: {lead.company} -> {display_name}")
        except Exception as e:
            print(f"      ⚠️ [{idx}/{len(leads_data)}] 质检跳过异常: {lead.company} ({e})")
            class FallbackEval:
                clean_company_name = lead.registered_company or lead.company
            eval_res = FallbackEval()

        eval_results.append(eval_res)

    # 4. 天眼查触点与地址兜底
    if enrich_tianyancha:
        needing_indices = []
        for i, (lead, enrich_res, eval_res) in enumerate(zip(leads_data, enriched_results, eval_results)):
            target = (eval_res.clean_company_name or lead.registered_company or lead.company or "").strip()
            clean_target = re.sub(r'[\.,;:\s"\'\)]+$', '', target).lower()
            is_overseas = any(clean_target.endswith(s) for s in ["gmbh", "llc", "s.r.l.", "pte. ltd.", "pte ltd", "inc", "corp"])
            has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', target))

            if (has_chinese or not is_overseas) and (not enrich_res.get("site_phone") or not lead.registered_address):
                needing_indices.append(i)

        if needing_indices:
            print(f"\n🏢 [Enrichment 2/2] 启动天眼查检索 (共 {len(needing_indices)} 家商户需补充天眼查联系方式/地址)...")
            crawler.ensure_chrome_running()
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{crawler.cdp_port}")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                tyc_page = await context.new_page()
                tyc_enricher = TianyanchaEnricher()

                try:
                    for idx in needing_indices:
                        lead = leads_data[idx]
                        eval_res = eval_results[idx]
                        search_target = eval_res.clean_company_name or lead.registered_company or lead.company

                        tyc_info = await tyc_enricher.search_and_enrich(tyc_page, search_target)

                        if tyc_info.get("phone"):
                            enriched_results[idx]["tyc_phone"] = tyc_info["phone"]
                        if not enriched_results[idx].get("email") and tyc_info.get("email"):
                            enriched_results[idx]["email"] = tyc_info["email"]
                        if tyc_info.get("contact_person"):
                            enriched_results[idx]["contact_person"] = tyc_info["contact_person"]
                            enriched_results[idx]["contact_title"] = tyc_info.get("contact_title", "法定代表人")
                        if tyc_info.get("registered_company"):
                            enriched_results[idx]["registered_company"] = tyc_info["registered_company"]
                        if not lead.registered_address and tyc_info.get("registered_address"):
                            lead.registered_address = tyc_info["registered_address"]

                        await asyncio.sleep(1.0)
                finally:
                    await tyc_page.close()
        else:
            print(f"\n🏢 [Enrichment 2/2] 所有符合条件的商户触点均已齐全，跳过天眼查检索。")

    # 5. 组装行数据并导出 Excel 报表
    print(f"\n📊 [数据整理与导出] 正在组织字段写入 Excel 报表...")
    new_rows = []
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for lead, enrich_res, eval_res in zip(leads_data, enriched_results, eval_results):
        official_site = lead.official_website or ""
        icp_status = enrich_res.get("icp", "无") if official_site else "无"

        company_chinese = (
            enrich_res.get("registered_company")
            or eval_res.clean_company_name
            or lead.registered_company
            or lead.company
        )
        company_address = lead.registered_address or enrich_res.get("registered_address") or ""

        new_rows.append({
            "平台名称": lead.platform,
            "公司英文名": lead.company,
            "平台网址": lead.store_url,
            "公司中文名": company_chinese,
            "公司注册地址": company_address,
            "天眼查联系人": enrich_res.get("contact_person", ""),
            "天眼查联系人职位": enrich_res.get("contact_title", ""),
            "独立站": official_site,
            "网址是否有备案": icp_status,
            "官网联系方式": enrich_res.get("site_phone", ""),
            "天眼查联系方式": enrich_res.get("tyc_phone", ""),
            "搜索的关键词": keyword,
            "email": enrich_res.get("email", ""),
            "入库时间": current_time
        })

    new_df = pd.DataFrame(new_rows)[TARGET_COLUMNS]

    if os.path.exists(output_file):
        try:
            old_df = pd.read_excel(output_file, dtype=str)
            if "联系方式" in old_df.columns and "官网联系方式" not in old_df.columns:
                old_df.rename(columns={"联系方式": "官网联系方式"}, inplace=True)
            for col in TARGET_COLUMNS:
                if col not in old_df.columns:
                    old_df[col] = ""

            old_df = old_df[TARGET_COLUMNS]
            merged_df = pd.concat([old_df, new_df], ignore_index=True)
            merged_df.drop_duplicates(subset=["平台网址"], keep="last", inplace=True)
            final_df = merged_df[TARGET_COLUMNS]
        except Exception:
            final_df = new_df
    else:
        final_df = new_df

    target_path = output_file
    try:
        if os.path.exists(target_path):
            with open(target_path, "a+"):
                pass
    except PermissionError:
        target_path = f"suppliers_leads_{int(datetime.now().timestamp())}.xlsx"
        print(f"⚠️ [占用警告] 原文件被占用，已重定向写入至: {target_path}")

    with pd.ExcelWriter(target_path, engine="openpyxl") as writer:
        final_df.to_excel(writer, index=False, sheet_name="Suppliers")
        ws = writer.sheets["Suppliers"]
        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        for cell in ws[1]:
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

        text_cols = [
            "官网联系方式", "天眼查联系方式", "email", "入库时间",
            "公司注册地址", "平台网址", "独立站", "网址是否有备案"
        ]
        for col_idx, col_name in enumerate(final_df.columns, start=1):
            is_text = col_name in text_cols
            for row_idx in range(2, len(final_df) + 2):
                cell = ws.cell(row=row_idx, column=col_idx)
                if is_text:
                    cell.number_format = "@"
                    cell.data_type = "s"
                    if cell.value is not None:
                        cell.value = str(cell.value)

    print(f"\n🎉 [流水线完成] 数据已入库: {target_path}")
    print(f"📈 累计总商户: {len(final_df)} 条 (本次追加: {len(new_df)} 条)")


if __name__ == "__main__":
    asyncio.run(run_pipeline(keyword="monitor", max_count=3, enrich_websites=True, enrich_tianyancha=True))