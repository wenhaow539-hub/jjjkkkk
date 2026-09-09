import re
import aiohttp
from typing import Tuple, List

# 正则提取器
EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
WHATSAPP_REGEX = r'(?:https?://(?:wa\.me|api\.whatsapp\.com/send\?phone=)|whatsapp:\s*)([+\d\s-]{8,20})'


async def search_official_website(company_name: str, api_key: str = "") -> str:
    """
    通过 Google Search / SerpAPI 根据法定全称搜索独立官网
    （若无第三方 API，可接入免费的 DuckDuckGo / SearXNG 接口）
    """
    # 模拟外部反查逻辑：若原本已有企业自建站则直接复用
    clean_query = company_name.replace("Co., Ltd", "").strip()
    # 生产环境中调用: https://serpapi.com/search.json?q={clean_query}+official+website
    return ""


async def extract_contacts_from_site(website_url: str) -> Tuple[List[str], List[str]]:
    """深度穿透外部独立站首页及 /contact-us，反查邮箱与 WhatsApp"""
    if not website_url or not website_url.startswith("http"):
        return [], []

    emails = set()
    whatsapps = set()
    target_urls = [website_url, website_url.rstrip("/") + "/contact", website_url.rstrip("/") + "/contact-us"]

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        for url in target_urls:
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                async with session.get(url, headers=headers, ssl=False) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        for em in re.findall(EMAIL_REGEX, html):
                            if not any(ign in em.lower() for ign in [".png", ".jpg", "example", "domain"]):
                                emails.add(em)
                        for wa in re.findall(WHATSAPP_REGEX, html):
                            whatsapps.add(wa.strip())
            except Exception:
                continue

    return list(emails), list(whatsapps)