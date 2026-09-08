import asyncio
import re
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from config import USER_DATA_DIR


def is_valid_company_name(name: str) -> bool:
    name = name.strip()
    if len(name) < 6 or len(name) > 80:
        return False
    invalid_keywords = [
        "verified", "supplier", "inquire", "chat", "contact",
        "manufacturer", "video", "exhibitor", "years", "global sources"
    ]
    if name.lower() in invalid_keywords:
        return False
    return bool(re.search(
        r'\b(co\.|ltd|limited|corp|inc|technology|electronics|industrial|electric|lighting|optoelectronic|trade|display)\b',
        name, re.I))


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
    bad_tokens = ["send inquiry", "inquiry now", "chat now", "inquire", "contact supplier", "verified", "view more",
                  "view less"]
    for b in bad_tokens:
        if b in val.lower():
            return ""
    return val


def normalize_website(url: str) -> str:
    """标准化独立官网 URL"""
    if not url:
        return ""
    url = url.strip()
    if "globalsources.com" in url.lower():
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url}"
    return url


async def scrape_supplier_profile_detail(page, store_url: str) -> dict:
    """双页面穿透：提取工商认证、联系人、职位及企业独立官网"""
    info = {
        "registered_company": "",
        "registered_address": "",
        "contact_person": "",
        "contact_title": "",
        "official_website": "",
        "full_text": ""
    }
    if not store_url or not store_url.startswith("http"):
        return info

    profile_url = re.sub(r'/(homepage|contact-us)_', '/company-profile_', store_url)
    contact_url = re.sub(r'/(homepage|company-profile)_', '/contact-us_', store_url)

    # ==================== 1. 访问【Company Profile】提取工商信息 ====================
    print(f"      🏢 [1/2] 访问企业工商档案: {profile_url}")
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2000)
        await page.mouse.wheel(0, 1000)
        await page.wait_for_timeout(1500)

        biz_data = await page.evaluate("""
            () => {
                let comp = "";
                let addr = "";
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
            print(f"      📌 [抓取成功] Registered Company: {info['registered_company']}")
        if info["registered_address"]:
            print(f"      📌 [抓取成功] Company Registration Address: {info['registered_address']}")

    except Exception as e:
        print(f"      ⚠️ 工商档案页提取异常: {e}")

    # ==================== 2. 访问【Contact Us】提取联系人、职位与独立官网 ====================
    print(f"      👤 [2/2] 访问联系人档案: {contact_url}")
    try:
        await page.goto(contact_url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2500)

        contact_data = await page.evaluate("""
            () => {
                let person = "";
                let title = "";
                let fallbackAddr = "";
                let officialWebsite = "";

                // 1. 精准提取联系人与职位
                const nameEl = document.querySelector('.contact-name');
                const workerEl = document.querySelector('.contact-worker');
                if (nameEl) person = nameEl.innerText.replace(/\\s+/g, ' ').trim();
                if (workerEl) title = workerEl.innerText.replace(/\\s+/g, ' ').trim();

                // 2. 精准提取 Other homepage website 独立官网
                const items = document.querySelectorAll('.contact-item');
                for (const it of items) {
                    const label = it.querySelector('.contact-label')?.innerText || "";
                    const val = it.querySelector('.contact-value')?.innerText || "";
                    if (/Other\\s+homepage\\s+website/i.test(label) && val) {
                        officialWebsite = val.trim();
                        break;
                    }
                }

                // 备用正则扫描官网
                const bodyText = document.body.innerText || "";
                if (!officialWebsite) {
                    const mSite = bodyText.match(/Other\\s+homepage\\s+website\\s*[:：]?\\s*([a-zA-Z0-9.-]+\\.[a-zA-Z]{2,})/i);
                    if (mSite) officialWebsite = mSite[1].trim();
                }

                // 提取备用地址
                const mAddr = bodyText.match(/Address\\s*[:：]?\\s*([^\\n\\r]+)/i);
                if (mAddr) fallbackAddr = mAddr[1].trim();

                return { person, title, officialWebsite, fallbackAddr };
            }
        """)

        info["contact_person"] = clean_token(contact_data.get("person", ""))
        info["contact_title"] = clean_token(contact_data.get("title", ""))
        info["official_website"] = normalize_website(contact_data.get("officialWebsite", ""))

        if not info["registered_address"] and contact_data.get("fallbackAddr"):
            info["registered_address"] = clean_token(contact_data.get("fallbackAddr"))

        if info["contact_person"]:
            print(f"      👤 [抓取成功] 联系人: {info['contact_person']} | 职位: {info['contact_title'] or '未注明'}")
        if info["official_website"]:
            print(f"      🌐 [抓取成功] 独立企业官网: {info['official_website']}")

    except Exception as e:
        print(f"      ⚠️ 联系人页提取异常: {e}")

    return info


async def scrape_globalsources_suppliers(keyword: str = "led", max_count: int = 5) -> list[dict]:
    clean_kw = keyword.lower().replace("manufacturer", "").strip()
    search_url = f"https://www.globalsources.com/searchList/suppliers?keyWord={quote_plus(clean_kw)}&pageNum=1"

    candidate_sellers = []
    final_results = []
    seen_companies = set()

    async with async_playwright() as p:
        print("🌐 启动 Chrome 自动化浏览器...")

        context = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            channel="chrome",
            headless=False,
            proxy={"server": "http://127.0.0.1:7897"},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            ignore_https_errors=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                "--no-sandbox"
            ],
            viewport=None
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        print(f"🔗 正在检索商户列表: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)

        for _ in range(4):
            await page.mouse.wheel(0, 800)
            await page.wait_for_timeout(1000)

        all_links = await page.query_selector_all("a")
        for a in all_links:
            if len(candidate_sellers) >= max_count:
                break
            text = (await a.inner_text()).strip()
            href = await a.get_attribute("href") or ""

            if is_valid_company_name(text) and text not in seen_companies and href:
                seen_companies.add(text)
                candidate_sellers.append({
                    "company": text,
                    "store_url": format_clean_url(href)
                })

        print(f"📦 一级搜索完成，锁定 {len(candidate_sellers)} 家待深入穿透的供应商！\n")

        for idx, seller in enumerate(candidate_sellers, 1):
            comp_name = seller["company"]
            store_url = seller["store_url"]
            print(f"🔍 [{idx}/{len(candidate_sellers)}] 深入挖掘: {comp_name}")

            detail_info = await scrape_supplier_profile_detail(page, store_url)

            final_results.append({
                "company": comp_name,
                "platform": "GlobalSources",
                "store_url": store_url,
                "registered_company": detail_info["registered_company"],
                "registered_address": detail_info["registered_address"],
                "contact_person": detail_info["contact_person"],
                "contact_title": detail_info["contact_title"],
                "official_website": detail_info["official_website"],
                "detail_content": detail_info["full_text"],
                "card_product": clean_kw
            })
            await page.wait_for_timeout(1500)

        await context.close()

    print(f"\n🎯 深度采集完毕，共获取 {len(final_results)} 家供应商！")
    return final_results