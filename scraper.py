import asyncio
import os
import random
import re
import socket
import subprocess
import time
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from dedup import dedup

# ==========================================
# 核心功能开关
# ==========================================
# True : 正常点击 View More / Exchange 按钮并解密电话（扣减配额）
# False: 额度用尽时关闭。依然进入联系人页面抓取【姓名、职位、官网】，但绝不点击名片交换按钮
UNLOCK_PHONE = False


def is_port_open(host: str = "127.0.0.1", port: int = 9222) -> bool:
    """检测指定端口是否已被占用（Chrome 是否已就绪）"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def ensure_chrome_running(port: int = 9222, profile_dir: str = r"C:\chrome_debug_profile"):
    """若 Chrome 调试端口未开放，自动寻找系统 Chrome 并启动"""
    if is_port_open("127.0.0.1", port):
        print(f"✅ 检测到 Chrome CDP 端口 {port} 已就绪，直接接入...")
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

    # 注入性能限制参数：约束磁盘与媒体缓存，禁用 GPU 着色器落盘
    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disk-cache-size=20971520",       # 强制磁盘缓存最大 20MB
        "--media-cache-size=1048576",       # 强制媒体缓存最大 1MB
        "--disable-gpu-shader-disk-cache",  # 禁止将渲染着色器写入磁盘
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(15):
        if is_port_open("127.0.0.1", port):
            print("✅ 本地 Chrome 已成功启动并开放调试端口！")
            time.sleep(1)
            return
        time.sleep(1)

    raise TimeoutError("等待 Chrome 启动超时，未能成功绑定 9222 端口。")


async def human_delay(min_sec: float = 2.0, max_sec: float = 4.0, desc: str = ""):
    """高精度拟人化随机延时辅助器"""
    sleep_time = round(random.uniform(min_sec, max_sec), 2)
    if desc:
        print(f"      ⏱️ [{desc}] 模拟停顿 {sleep_time} 秒...")
    await asyncio.sleep(sleep_time)


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


def normalize_website(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if "globalsources.com" in url.lower():
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


def merge_and_dedup_phones(*sources) -> str:
    raw_list = []
    for s in sources:
        if isinstance(s, list):
            raw_list.extend(s)
        elif isinstance(s, str) and s.strip():
            raw_list.extend([p.strip() for p in re.split(r'[/,;]', s) if p.strip()])

    unique_list = []
    for p in raw_list:
        p_clean = p.strip()
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

    return " / ".join(unique_list)


def parse_contact_number_json(data: dict) -> list[str]:
    if not data or not isinstance(data, dict):
        return []

    collected = []

    for item in data.get("telephoneList") or []:
        cc = str(item.get("countryCode", "") or "").strip()
        ac = str(item.get("areaCode", "") or "").strip()
        num = str(item.get("telNum", "") or "").strip()
        ext = str(item.get("extension", "") or "").strip()

        prefix = f"+{cc}" if cc else ""
        main_num = "-".join(filter(None, [prefix, ac, num]))
        if ext:
            main_num += f" ext {ext}"
        if main_num:
            collected.append(main_num)

    mobiles = []
    if "mobileList" in data and isinstance(data["mobileList"], list):
        mobiles.extend(data["mobileList"])
    if data.get("mobile"):
        mobiles.append(data["mobile"])

    for m in mobiles:
        if isinstance(m, dict):
            m_cc = str(m.get("countryCode", "") or "").strip()
            m_ac = str(m.get("areaCode", "") or "").strip()
            m_num = str(m.get("telNum", "") or "").strip()
            prefix = f"+{m_cc}" if m_cc else ""
            full_m = "-".join(filter(None, [prefix, m_ac, m_num]))
            if full_m:
                collected.append(full_m)
        elif isinstance(m, str) and m.strip():
            collected.append(m.strip())

    return collected


async def safe_navigate(page, url: str, timeout: int = 35000, max_retries: int = 3) -> bool:
    """安全导航：捕获 Too much connections 限流并退避冷却重试"""
    for attempt in range(1, max_retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            await human_delay(2.0, 3.5)

            body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            if "Too much connections" in body_text:
                cooldown = random.randint(20, 35)
                print(f"      ⏳ [触发服务端限流] 命中频控上限，强制冷却 {cooldown} 秒 (尝试 {attempt}/{max_retries})...")
                await asyncio.sleep(cooldown)
                continue

            return True
        except Exception as e:
            print(f"      ⚠️ 页面加载受阻 (尝试 {attempt}/{max_retries}): {url.split('/')[-1]} ({e})")
            await human_delay(3.0, 6.0)

    return False


async def scrape_supplier_profile_detail(page, store_url: str) -> dict:
    """三页面穿透：结合拟人交互与间隔挖掘数据"""
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
            await page.mouse.wheel(0, 500)
            await human_delay(0.8, 1.5)
            await page.mouse.wheel(0, 500)
            await human_delay(1.0, 1.8)

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

    await human_delay(2.5, 4.5, desc="档案页浏览完毕")

    # ==================== 2. 访问【Contact Us】====================
    print(f"      👤 [2/3] 访问联系人档案: {contact_url}")
    if await safe_navigate(page, contact_url):
        api_phones = []

        async def capture_contact_api(response):
            if "contact-number" in response.url and response.status == 200:
                try:
                    res_json = await response.json()
                    if res_json.get("code") == "200" and "data" in res_json:
                        extracted = parse_contact_number_json(res_json["data"])
                        if extracted:
                            api_phones.extend(extracted)
                except Exception:
                    pass

        if UNLOCK_PHONE:
            page.on("response", capture_contact_api)

        try:
            await page.mouse.wheel(0, 400)
            await human_delay(1.0, 1.8)

            already_visible_phones = await page.evaluate("""
                () => {
                    let phones = [];
                    const items = document.querySelectorAll('.contact-item');
                    for (const it of items) {
                        const label = (it.querySelector('.contact-label')?.innerText || "").toLowerCase();
                        const val = it.querySelector('.contact-value')?.innerText || "";
                        if (/(?:telephone|mobile)/i.test(label)) {
                            const cleanVal = val.replace(/view\\s*more|exchange/ig, '').trim();
                            if (cleanVal && /\\d{6,}/.test(cleanVal)) {
                                phones.push(cleanVal);
                            }
                        }
                    }
                    return phones;
                }
            """)

            if UNLOCK_PHONE:
                if already_visible_phones:
                    print(f"      ⚡ 检测到联系方式已解锁/直接公开，跳过点击按钮: {already_visible_phones}")
                else:
                    btn_selector = (
                        'button:has-text("View More"), '
                        'a:has-text("View More"), '
                        'div:has-text("View More"):not(:has(div)), '
                        'span:has-text("View More"), '
                        '[class*="view-more"], [class*="viewMore"]'
                    )

                    view_more_btn = None
                    try:
                        view_more_btn = await page.wait_for_selector(btn_selector, timeout=3000, state="visible")
                    except Exception:
                        pass

                    if view_more_btn:
                        print("      🖱️ 发现【View More】遮罩，正在模拟点击...")
                        await view_more_btn.scroll_into_view_if_needed()
                        await view_more_btn.hover()
                        await human_delay(0.8, 1.5, desc="点击前停顿")
                        await view_more_btn.click()
                        await human_delay(1.2, 2.0, desc="等待解密响应")

                        try:
                            exchange_selector = (
                                'button:has-text("Exchange"), '
                                '.el-dialog__footer button:has-text("Exchange"), '
                                'div[role="dialog"] button:has-text("Exchange"), '
                                '.modal-footer button:has-text("Exchange")'
                            )
                            exchange_btn = await page.wait_for_selector(exchange_selector, timeout=2500, state="visible")
                            if exchange_btn:
                                print("      🪪 检测到【Exchange 名片交换】弹窗，自动点击...")
                                await exchange_btn.hover()
                                await exchange_btn.click()
                                await human_delay(1.5, 2.5, desc="名片交换处理")
                        except Exception:
                            pass
            else:
                print("      🛡️ [配额保护] 跳过 View More/名片交换点击，仅提取公开联系人信息与官网。")

            contact_data = await page.evaluate("""
                () => {
                    let person = "", title = "", fallbackAddr = "", officialWebsite = "";
                    let domTelephones = [];
                    let domMobiles = [];

                    const nameEl = document.querySelector('.contact-name');
                    const workerEl = document.querySelector('.contact-worker');
                    if (nameEl) person = nameEl.innerText.replace(/\\s+/g, ' ').trim();
                    if (workerEl) title = workerEl.innerText.replace(/\\s+/g, ' ').trim();

                    const items = document.querySelectorAll('.contact-item');
                    for (const it of items) {
                        const label = (it.querySelector('.contact-label')?.innerText || "").toLowerCase();
                        const val = it.querySelector('.contact-value')?.innerText || "";
                        const cleanVal = val.replace(/view\\s*more|exchange/ig, '').trim();

                        if (/other\\s+homepage\\s+website/i.test(label) && val) {
                            officialWebsite = val.trim();
                        }
                        if (/telephone/i.test(label) && cleanVal) {
                            domTelephones.push(cleanVal);
                        }
                        if (/mobile/i.test(label) && cleanVal) {
                            domMobiles.push(cleanVal);
                        }
                    }

                    const mAddr = document.body.innerText.match(/Address\\s*[:：]?\\s*([^\\n\\r]+)/i);
                    if (mAddr) fallbackAddr = mAddr[1].trim();

                    return {
                        person,
                        title,
                        officialWebsite,
                        fallbackAddr,
                        domTelephones,
                        domMobiles
                    };
                }
            """)

            combined_phone = merge_and_dedup_phones(
                already_visible_phones,
                api_phones,
                contact_data.get("domTelephones", []),
                contact_data.get("domMobiles", [])
            )
            info["phone"] = combined_phone
            info["contact_person"] = clean_token(contact_data.get("person", ""))
            info["contact_title"] = clean_token(contact_data.get("title", ""))
            info["official_website"] = normalize_website(contact_data.get("officialWebsite", ""))

            if not info["registered_address"] and contact_data.get("fallbackAddr"):
                info["registered_address"] = clean_token(contact_data.get("fallbackAddr"))

            if info["contact_person"]:
                print(f"      👤 [抓取成功] 联系人: {info['contact_person']} | 职位: {info['contact_title'] or '未注明'}")
            if info["official_website"]:
                print(f"      🌐 [抓取成功] 企业官网: {info['official_website']}")
            if info["phone"]:
                print(f"      📞 [电话提取/公开可用]: {info['phone']}")

        except Exception as e:
            print(f"      ⚠️ 联系人页解析异常: {e}")
        finally:
            if UNLOCK_PHONE:
                page.remove_listener("response", capture_contact_api)

    await human_delay(2.5, 4.5, desc="联系人页浏览完毕")

    # ==================== 3. 访问【Showroom】====================
    print(f"      📦 [3/3] 访问产品展厅: {showroom_url}")
    if await safe_navigate(page, showroom_url):
        try:
            await page.mouse.wheel(0, 600)
            await human_delay(1.2, 2.0)

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

        # 核心防限流与流量压减：新增 stylesheet 样式表拦截，不下载 CSS
        async def block_unnecessary_resources(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            elif any(beacon in route.request.url.lower() for beacon in ["google-analytics", "doubleclick", "sensorsdata"]):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_unnecessary_resources)

        # ========================================================
        # 核心多页自动翻页检索循环：凑满 max_count 家未抓取的新店铺
        # ========================================================
        page_num = 1
        max_search_pages = 30

        while len(candidate_sellers) < max_count and page_num <= max_search_pages:
            search_url = f"https://www.globalsources.com/searchList/suppliers?keyWord={quote_plus(clean_kw)}&pageNum={page_num}"
            print(f"\n📑 [多页翻页] 正在检索搜索列表第 {page_num} 页: {search_url}")

            nav_ok = await safe_navigate(page, search_url)
            if not nav_ok:
                print(f"⚠️ 第 {page_num} 页加载失败，终止翻页。")
                break

            await human_delay(3.0, 4.5, desc=f"第 {page_num} 页列表加载")

            try:
                if "products" in page.url.lower():
                    supplier_tab = await page.query_selector('a[href*="searchList/suppliers"], :has-text("Suppliers")')
                    if supplier_tab:
                        await supplier_tab.click()
                        await human_delay(2.5, 3.5, desc="切换至供应商列表")
            except Exception:
                pass

            for _ in range(4):
                scroll_delta = random.randint(600, 950)
                await page.mouse.wheel(0, scroll_delta)
                await human_delay(0.8, 1.6)

            candidate_elements = await page.query_selector_all(
                'a[href*="manufacturer.globalsources.com/homepage_"], a[href*="/si/"], a.company-name, a.supplier-name'
            )
            if not candidate_elements:
                candidate_elements = await page.query_selector_all('a[href*="manufacturer.globalsources.com"]')

            if not candidate_elements:
                print(f"🏁 第 {page_num} 页未检测到任何供应商卡片，搜索结果已到底！")
                break

            print(f"📦 第 {page_num} 页发现 {len(candidate_elements)} 个供应商卡片，执行哈希指纹过滤...")

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

                if dedup.is_seen(comp_name) or dedup.is_seen(clean_url):
                    print(f"      ⏭️ [指纹库命中] 跳过已采店铺: {comp_name}")
                    continue

                if is_valid_company_name(comp_name) and comp_name not in seen_companies and href:
                    seen_companies.add(comp_name)
                    candidate_sellers.append({
                        "company": comp_name,
                        "store_url": clean_url
                    })
                    page_added += 1

            print(f"✅ 第 {page_num} 页提取到 {page_added} 家全新供应商（当前累计已就绪: {len(candidate_sellers)}/{max_count}）")

            if len(candidate_sellers) < max_count:
                page_num += 1
                await human_delay(3.0, 5.0, desc="翻页冷却")

        print(f"\n🎯 候选商户锁定完成！共在全站多页中锁定 {len(candidate_sellers)} 家未抓取的新供应商，开始挖掘详情...\n")

        # ========================================================
        # 逐家店铺穿透并实时写入指纹库（包含内存释放机制）
        # ========================================================
        for idx, seller in enumerate(candidate_sellers, 1):
            comp_name = seller["company"]
            store_url = seller["store_url"]
            print(f"\n🔍 [{idx}/{len(candidate_sellers)}] 深入挖掘: {comp_name}")

            detail_info = await scrape_supplier_profile_detail(page, store_url)

            dedup.add(comp_name)
            dedup.add(store_url)
            if detail_info.get("registered_company"):
                dedup.add(detail_info["registered_company"])

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

            if idx < len(candidate_sellers):
                await human_delay(4.0, 7.0, desc=f"完成第 {idx} 家，商铺间防风控冷却")

            # 批次冷却与 Chrome 标签页内存清理重置
            if idx % 5 == 0 and idx < len(candidate_sellers):
                batch_pause = random.randint(15, 25)
                print(f"\n☕ [长效冷却] 已连续抓取 5 家商户，休息 {batch_pause} 秒避开 WAF 频率检测...")

                # 彻底销毁旧标签页并新建，清空 V8 引擎历史栈与临时内存
                try:
                    await page.close()
                    page = await context.new_page()
                    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
                    await page.route("**/*", block_unnecessary_resources)
                    print("🧹 [内存释放] 已重置当前标签页，Chrome 内存占用已释放归零。")
                except Exception as e:
                    print(f"⚠️ 标签页重置失败: {e}")

                await asyncio.sleep(batch_pause)

        try:
            await page.unroute("**/*")
        except Exception:
            pass

    print(f"\n🎉 深度采集完毕，共获取 {len(final_results)} 家新增线索！")
    return final_results