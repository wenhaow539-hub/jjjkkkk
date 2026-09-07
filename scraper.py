import asyncio
import re
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from config import USER_DATA_DIR


def is_valid_company_name(name: str) -> bool:
    """严格过滤年限、国别、纯数字及非公司杂质标签"""
    name = name.strip()
    # 长度过短直接过滤
    if len(name) < 6:
        return False
    # 过滤 "1年 CN", "19 yrs CN" 等年限标识
    if re.search(r'^\d+\s*(年|yr|yrs|year|years)', name, re.I):
        return False
    # 过滤只有几个字符带国别后缀的徽章（如 "5 YRS CN"）
    if re.search(r'\b(CN|HK|TW|US)\b$', name) and len(name) < 15:
        return False
    # 过滤常见平台操作词
    invalid_words = ["contact supplier", "chat now", "supplier", "inquiry", "follow"]
    if name.lower() in invalid_words:
        return False
    return True


async def scrape_alibaba_suppliers(keyword: str, max_count: int = 5) -> list[dict]:
    search_url = f"https://www.alibaba.com/trade/search?SearchText={quote_plus(keyword)}"
    results = []

    async with async_playwright() as p:
        print("🌐 启动自动化浏览器...")
        context = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized"
            ],
            viewport=None
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        print(f"🔗 正在导航至搜索页: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

        # 1. 检测是否触发反爬/安全验证页
        current_url = page.url
        if "punish" in current_url or "sec.alibaba.com" in current_url or "captcha" in current_url:
            print("\n🚨 检测到阿里滑块验证码拦截！")
            print("👉 请在弹出的浏览器窗口中手动完成滑块/拼图验证（程序将等待最多 30 秒）...\n")
            for _ in range(30):
                await page.wait_for_timeout(1000)
                if "punish" not in page.url and "sec.alibaba.com" not in page.url:
                    print("✅ 验证通过，继续解析数据！")
                    break
        else:
            await page.wait_for_timeout(3000)

        # 2. 模拟向下滚动加载异步卡片
        print("📜 正在滚动加载页面列表...")
        for _ in range(3):
            await page.mouse.wheel(0, 800)
            await page.wait_for_timeout(1000)

        # 3. 使用覆盖面最广的链接级选择器定位供应商
        elements = await page.query_selector_all('a[href*="company_profile.html"], a.search-card-e-company')
        print(f"📦 匹配到 {len(elements)} 个候选商户节点，开始筛选...")

        seen_companies = set()
        for el in elements:
            if len(results) >= max_count:
                break

            # 获取公司名称（优先取 title 属性，避免读到内部徽章小字）
            name = await el.get_attribute("title")
            if not name:
                name = await el.inner_text()

            # 清理换行符
            name = name.split("\n")[0].strip()

            # 校验是否为合法公司名
            if not is_valid_company_name(name) or name in seen_companies:
                continue

            link = await el.get_attribute("href") or ""
            if link and not link.startswith("http"):
                link = f"https:{link}"

            seen_companies.add(name)
            results.append({
                "company": name,
                "platform": "Alibaba",
                "store_url": link,
                "card_product": keyword  # 保底以搜索关键词作为主营品类参考
            })

        await context.close()

    print(f"🎯 成功采集到 {len(results)} 家有效供应商！")
    return results