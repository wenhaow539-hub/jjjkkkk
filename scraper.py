import asyncio
import html as html_lib
import os
import random
import re
import socket
import subprocess
import time
from urllib.parse import quote_plus
import httpx
from playwright.async_api import async_playwright
from dedup import dedup

# ==========================================
# 核心功能配置与防封控阈值
# ==========================================
AUTO_UNLOCK_PHONE = False      # 是否自动点击 View More 和 Exchange 解锁联系方式
MIN_CONTACT_DELAY = 60        # 获取下一个联系方式的最短等待时间（秒）
MAX_CONTACT_DELAY = 120       # 获取下一个联系方式的最长等待时间（秒）


def is_port_open(host: str = "127.0.0.1", port: int = 9222) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def ensure_chrome_running(port: int = 9222, profile_dir: str = r"C:\chrome_debug_profile"):
    if is_port_open("127.0.0.1", port):
        print(f"✅ 检测到 Chrome CDP 端口 {port} 已就绪，直接接入当前已登录会话...")
        return

    print(f"🚀 未检测到运行中的调试浏览器，正在自动拉起本地 Chrome (端口: {port})...")
    possible_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
    ]

    chrome_path = next((p for p in possible_paths if os.path.exists(p)), None)
    if not chrome_path:
        raise FileNotFoundError("未在常见安装路径下找到 chrome.exe，请检查本地 Chrome 是否安装。")

    os.makedirs(profile_dir, exist_ok=True)
    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disk-cache-size=20971520",
        "--media-cache-size=1048576",
        "--disable-gpu-shader-disk-cache",
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(15):
        if is_port_open("127.0.0.1", port):
            print("✅ 本地 Chrome 已成功启动并开放调试端口！")
            time.sleep(1)
            return
        time.sleep(1)

    raise TimeoutError("等待 Chrome 启动超时，未能成功绑定 9222 端口。")


async def human_delay(min_sec: float = 2.0, max_sec: float = 3.5, desc: str = ""):
    sleep_time = round(random.uniform(min_sec, max_sec), 2)
    if desc:
        print(f"      ⏱️ [{desc}] 模拟停顿 {sleep_time} 秒...")
    await asyncio.sleep(sleep_time)


async def account_safe_countdown(min_sec: int = MIN_CONTACT_DELAY, max_sec: int = MAX_CONTACT_DELAY):
    """防风控长效动态倒计时（60-120秒，每10秒打印进度）"""
    wait_time = random.randint(min_sec, max_sec)
    print(f"\n🛡️ [账号风控保护] 正在进入商户间隔深度冷却: 随机休眠 {wait_time} 秒...")

    remaining = wait_time
    while remaining > 0:
        step = min(10, remaining)
        await asyncio.sleep(step)
        remaining -= step
        if remaining > 0:
            print(f"   ⏳ [休眠中] 距离处理下一家商户还剩: {remaining} 秒...")

    print("   ✅ 冷却完毕，恢复下一家商户联系方式采集。\n")


async def human_click(page, locator):
    """拟人化点击：多帧平滑移动、非中心散列坐标、物理微按压"""
    if not locator:
        return
    try:
        await locator.scroll_into_view_if_needed()
        await asyncio.sleep(random.uniform(0.2, 0.4))

        box = await locator.bounding_box()
        if box:
            target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

            await page.mouse.move(target_x, target_y, steps=random.randint(8, 15))
            await asyncio.sleep(random.uniform(0.2, 0.4))

            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.08, 0.15))
            await page.mouse.up()
        else:
            await locator.click(timeout=3000)
    except Exception:
        try:
            await locator.click(timeout=3000)
        except Exception:
            pass


