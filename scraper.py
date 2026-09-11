import asyncio
import html as html_lib
from html.parser import HTMLParser
import os
import random
import re
import shutil
import socket
import subprocess
import time
from urllib.parse import quote_plus, urljoin
import httpx
from playwright.async_api import async_playwright
from dedup import dedup

HTTPX_CONCURRENCY = 4


def is_port_open(host: str = "127.0.0.1", port: int = 9222) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def ensure_chrome_running(port: int = 9222, profile_dir: str = "./chrome_debug_profile"):
    if is_port_open("127.0.0.1", port):
        print(f"✅ 检测到 Chrome CDP 端口 {port} 已就绪，直接接入...")
        return

    print(f"🚀 未检测到运行中的调试浏览器，正在自动拉起 Chrome (端口: {port})...")

    possible_paths = [
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]

    chrome_path = next((p for p in possible_paths if p and os.path.exists(p)), None)
    if not chrome_path:
        raise FileNotFoundError("未在系统路径下找到 Chrome 可执行文件，请确认是否已安装 Chrome。")

    os.makedirs(profile_dir, exist_ok=True)
    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={os.path.abspath(profile_dir)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu-shader-disk-cache",
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(15):
        if is_port_open("127.0.0.1", port):
            print("✅ 本地 Chrome 已成功启动并开放调试端口！")
            time.sleep(1)
            return
        time.sleep(1)

    raise TimeoutError(f"等待 Chrome 启动超时，未能成功绑定 {port} 端口。")


async def human_delay(min_sec: float = 1.5, max_sec: float = 3.0, desc: str = ""):
    sleep_time = round(random.uniform(min_sec, max_sec), 2)
    if desc:
        print(f"      ⏱️ [{desc}] 模拟停顿 {sleep_time} 秒...")
    await asyncio.sleep(sleep_time)


def is_valid_company_name(name: str) -> bool:
    r"""
    企业名称校验：
    1. 剥离两端标点与空格，抹平大小写差异 (ltd / Ltd / LTD)
    2. 使用 (?:\.|\b) 解决结尾句点导致 \b 边界失效的问题
    3. 支持多国法定企业后缀（Pte. Ltd., LLC, GmbH, S.R.L., Sdn Bhd 等）
    """
    if not name or not isinstance(name, str):
        return False

    clean_name = re.sub(r'^[,\.;:\s"\'\(]+|[,\.;:\s"\'\)]+$', '', name.strip())
    if len(clean_name) < 4 or len(clean_name) > 100:
        return False

    name_lower = clean_name.lower()

    company_pattern = (
        r'(\bco\.?,?\s*ltd(?:\.|\b)|'  # Co., Ltd / Co. Ltd / Co Ltd
        r'\bpte\.?\s*ltd(?:\.|\b)|'  # Pte Ltd / Pte. Ltd.
        r'\bltd(?:\.|\b)|'  # Ltd / Ltd.
        r'\blimited\b|'  # Limited
        r'\bllc(?:\.|\b)|'  # LLC
        r'\binc(?:\.|\b)|'  # Inc / Inc.
        r'\bcorp(?:\.|\b)|'  # Corp / Corp.
        r'\bcorporation\b|'  # Corporation
        r'\bgmbh(?:\.|\b)|'  # GmbH
        r'\bs\.?r\.?l(?:\.|\b)|'  # S.R.L. / SRL
        r'\bs\.?a(?:\.|\b)|'  # S.A.
        r'\bsdn\.?\s*bhd(?:\.|\b)|'  # Sdn Bhd
        r'\bcompany\b|\bgroup\b|\bfactory\b|'  # Company / Group / Factory
        r'\btechnology\b|\btechnologies\b|'  # Technology / Technologies
        r'\benterprise\b|\bindustrial\b)'  # Enterprise / Industrial
    )

    if bool(re.search(company_pattern, name_lower)):
        return True

    pure_product_keywords = [
        "gaming monitor", "wholesale", "factory price",
        "moq", "pieces", "frameless", "hot sale", "ready to ship"
    ]
    if any(pk in name_lower for pk in pure_product_keywords):
        return False

    return False


def format_clean_url(href: str) -> str:
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


def clean_token(val: str) -> str:
    if not val:
        return ""
    val = re.sub(r'[\r\n\t]+', ' ', val).strip()
    bad_tokens = [
        "send inquiry", "inquiry now", "chat now", "inquire",
        "contact supplier", "verified", "view more", "view less", "exchange"
    ]
    for b in bad_tokens:
        if b in val.lower():
            return ""
    return val


def normalize_website(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    url = re.sub(r'[,;:\s<>"\'\)]+$', '', url)
    if "globalsources.com" in url.lower():
        return ""
    if any(url.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.css', '.js', '.svg']):
        return ""
    clean_domain = re.sub(r'^https?://', '', url).split('/')[0]
    if '.' not in clean_domain or len(clean_domain) < 4:
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url}"
    return url


def html_to_clean_text(html_content: str) -> str:
    if not html_content:
        return ""
    clean = re.sub(r'<(script|style|head|noscript|svg)[^>]*>.*?</\1>', '', html_content,
                   flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<(?:br|p|div|tr|li|h[1-6]|section|article)[^>]*>', '\n', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    clean = html_lib.unescape(clean)
    lines = [re.sub(r'[ \t\xa0\u3000]+', ' ', line).strip() for line in clean.splitlines()]
    return '\n'.join([line for line in lines if line])


class FastSupplierExtractor:
    """提取店铺工商登记与独立官网入口"""

    def __init__(self, cookies: dict, headers: dict, concurrency: int = HTTPX_CONCURRENCY):
        self.cookies = cookies
        self.headers = headers
        self.semaphore = asyncio.Semaphore(concurrency)

    async def fetch_html(self, client: httpx.AsyncClient, url: str, retries: int = 2) -> str:
        if not url:
            return ""
        for attempt in range(retries + 1):
            try:
                resp = await client.get(url, timeout=15.0)
                if resp.status_code == 200:
                    return resp.text
                elif resp.status_code in [429, 503]:
                    await asyncio.sleep(1.0 * (attempt + 1))
            except Exception:
                if attempt == retries:
                    return ""
                await asyncio.sleep(0.8)
        return ""

    def resolve_target_urls(self, store_url: str) -> tuple[str, str]:
        if re.search(r'/(?:homepage|contact-us|company-profile|showroom)_(\d+)\.htm', store_url, re.I):
            profile_url = re.sub(r'/(?:homepage|contact-us|company-profile|showroom)_', '/company-profile_', store_url,
                                 flags=re.I)
            contact_url = re.sub(r'/(?:homepage|contact-us|company-profile|showroom)_', '/contact-us_', store_url,
                                 flags=re.I)
            return profile_url, contact_url

        si_match = re.search(r'/si/(\d+)', store_url, re.I)
        if si_match:
            supplier_id = si_match.group(1)
            base_site = store_url.split('/si/')[0]
            profile_url = f"{base_site}/company-profile_{supplier_id}.htm"
            contact_url = f"{base_site}/contact-us_{supplier_id}.htm"
            return profile_url, contact_url

        clean_base = store_url.rstrip('/')
        return f"{clean_base}/company-profile", f"{clean_base}/contact-us"

    async def parse_detail(self, client: httpx.AsyncClient, store_url: str) -> dict:
        info = {
            "registered_company": "",
            "registered_address": "",
            "official_website": ""
        }
        if not store_url or not store_url.startswith("http"):
            return info

        profile_url, contact_url = self.resolve_target_urls(store_url)

        async with self.semaphore:
            profile_html, contact_html = await asyncio.gather(
                self.fetch_html(client, profile_url),
                self.fetch_html(client, contact_url)
            )

            # 动态嗅探导航栏
            if not profile_html or not contact_html:
                home_html = await self.fetch_html(client, store_url)
                if home_html:
                    if not profile_html:
                        p_links = re.findall(r'href=["\']([^"\']*(?:company-profile|about-us)[^"\']*)["\']', home_html,
                                             re.I)
                        if p_links:
                            real_p_url = format_clean_url(urljoin(store_url, p_links[0]))
                            profile_html = await self.fetch_html(client, real_p_url)

                    if not contact_html:
                        c_links = re.findall(r'href=["\']([^"\']*(?:contact-us|contact)[^"\']*)["\']', home_html, re.I)
                        if c_links:
                            real_c_url = format_clean_url(urljoin(store_url, c_links[0]))
                            contact_html = await self.fetch_html(client, real_c_url)

        # 1. 工商全称与注册地址
        if profile_html:
            profile_text = html_to_clean_text(profile_html)
            comp_match = re.search(
                r'Registered\s*Company\s*[:：]?\s*([\s\S]*?)(?:Registration\s*Number|Company\s*Registration Address)',
                profile_text, re.I
            )
            if comp_match:
                lines = [s.strip() for s in comp_match.group(1).split('\n') if s.strip()]
                if lines:
                    info["registered_company"] = clean_token(lines[0])

            addr_match = re.search(
                r'(?:Company\s*Registration\s*Address|Registered\s*Address)\s*[:：]?\s*([\s\S]*?)(?:\*\s*In\s*China|View\s*Less|Production\s*Capacity|\n\n)',
                profile_text, re.I
            )
            if addr_match:
                lines = [s.strip() for s in addr_match.group(1).split('\n') if s.strip()]
                if lines:
                    info["registered_address"] = clean_token(lines[0])

        # 2. 仅提取企业外部独立站网址
        if contact_html:
            contact_text = html_to_clean_text(contact_html)

            other_web_m = re.search(
                r'Other\s+(?:homepage\s+)?website\s*[:：]?\s*([^\s\r\n<"\'>]+)',
                contact_text, re.I
            )
            if other_web_m:
                cand = normalize_website(other_web_m.group(1))
                if cand:
                    info["official_website"] = cand

            if not info["official_website"]:
                html_web_m = re.search(
                    r'Other\s+(?:homepage\s+)?website[\s\S]*?(?:href=["\']([^"\']+)["\']|>([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}[^<\s]*))',
                    contact_html, re.I
                )
                if html_web_m:
                    raw_url = html_web_m.group(1) or html_web_m.group(2)
                    cand = normalize_website(raw_url)
                    if cand:
                        info["official_website"] = cand

            # 兜底匹配普通 website
            if not info["official_website"]:
                gen_web_m = re.search(
                    r'(?:website|homepage)\s*[:：]?\s*([^\s\r\n<"\'>]+)',
                    contact_text, re.I
                )
                if gen_web_m:
                    cand = normalize_website(gen_web_m.group(1))
                    if cand:
                        info["official_website"] = cand

        return info


async def scrape_globalsources_suppliers(keyword: str = "led", max_count: int = 5) -> list[dict]:
    ensure_chrome_running(port=9222)
    clean_kw = keyword.lower().replace("manufacturer", "").strip()

    candidate_sellers = []
    seen_companies = set()

    async with async_playwright() as p:
        print("🔌 正在连接本地 Chrome (CDP 端口: 9222)...")
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        async def block_resources(route):
            if route.request.resource_type in ["image", "media", "font"]:
                await route.abort()
            elif any(b in route.request.url.lower() for b in ["google-analytics", "doubleclick", "sensorsdata"]):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_resources)

        page_num = 1
        max_search_pages = 25

        while len(candidate_sellers) < max_count and page_num <= max_search_pages:
            search_url = f"https://www.globalsources.com/searchList/suppliers?keyWord={quote_plus(clean_kw)}&pageNum={page_num}"
            print(f"\n📑 [浏览器探路] 检索搜索列表第 {page_num} 页: {search_url}")

            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
                await human_delay(2.0, 3.0, desc=f"第 {page_num} 页就绪")
            except Exception as e:
                print(f"⚠️ 第 {page_num} 页加载超时: {e}")
                break

            for _ in range(3):
                await page.mouse.wheel(0, random.randint(650, 950))
                await human_delay(0.5, 1.0)

            candidate_elements = await page.query_selector_all(
                'a[href*="manufacturer.globalsources.com/homepage_"], a[href*="/si/"], a.company-name, a.supplier-name'
            )
            if not candidate_elements:
                print(f"🏁 第 {page_num} 页未检测到更多供应商卡片，列表见底！")
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
                comp_name = title_attr if is_valid_company_name(title_attr) else text
                clean_url = format_clean_url(href).rstrip('/')

                if dedup.is_seen(comp_name):
                    print(f"      ⏭️ [指纹库命中] 跳过已采店铺: {comp_name}")
                    continue

                if is_valid_company_name(comp_name) and comp_name not in seen_companies and href:
                    seen_companies.add(comp_name)
                    candidate_sellers.append({
                        "company": comp_name,
                        "store_url": clean_url
                    })
                    page_added += 1

            print(f"✅ 第 {page_num} 页提取到 {page_added} 家新供应商（累计候选: {len(candidate_sellers)}/{max_count}）")

            if len(candidate_sellers) < max_count:
                page_num += 1
                await human_delay(1.5, 2.5, desc="列表翻页冷却")

        raw_cookies = await context.cookies()
        session_cookies = {c['name']: c['value'] for c in raw_cookies}
        user_agent = await page.evaluate("navigator.userAgent")

        try:
            await page.unroute("**/*")
        except Exception:
            pass

    if not candidate_sellers:
        return []

    print(f"\n🚀 开始通过 HTTPX 异步并发提取 {len(candidate_sellers)} 家商户工商与独立站...")

    custom_headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
    }

    extractor = FastSupplierExtractor(cookies=session_cookies, headers=custom_headers)

    async with httpx.AsyncClient(
            cookies=session_cookies,
            headers=custom_headers,
            follow_redirects=True,
            timeout=15.0
    ) as client:

        async def process_single_seller(idx: int, seller: dict):
            comp_name = seller["company"]
            store_url = seller["store_url"]
            print(f"   ⚡ [{idx}/{len(candidate_sellers)}] 并发拉取工商与独立官网: {comp_name}")

            detail_info = await extractor.parse_detail(client, store_url)

            dedup.add(comp_name)
            if detail_info.get("registered_company"):
                dedup.add(detail_info["registered_company"])

            return {
                "company": comp_name,
                "platform": "Global Sources",
                "store_url": store_url,
                "registered_company": detail_info["registered_company"],
                "registered_address": detail_info["registered_address"],
                "official_website": detail_info["official_website"],
                "card_product": clean_kw
            }

        tasks = [process_single_seller(i, s) for i, s in enumerate(candidate_sellers, 1)]
        final_results = await asyncio.gather(*tasks)

    print(f"\n🎉 平台初采完成，获得 {len(final_results)} 条商户工商与独立官网。")
    return final_results