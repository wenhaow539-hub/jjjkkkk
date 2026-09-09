import asyncio
import os
import random
import re
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from config import USER_DATA_DIR


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
        "contact supplier", "verified", "view more", "view less"
    ]
    for b in bad_tokens:
        if b in val.lower():
            return ""
    return val


def normalize_website(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if "globalsources.com" in url.lower():
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url}"
    return url


async def safe_navigate(page, url: str, timeout: int = 35000, max_retries: int = 3) -> bool:
    """安全导航函数：自动捕获 Too much connections 限流并退避冷却重试"""
    for attempt in range(1, max_retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_timeout(random.randint(1800, 3000))

            # 检测是否触发服务端频控
            body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            if "Too much connections" in body_text:
                cooldown = random.randint(15, 25)
                print(f"      ⏳ [触发服务端限流] 命中 Too much connections，冷却 {cooldown} 秒后重试 (第 {attempt}/{max_retries} 次)...")
                await asyncio.sleep(cooldown)
                continue

            return True
        except Exception as e:
            print(f"      ⚠️ 页面加载受阻 (尝试 {attempt}/{max_retries}): {url.split('/')[-1]} ({e})")
            await asyncio.sleep(random.randint(3, 6))

    return False


async def scrape_supplier_profile_detail(page, store_url: str) -> dict:
    """三页面穿透：分别直达 company-profile、contact-us 与 showroom 提取全面信息"""
    info = {
        "registered_company": "",
        "registered_address": "",
        "contact_person": "",
        "contact_title": "",
        "official_website": "",
        "phone": "",
        "raw_products": "",
        "full_text": ""
    }
    if not store_url or not store_url.startswith("http"):
        return info

    profile_url = re.sub(r'/(homepage|contact-us|showroom)_', '/company-profile_', store_url)
    contact_url = re.sub(r'/(homepage|company-profile|showroom)_', '/contact-us_', store_url)
    showroom_url = re.sub(r'/(homepage|contact-us|company-profile)_', '/showroom_', store_url)

    # ==================== 1. 访问【Company Profile】====================
    print(f"      🏢 [1/3] 访问企业档案: {profile_url}")
    if await safe_navigate(page, profile_url):
        try:
            await page.mouse.wheel(0, 1000)
            await page.wait_for_timeout(1200)

            biz_data = await page.evaluate("""
                () => {
                    let comp = "", addr = "";
                    const bodyText = document.body.innerText || "";
                    const compMatch = bodyText.match(/Registered\\s*Company\\s*[:：]?\\s*([\\s\\S]*?)(?:Registration\\s*Number|Company\\s*Registration Address)/i);
                    if (compMatch) {
                        const lines = compMatch[1].split('\\n').map(s => s.trim()).filter(Boolean);
                        if (lines.length > 0) comp = lines[0];
                    }
                    const addrMatch = bodyText.match(/(?:Company\\s*Registration\\s*Address|Registered\\s*Address)\\s*[:：]?\\s*([\\s\\S]*?)(?:\\*\\s*In\\s*China|View\\s*Less|Production\\s*Capacity|\\n\\n)/i);
                    if (addrMatch) {
                        const lines = addrMatch[1].split('\\n').map(s => s.trim()).filter(Boolean);
                        if (lines.length > 0) addr = lines[0];
                    }
                    return { comp, addr, rawText: bodyText.slice(0, 2000) };
                }
            """)
            info["registered_company"] = clean_token(biz_data.get("comp", ""))
            info["registered_address"] = clean_token(biz_data.get("addr", ""))
            info["full_text"] = biz_data.get("rawText", "")

            if info["registered_company"]:
                print(f"      📌 [抓取成功] 法定公司: {info['registered_company']}")
            if info["registered_address"]:
                print(f"      📌 [抓取成功] 注册地址: {info['registered_address']}")
        except Exception as e:
            print(f"      ⚠️ 工商档案解析异常: {e}")

    # 子页面缓冲，降低单域名请求频次
    await asyncio.sleep(random.uniform(2.0, 3.5))

    # ==================== 2. 访问【Contact Us】====================
    print(f"      👤 [2/3] 访问联系人档案: {contact_url}")
    if await safe_navigate(page, contact_url):
        try:
            try:
                view_more_buttons = await page.query_selector_all('button:has-text("View More"), a:has-text("View More"), [class*="view-more"]')
                for btn in view_more_buttons:
                    if await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(800)
            except Exception:
                pass

            contact_data = await page.evaluate("""
                () => {
                    let person = "", title = "", fallbackAddr = "", officialWebsite = "";
                    let phoneList = [];

                    const nameEl = document.querySelector('.contact-name');
                    const workerEl = document.querySelector('.contact-worker');
                    if (nameEl) person = nameEl.innerText.replace(/\\s+/g, ' ').trim();
                    if (workerEl) title = workerEl.innerText.replace(/\\s+/g, ' ').trim();

                    const items = document.querySelectorAll('.contact-item');
                    for (const it of items) {
                        const label = it.querySelector('.contact-label')?.innerText || "";
                        const val = it.querySelector('.contact-value')?.innerText || "";
                        if (/Other\\s+homepage\\s+website/i.test(label) && val) {
                            officialWebsite = val.trim();
                        }
                        if (/(?:Telephone|Mobile\\s*Phone|Phone|Fax)/i.test(label) && val) {
                            const cleanVal = val.replace(/view\\s*more/ig, '').trim();
                            if (cleanVal && !phoneList.includes(cleanVal)) {
                                phoneList.push(cleanVal);
                            }
                        }
                    }

                    const bodyText = document.body.innerText || "";
                    if (phoneList.length === 0) {
                        const mPhone = bodyText.match(/(?:Telephone|Mobile\\s*Phone)\\s*[:：]?\\s*([+\\d\\s-]{7,25})/i);
                        if (mPhone && !/view/i.test(mPhone[1])) {
                            phoneList.push(mPhone[1].trim());
                        }
                    }

                    const mAddr = bodyText.match(/Address\\s*[:：]?\\s*([^\\n\\r]+)/i);
                    if (mAddr) fallbackAddr = mAddr[1].trim();

                    return {
                        person,
                        title,
                        officialWebsite,
                        fallbackAddr,
                        phone: phoneList.join(" / ")
                    };
                }
            """)
            info["contact_person"] = clean_token(contact_data.get("person", ""))
            info["contact_title"] = clean_token(contact_data.get("title", ""))
            info["official_website"] = normalize_website(contact_data.get("officialWebsite", ""))
            info["phone"] = clean_token(contact_data.get("phone", ""))

            if not info["registered_address"] and contact_data.get("fallbackAddr"):
                info["registered_address"] = clean_token(contact_data.get("fallbackAddr"))

            if info["contact_person"]:
                print(f"      👤 [抓取成功] 联系人: {info['contact_person']} | 职位: {info['contact_title'] or '未注明'}")
            if info["phone"]:
                print(f"      📞 [解掩码成功] 真实联系电话: {info['phone']}")
            if info["official_website"]:
                print(f"      🌐 [抓取成功] 独立企业官网: {info['official_website']}")
        except Exception as e:
            print(f"      ⚠️ 联系人页解析异常: {e}")

    # 子页面缓冲
    await asyncio.sleep(random.uniform(2.0, 3.5))

    # ==================== 3. 访问【Showroom】====================
    print(f"      📦 [3/3] 访问产品展厅: {showroom_url}")
    if await safe_navigate(page, showroom_url):
        try:
            raw_products_data = await page.evaluate("""
                () => {
                    const text = document.body.innerText || "";
                    const m = text.match(/Product\\s*Groups([\\s\\S]*?)(?:All\\s*Products|Product\\s*Highlights|Total\\s*Products|View:)/i);
                    if (m) {
                        const lines = m[1].split('\\n')
                            .map(s => s.trim())
                            .filter(s => s && !/^Product\\s*Groups$/i.test(s));
                        return lines.join(', ');
                    }
                    return "";
                }
            """)
            info["raw_products"] = clean_token(raw_products_data)
            if info["raw_products"]:
                print(f"      🏷️ [抓取成功] 原始产品分组: {info['raw_products']}")
        except Exception as e:
            print(f"      ⚠️ 产品展厅解析异常: {e}")

    return info


async def scrape_globalsources_suppliers(keyword: str = "led", max_count: int = 5) -> list[dict]:
    clean_kw = keyword.lower().replace("manufacturer", "").strip()
    search_url = f"https://www.globalsources.com/searchList/suppliers?keyWord={quote_plus(clean_kw)}&pageNum=1"

    candidate_sellers = []
    final_results = []
    seen_companies = set()

    async with async_playwright() as p:
        print(f"🌐 启动自动化 Chrome 浏览器 (档案路径: {USER_DATA_DIR})...")

        context = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            channel="chrome",
            headless=False,
            proxy={"server": "http://127.0.0.1:7897"},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            ignore_https_errors=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized"
            ],
            viewport=None
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        # ================= 核心防限流：拦截图片、字体与多媒体 =================
        async def block_unnecessary_resources(route):
            # 过滤掉图片、音视频、网页字体、分析埋点，TCP 连接数直接锐减 80%
            if route.request.resource_type in ["image", "media", "font"]:
                await route.abort()
            elif any(beacon in route.request.url.lower() for beacon in
                     ["google-analytics", "doubleclick", "sensorsdata"]):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_unnecessary_resources)
        # ======================================================================


        print(f"🔗 正在导航至供应商搜索列表: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3500)

        # 锁定供应商选项卡
        try:
            if "products" in page.url.lower():
                supplier_tab = await page.query_selector('a[href*="searchList/suppliers"], :has-text("Suppliers")')
                if supplier_tab:
                    await supplier_tab.click()
                    await page.wait_for_timeout(3000)
        except Exception:
            pass

        for _ in range(4):
            await page.mouse.wheel(0, 800)
            await page.wait_for_timeout(1000)

        candidate_elements = await page.query_selector_all(
            'a[href*="manufacturer.globalsources.com/homepage_"], a[href*="/si/"], a.company-name, a.supplier-name'
        )
        if not candidate_elements:
            candidate_elements = await page.query_selector_all('a[href*="manufacturer.globalsources.com"]')

        print(f"📦 筛选到 {len(candidate_elements)} 个供应商候选链接，正在清洗...")

        for el in candidate_elements:
            if len(candidate_sellers) >= max_count:
                break
            href = await el.get_attribute("href") or ""
            if any(pk in href.lower() for pk in ["/pdtl/", "/product_", "productdetail", "/product/"]):
                continue

            text = (await el.inner_text()).strip()
            title_attr = (await el.get_attribute("title") or "").strip()
            comp_name = title_attr if is_valid_company_name(title_attr) else text

            if is_valid_company_name(comp_name) and comp_name not in seen_companies and href:
                seen_companies.add(comp_name)
                candidate_sellers.append({
                    "company": comp_name,
                    "store_url": format_clean_url(href)
                })

        print(f"🎯 成功锁定 {len(candidate_sellers)} 家真实供应商店铺！\n")

        for idx, seller in enumerate(candidate_sellers, 1):
            comp_name = seller["company"]
            store_url = seller["store_url"]
            print(f"🔍 [{idx}/{len(candidate_sellers)}] 深入挖掘: {comp_name}")

            detail_info = await scrape_supplier_profile_detail(page, store_url)

            final_results.append({
                "company": comp_name,
                "platform": "Global Sources",
                "store_url": store_url,
                "registered_company": detail_info["registered_company"],
                "registered_address": detail_info["registered_address"],
                "contact_person": detail_info["contact_person"],
                "contact_title": detail_info["contact_title"],
                "official_website": detail_info["official_website"],
                "phone": detail_info["phone"],
                "raw_products": detail_info["raw_products"],
                "detail_content": detail_info["full_text"],
                "card_product": clean_kw
            })

            # 抓取完毕后随机冷却，避免触发 TCP 连接上限
            vendor_cooldown = random.randint(4000, 7000)
            await page.wait_for_timeout(vendor_cooldown)

        await context.close()

    print(f"\n🎉 深度采集完毕，共获取 {len(final_results)} 家纯供应商线索！")
    return final_results