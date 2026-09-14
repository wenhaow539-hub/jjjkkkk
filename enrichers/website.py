import asyncio
import re
from urllib.parse import urljoin, urlparse
import httpx
from utils.phone import deduplicate_phone_list

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', re.IGNORECASE)
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.css', '.js', '.ico')
JUNK_EMAIL_DOMAINS = ('wixpress.com', 'sentry.io', 'example.com', 'domain.com', 'google.com', 'myshopify.com')
INVALID_PREFIXES = ('noreply', 'no-reply', 'mailer-daemon', 'donotreply')
ICP_REGEX = re.compile(
    r'([京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Za-z]?ICP备\s*\d+\s*号?(?:-\d+)?|'
    r'[A-Za-z\u4e00-\u9fa5]*ICP[备证]\s*\d+\s*号?(?:-\d+)?|'
    r'[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]\s*公网安备\s*\d+\s*号)',
    re.IGNORECASE
)

class WebsiteEnricher:
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
            if any(e_lower.endswith(ext) for ext in IMAGE_EXTS): continue
            if any(junk in e_lower for junk in JUNK_EMAIL_DOMAINS): continue
            prefix = e_lower.split('@')[0]
            if any(prefix == inv for inv in INVALID_PREFIXES): continue
            if len(e_lower) <= 50: valid_emails.add(e_lower)
        return list(valid_emails)

    def _extract_valid_phones(self, html: str) -> list[str]:
        found_phones = []
        for tel in re.findall(r'href=["\']tel:([^"\'>]+)["\']', html, re.I):
            clean_p = re.sub(r'[^\d+\-\s()]', '', tel.strip())
            if len(re.sub(r'\D', '', clean_p)) >= 7:
                found_phones.append(clean_p)

        wa_pattern = r'(?:wa\.me/(?:send\?.*?[?&]phone=)?|api\.whatsapp\.com/send\?.*?[?&]phone=)([+\d]+)'
        for wa in re.findall(wa_pattern, html, re.I):
            clean_wa = wa.strip()
            if not clean_wa.startswith("+") and clean_wa.startswith("86"):
                clean_wa = "+" + clean_wa
            if len(re.sub(r'\D', '', clean_wa)) >= 8:
                found_phones.append(clean_wa)

        clean_visible_text = re.sub(r'<(script|style|svg|noscript)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        clean_visible_text = re.sub(r'<[^>]+>', ' ', clean_visible_text)

        labeled_matches = re.findall(
            r'(?:phone|telephone|mobile|tel|whatsapp|cell|contact)\s*[:：]?\s*([+\d\s\-\(\)\.]{7,25})',
            clean_visible_text, re.I
        )
        for p in labeled_matches:
            digits = re.sub(r'\D', '', p)
            if 7 <= len(digits) <= 16 and not digits.startswith("202") and not digits.startswith("201"):
                found_phones.append(p.strip())

        for intl in re.findall(r'\+86[\s\-]?(?:1[3-9]\d[\s\-]?\d{4}[\s\-]?\d{4}|[1-9]\d{1,3}[\s\-]?\d{7,8})', clean_visible_text):
            found_phones.append(intl.strip())

        for domestic in re.findall(r'(?:0[1-9]\d{1,2}[\s\-])\d{7,8}', clean_visible_text):
            found_phones.append(domestic.strip())

        return [ph for ph in found_phones if not any(b in ph for b in ["123456", "000000", "888888"])]

    async def _fetch_html_resilient(self, client: httpx.AsyncClient, url: str) -> tuple[int, str, str]:
        candidates = [url]
        if url.startswith("https://"): candidates.append("http://" + url[8:])
        elif url.startswith("http://"): candidates.append("https://" + url[7:])

        parsed = urlparse(url)
        if parsed.netloc:
            if parsed.netloc.startswith("www."):
                candidates.append(parsed._replace(netloc=parsed.netloc[4:]).geturl())
            else:
                candidates.append(parsed._replace(netloc=f"www.{parsed.netloc}").geturl())

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

            icp_match = ICP_REGEX.search(home_html)
            result["icp"] = re.sub(r'\s+', '', icp_match.group(1)) if icp_match else "无"

            raw_emails = self._extract_valid_emails(home_html)
            raw_phones = self._extract_valid_phones(home_html)

            all_links = re.findall(r'href=["\']([^"\']*(?:contact|about)[^"\']*)["\']', home_html, re.I)
            unique_links = list(dict.fromkeys(all_links))
            unique_links.sort(key=lambda x: 0 if "contact" in x.lower() else 1)

            base_domain = urlparse(website).netloc.replace("www.", "")
            sub_pages = []
            for lk in unique_links:
                if lk.startswith("#") or lk.startswith("javascript:") or any(lk.lower().endswith(ext) for ext in IMAGE_EXTS):
                    continue
                full_url = urljoin(website, lk)
                if urlparse(full_url).netloc.replace("www.", "") == base_domain and full_url != website:
                    sub_pages.append(full_url)
                if len(sub_pages) >= 2: break

            if sub_pages:
                tasks = [self._fetch_html_resilient(client, sub_u) for sub_u in sub_pages]
                sub_res_list = await asyncio.gather(*tasks)
                for code, sub_html, _ in sub_res_list:
                    if code == 200 and sub_html:
                        raw_emails.extend(self._extract_valid_emails(sub_html))
                        raw_phones.extend(self._extract_valid_phones(sub_html))

            if raw_emails:
                site_domain = urlparse(website).netloc.replace("www.", "").lower()
                def score_email(e: str) -> int:
                    score = 0
                    if site_domain in e: score += 50
                    if any(k in e for k in ["sales", "info", "contact", "export"]): score += 20
                    if any(free in e for free in ["@gmail.com", "@yahoo.com", "@hotmail.com"]): score -= 10
                    return score
                result["email"] = sorted(list(set(raw_emails)), key=score_email, reverse=True)[0]
                print(f"      📧 [官网邮箱] 提取成功: {result['email']} ({website})")

            clean_phones = deduplicate_phone_list(raw_phones)
            if clean_phones:
                result["site_phone"] = " / ".join(clean_phones[:3])
                print(f"      📞 [官网联系方式] 提取成功: {result['site_phone']} ({website})")

            if result["icp"] != "无":
                print(f"      🛡️ [备案信息] 提取到备案号: {result['icp']} ({website})")

        return result