async def safe_navigate(page, url: str, timeout: int = 35000, max_retries: int = 3) -> bool:
    for attempt in range(1, max_retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            await human_delay(1.5, 2.5)

            body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            if "Too much connections" in body_text:
                cooldown = random.randint(30, 50)
                print(f"      ⏳ [触发服务端限流] 命中频控上限，强制冷却 {cooldown} 秒 (尝试 {attempt}/{max_retries})...")
                await asyncio.sleep(cooldown)
                continue

            return True
        except Exception as e:
            print(f"      ⚠️ 页面加载受阻 (尝试 {attempt}/{max_retries}): {url.split('/')[-1]} ({e})")
            await human_delay(3.0, 5.0)

    return False


# ==========================================
# 数据清洗与规范化工具
# ==========================================
def is_valid_company_name(name: str) -> bool:
    name = name.strip()
    if len(name) < 6 or len(name) > 80:
        return False
    name_lower = name.lower()
    product_keywords = [
        "monitor", "inch", "gaming", "rgb", "panel", "display", "screen",
        "charger", "wireless", "cable", "battery", "adapter", "usb",
        "lamp", "bulb", "strip", "fixture", "watt", "lumen", "oem", "odm",
        "wholesale", "factory price", "moq", "pieces", "frameless"
    ]
    for pk in product_keywords:
        if re.search(rf'\b{pk}\b', name_lower):
            return False
    company_pattern = r'\b(co\.?,?\s*ltd|limited|corp\b|corporation|inc\b|incorporated|company|group)\b'
    return bool(re.search(company_pattern, name_lower))


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


def format_contact_person(val: str) -> str:
    if not val:
        return ""
    val = clean_token(val)
    val = re.sub(r'[\s\u00a0\u3000\r\n\t]+', ' ', val).strip()
    val = re.sub(r'\b(Mr|Mrs|Ms|Miss|Dr)\s*\.\s*', r'\1. ', val, flags=re.I)
    val = re.sub(r'\b(Mr|Mrs|Ms|Miss|Dr)\s+(?!\.)', r'\1. ', val, flags=re.I)
    return re.sub(r'\s+', ' ', val).strip()


def normalize_website(url: str) -> str:
    if not url:
        return ""
    url = str(url).strip()
    url = re.sub(r'^(?:other\s+)?(?:homepage\s+)?website\s*[:：]?\s*', '', url, flags=re.I).strip()
    url = re.sub(r'[,;:\s<>"\'\)]+$', '', url)

    m = re.search(r'(https?://[^\s<>"\'()]+|www\.[^\s<>"\'()]+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s<>"\'()]*)?)', url)
    if m:
        url = m.group(1)

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


def normalize_phone_digits(p: str) -> str:
    main_num = p.split("ext")[0] if "ext" in p else p
    return re.sub(r'\D', '', main_num)


def is_same_phone(p1: str, p2: str) -> bool:
    if not p1 or not p2:
        return False
    if p1.strip() == p2.strip():
        return True
    d1 = normalize_phone_digits(p1)
    d2 = normalize_phone_digits(p2)
    if not d1 or not d2:
        return False
    if d1 == d2:
        return True
    if len(d1) >= 7 and len(d2) >= 7:
        if d1.endswith(d2) or d2.endswith(d1):
            return True
    return False


def merge_and_dedup_phone_list(raw_list: list) -> list[str]:
    unique_list = []
    for item in raw_list:
        p_clean = str(item or "").strip()
        if not p_clean or p_clean.lower() in ["view more", "exchange", "null", "undefined"]:
            continue
        exists = False
        for idx, u in enumerate(unique_list):
            if is_same_phone(p_clean, u):
                if len(p_clean) > len(u):
                    unique_list[idx] = p_clean
                exists = True
                break
        if not exists:
            unique_list.append(p_clean)
    return unique_list


def parse_only_tel_and_mobile_json(data: dict) -> tuple[list[str], list[str]]:
    if not data or not isinstance(data, dict):
        return [], []

    tels = []
    mobiles = []

    for item in data.get("telephoneList") or []:
        cc = str(item.get("countryCode", "") or "").strip()
        ac = str(item.get("areaCode", "") or "").strip()
        num = str(item.get("telNum", "") or "").strip()
        ext = str(item.get("extension", "") or "").strip()
        prefix = f"+{cc}" if cc else ""
        main_num = "-".join(filter(None, [prefix, ac, num]))
        if ext:
            main_num += f" ext {ext}"
        if main_num and len(re.sub(r'\D', '', main_num)) >= 6:
            tels.append(main_num)

    raw_mobiles = []
    if "mobileList" in data and isinstance(data["mobileList"], list):
        raw_mobiles.extend(data["mobileList"])
    if data.get("mobile"):
        raw_mobiles.append(data["mobile"])

    for m in raw_mobiles:
        if isinstance(m, dict):
            m_cc = str(m.get("countryCode", "") or "").strip()
            m_ac = str(m.get("areaCode", "") or "").strip()
            m_num = str(m.get("telNum", "") or "").strip()
            prefix = f"+{m_cc}" if m_cc else ""
            full_m = "-".join(filter(None, [prefix, m_ac, m_num]))
            if full_m and len(re.sub(r'\D', '', full_m)) >= 6:
                mobiles.append(full_m)
        elif isinstance(m, str) and m.strip() and len(re.sub(r'\D', '', m)) >= 6:
            mobiles.append(m.strip())

    return tels, mobiles


def html_to_clean_text(html_content: str) -> str:
    if not html_content:
        return ""
    clean = re.sub(r'<(script|style|head|noscript|svg)[^>]*>.*?</\1>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<(?:br|p|div|tr|li|h[1-6]|section|article)[^>]*>', '\n', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    clean = html_lib.unescape(clean)
    lines = [re.sub(r'[ \t\xa0\u3000]+', ' ', line).strip() for line in clean.splitlines()]
    return '\n'.join([line for line in lines if line])


# ==========================================
# 自动化解锁与详情抓取核心
# ==========================================
async def unlock_and_scrape_contact(page, contact_url: str) -> dict:
    info = {
        "contact_person": "",
        "contact_title": "",
        "official_website": "",
        "phone": "",
        "fallback_address": ""
    }
    if not contact_url:
        return info

    api_tels = []
    api_mobiles = []

    async def capture_contact_api(response):
        url_lower = response.url.lower()
        if any(k in url_lower for k in ["contact-number", "contactnumber", "getcontact", "exchange"]) and response.status == 200:
            try:
                res_json = await response.json()
                data = res_json.get("data") or res_json
                t, m = parse_only_tel_and_mobile_json(data)
                if t:
                    api_tels.extend(t)
                if m:
                    api_mobiles.extend(m)
                if t or m:
                    print(f"      ⚡ [接口嗅探成功] 截获数据: Telephone={t} | Mobile={m}")
            except Exception:
                pass

    page.on("response", capture_contact_api)

    try:
        nav_ok = await safe_navigate(page, contact_url)
        if not nav_ok:
            print(f"      ⚠️ Contact 页面无法正常加载，跳过交互。")
            return info

        try:
            await page.wait_for_selector(
                '.contact-details, .contact-item, div:has-text("Telephone"), div:has-text("Contact Details")',
                timeout=12000,
                state="attached"
            )
        except Exception:
            pass

        await page.mouse.wheel(0, 350)
        await human_delay(1.2, 2.0, desc="联系人页面渲染激活")

        if AUTO_UNLOCK_PHONE:
            view_more_loc = page.locator(
                'button:text-is("View More"), '
                'div:text-is("View More"), '
                'span:text-is("View More"), '
                'a:text-is("View More"), '
                '[class*="view-more" i]:not(:has([class*="view-more" i])), '
                '[class*="viewMore" i]:not(:has([class*="viewMore" i]))'
            ).first

            is_view_more_found = False
            try:
                await view_more_loc.wait_for(state="visible", timeout=6000)
                is_view_more_found = True
            except Exception:
                fallback_loc = page.locator('text=/^\\s*View More\\s*$/i').first
                if await fallback_loc.is_visible():
                    view_more_loc = fallback_loc
                    is_view_more_found = True

            if is_view_more_found:
                print("      🖱️ 检测到【View More】按钮，正在模拟拟人移动与点击...")
                await human_click(page, view_more_loc)
                await human_delay(1.2, 2.0, desc="等待名片交换弹窗")

                exchange_loc = page.locator(
                    'button:text-is("Exchange"), '
                    'div[role="dialog"] button:text-is("Exchange"), '
                    '.el-dialog button:text-is("Exchange"), '
                    '.modal button:text-is("Exchange")'
                ).first

                is_exchange_found = False
                try:
                    await exchange_loc.wait_for(state="visible", timeout=4500)
                    is_exchange_found = True
                except Exception:
                    modal_exchange = page.locator('[role="dialog"] button, .el-dialog button').filter(
                        has_text=re.compile(r"^\s*exchange\s*$", re.I)
                    ).first
                    if await modal_exchange.is_visible():
                        exchange_loc = modal_exchange
                        is_exchange_found = True

                if is_exchange_found:
                    print("      🪪 检测到【Exchange 名片交换】弹窗，正在模拟拟人点击互换...")
                    await human_click(page, exchange_loc)
                    await human_delay(2.5, 4.0, desc="等待解密完成与号码渲染")
                else:
                    print("      ⚡ 未出现名片弹窗或已自动免密解密")
            else:
                print("      ⚡ 未发现 View More 按钮（联系方式已公开或先前已互换名片）")

        # DOM 提取
        raw_dom = await page.evaluate("""
            () => {
                const nameEl = document.querySelector('.contact-name');
                const workerEl = document.querySelector('.contact-worker');
                const person = nameEl ? (nameEl.innerText || "") : "";
                const title = workerEl ? (workerEl.innerText || "") : "";

                const rows = Array.from(document.querySelectorAll('.contact-item, tr, li, div[class*="contact" i]')).map(el => {
                    const labelEl = el.querySelector('.contact-label, th, td:first-child');
                    const valEl = el.querySelector('.contact-value, td:last-child');
                    const aTag = el.querySelector('a');
                    return {
                        label: labelEl ? labelEl.innerText.trim() : "",
                        value: valEl ? valEl.innerText.trim() : "",
                        linkHref: aTag ? aTag.href : "",
                        fullText: el.innerText.trim()
                    };
                });

                return {
                    person,
                    title,
                    rows,
                    bodyText: document.body ? document.body.innerText : "",
                    bodyHtml: document.body ? document.body.innerHTML : ""
                };
            }
        """)

        info["contact_person"] = format_contact_person(raw_dom.get("person", ""))
        info["contact_title"] = clean_token(raw_dom.get("title", ""))

        body_text = raw_dom.get("bodyText", "")
        body_html = raw_dom.get("bodyHtml", "")
        rows = raw_dom.get("rows", [])

        extracted_web = ""
        for r in rows:
            combined = f"{r.get('label', '')} {r.get('fullText', '')}"
            if re.search(r'other\s+(?:homepage\s+)?website', combined, re.I):
                if r.get("linkHref") and "globalsources.com" not in r["linkHref"]:
                    extracted_web = r["linkHref"]
                    break
                web_candidate = r.get("value") or combined
                extracted_web = normalize_website(web_candidate)
                if extracted_web:
                    break

        if not extracted_web:
            m_text = re.search(r'Other\s+(?:homepage\s+)?website\s*[:：]?\s*(\S+)', body_text, re.I)
            if m_text:
                extracted_web = normalize_website(m_text.group(1))

        if not extracted_web:
            m_html = re.search(r'Other\s+(?:homepage\s+)?website[\s\S]{0,300}?href=["\']([^"\']+)["\']', body_html, re.I)
            if m_html:
                extracted_web = normalize_website(m_html.group(1))

        info["official_website"] = extracted_web

        dom_tels = []
        dom_mobiles = []
        for r in rows:
            label = r.get("label", "").lower()
            val = r.get("value") or r.get("fullText", "")
            val_clean = re.sub(r'view\s*more|exchange', '', val, flags=re.I).strip()

            if "telephone" in label and "fax" not in label:
                m = re.search(r'[+\d\s\-()ext]{6,30}', val_clean)
                if m and len(re.sub(r'\D', '', m.group(0))) >= 6:
                    dom_tels.append(m.group(0).strip())

            if "mobile" in label and "fax" not in label:
                m = re.search(r'[+\d\s\-()ext]{6,30}', val_clean)
                if m and len(re.sub(r'\D', '', m.group(0))) >= 6:
                    dom_mobiles.append(m.group(0).strip())

        m_addr = re.search(r'Address\s*[:：]?\s*([^\n\r]+)', body_text, re.I)
        if m_addr:
            info["fallback_address"] = clean_token(m_addr.group(1))

        final_tels = merge_and_dedup_phone_list(api_tels + dom_tels)
        final_mobiles = merge_and_dedup_phone_list(api_mobiles + dom_mobiles)

        phone_parts = []
        if final_tels:
            phone_parts.append(f"Tel: {' / '.join(final_tels)}")
        if final_mobiles:
            phone_parts.append(f"Mobile: {' / '.join(final_mobiles)}")

        info["phone"] = " | ".join(phone_parts)

        if info["contact_person"]:
            print(f"      👤 [提取成功] 紧凑联系人: {info['contact_person']} | 职位: {info['contact_title'] or '未注明'}")
        if info["official_website"]:
            print(f"      🌐 [提取成功] 企业独立官网: {info['official_website']}")
        if info["phone"]:
            print(f"      📞 [电话已提取（仅座机/手机）]: {info['phone']}")

    except Exception as e:
        print(f"      ⚠️ 联系人页自动化异常: {e}")
    finally:
        page.remove_listener("response", capture_contact_api)

    return info


async def scrape_company_profile_fast(client: httpx.AsyncClient, profile_url: str) -> dict:
    res = {"registered_company": "", "registered_address": "", "official_website": ""}
    if not profile_url:
        return res
    try:
        resp = await client.get(profile_url, timeout=10.0)
        if resp.status_code == 200:
            text = html_to_clean_text(resp.text)
            if "Too much connections" in text:
                return res

            comp_m = re.search(r'Registered\s*Company\s*[:：]?\s*([\s\S]*?)(?:Registration\s*Number|Company\s*Registration Address)', text, re.I)
            if comp_m:
                lines = [s.strip() for s in comp_m.group(1).split('\n') if s.strip()]
                if lines:
                    res["registered_company"] = clean_token(lines[0])

            addr_m = re.search(r'(?:Company\s*Registration\s*Address|Registered\s*Address)\s*[:：]?\s*([\s\S]*?)(?:\*\s*In\s*China|View\s*Less|Production\s*Capacity|\n\n)', text, re.I)
            if addr_m:
                lines = [s.strip() for s in addr_m.group(1).split('\n') if s.strip()]
                if lines:
                    res["registered_address"] = clean_token(lines[0])

            web_m = re.search(
                r'(?:Other\s+(?:homepage\s+)?website|Company\s*Website|Homepage)\s*[:：]?\s*(\S+)',
                text,
                re.I
            )
            if web_m:
                cand = normalize_website(web_m.group(1))
                if cand:
                    res["official_website"] = cand
    except Exception:
        pass
    return res


# ==========================================
# 爬虫主入口
# ==========================================
async def scrape_globalsources_suppliers(keyword: str = "led", max_count: int = 5) -> list[dict]:
    ensure_chrome_running(port=9222)
    clean_kw = keyword.lower().replace("manufacturer", "").strip()

    candidate_sellers = []
    final_results = []
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

        # 1. 列表翻页搜索
        page_num = 1
        max_search_pages = 25

        while len(candidate_sellers) < max_count and page_num <= max_search_pages:
            search_url = f"https://www.globalsources.com/searchList/suppliers?keyWord={quote_plus(clean_kw)}&pageNum={page_num}"
            print(f"\n📑 [浏览器探路] 正在检索搜索列表第 {page_num} 页: {search_url}")

            nav_ok = await safe_navigate(page, search_url)
            if not nav_ok:
                print(f"⚠️ 第 {page_num} 页遭遇持续限流/网络异常，终止翻页。")
                break

            for _ in range(3):
                await page.mouse.wheel(0, random.randint(650, 900))
                await human_delay(0.6, 1.2)

            candidate_elements = await page.query_selector_all(
                'a[href*="manufacturer.globalsources.com/homepage_"], a[href*="/si/"], a.company-name, a.supplier-name'
            )
            if not candidate_elements:
                print(f"🏁 第 {page_num} 页未检测到供应商卡片，搜索已到底！")
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
                await human_delay(3.0, 5.0, desc="翻页冷却防频控")

        raw_cookies = await context.cookies()
        session_cookies = {c['name']: c['value'] for c in raw_cookies}
        user_agent = await page.evaluate("navigator.userAgent")

        try:
            await page.unroute("**/*")
        except Exception:
            pass

        if not candidate_sellers:
            print("\n💡 未检索到任何新增候选供应商，流程结束。")
            return []

        # 2. 深入采集商户（严格加入 60-120 秒冷却）
        print(f"\n🚀 开始深入采集 {len(candidate_sellers)} 家商户（每家商户严格间隔 {MIN_CONTACT_DELAY}-{MAX_CONTACT_DELAY} 秒）...")

        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        async with httpx.AsyncClient(cookies=session_cookies, headers=headers, timeout=12.0) as http_client:
            for idx, seller in enumerate(candidate_sellers, 1):
                comp_name = seller["company"]
                store_url = seller["store_url"]
                print(f"\n🔍 [{idx}/{len(candidate_sellers)}] 深度挖掘: {comp_name}")

                contact_url = re.sub(r'/(homepage|company-profile|showroom)_', '/contact-us_', store_url)
                profile_url = re.sub(r'/(homepage|contact-us|showroom)_', '/company-profile_', store_url)

                contact_task = unlock_and_scrape_contact(page, contact_url)
                profile_task = scrape_company_profile_fast(http_client, profile_url)

                contact_info, profile_info = await asyncio.gather(contact_task, profile_task)

                reg_company = profile_info["registered_company"]
                reg_address = profile_info["registered_address"] or contact_info["fallback_address"]

                official_website = contact_info["official_website"] or profile_info.get("official_website", "")

                dedup.add(comp_name)
                if reg_company:
                    dedup.add(reg_company)

                final_results.append({
                    "company": comp_name,
                    "platform": "Global Sources",
                    "store_url": store_url,
                    "registered_company": reg_company,
                    "registered_address": reg_address,
                    "contact_person": contact_info["contact_person"],
                    "contact_title": contact_info["contact_title"],
                    "official_website": official_website,
                    "phone": contact_info["phone"],
                    "card_product": clean_kw
                })

                # 控制在 60-120 秒获取一个，展示动态倒计时
                if idx < len(candidate_sellers):
                    await account_safe_countdown(min_sec=MIN_CONTACT_DELAY, max_sec=MAX_CONTACT_DELAY)

    print(f"\n🎉 采集全部完成！成功沉淀 {len(final_results)} 家新增商户线索。")
    return final_results