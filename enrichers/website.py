import asyncio
import html
import re
from urllib.parse import urljoin, urlparse
import httpx


class WebsiteEnricher:
    def __init__(self, concurrency: int = 5):
        self.concurrency = concurrency
        self.semaphore = asyncio.Semaphore(concurrency)
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Upgrade-Insecure-Requests": "1",
        }
        self.fallback_paths = [
            "/contact", "/contact-us", "/contact.html", "/contactus.html", "/contact-us.html",
            "/pages/contact", "/pages/contact-us", "/en/contact-us", "/en/contact",
            "/lianxiwomen/", "/lianxiwomen", "/lxwm/", "/lxwm", "/lxwm.html",
            "/about", "/about-us", "/about.html"
        ]

    def _decode_cf_email(self, cfemail: str) -> str:
        """解密 Cloudflare 混淆邮箱"""
        try:
            r = int(cfemail[:2], 16)
            email = ''.join([chr(int(cfemail[i:i+2], 16) ^ r) for i in range(2, len(cfemail), 2)])
            return email.lower() if "@" in email else ""
        except Exception:
            return ""

    def _get_base_domain(self, netloc: str) -> str:
        """提取纯净主域名"""
        host = netloc.lower().split(':')[0]
        if host.startswith("www."):
            return host[4:]
        return host

    def _normalize_url(self, raw_url: str) -> list[str]:
        if not raw_url:
            return []
        u = raw_url.strip().rstrip('/')
        clean_host = re.sub(r'^https?://', '', u, flags=re.I).strip('/')
        if not clean_host:
            return []

        candidates = [
            f"https://{clean_host}",
            f"https://www.{clean_host}" if not clean_host.startswith("www.") else f"https://{clean_host}",
            f"http://{clean_host}",
            f"http://www.{clean_host}" if not clean_host.startswith("www.") else f"http://{clean_host}",
        ]

        seen = set()
        result = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                result.append(c)
        return result

    def _safe_decode(self, resp: httpx.Response) -> str:
        content = resp.content
        if not content:
            return ""

        charset = None
        ct = resp.headers.get("content-type", "").lower()
        m_ct = re.search(r"charset=['\"]?([a-zA-Z0-9_\-]+)", ct)
        if m_ct:
            charset = m_ct.group(1)

        if not charset:
            meta_m = re.search(rb"charset=['\"]?([a-zA-Z0-9_\-]+)", content[:2048], re.I)
            if meta_m:
                try:
                    charset = meta_m.group(1).decode("ascii")
                except Exception:
                    pass

        for enc in [charset, "utf-8", "gb18030", "gbk", "gb2312"]:
            if not enc:
                continue
            try:
                return content.decode(enc)
            except Exception:
                continue

        return content.decode("utf-8", errors="ignore")

    def _html_to_clean_text(self, raw_html: str) -> str:
        if not raw_html:
            return ""
        text = re.sub(r'<(script|style|svg|noscript)[^>]*>.*?</\1>', ' ', raw_html, flags=re.I | re.S)
        text = re.sub(r'<!--.*?-->', ' ', text, flags=re.S)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = html.unescape(text)
        text = re.sub(r'[ \t]+', ' ', text)
        return text

    def _extract_icp(self, text: str) -> str:
        if not text:
            return ""
        pattern = (
            r"([京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]"
            r"\s*(?:ICP\s*备|ICP\s*证)\s*[0-9]{5,12}\s*号?(?:\s*-\s*[0-9]{1,4})?\s*号?)"
        )
        m = re.search(pattern, text, re.I)
        if m:
            clean_icp = re.sub(r"\s+", "", m.group(1))
            if not clean_icp.endswith("号") and "-" not in clean_icp:
                clean_icp += "号"
            return clean_icp
        return ""

    def _extract_contact_info(self, raw_html: str) -> tuple[str, str]:
        emails = []

        # 1. 结构化属性与混淆邮箱
        for cf in re.findall(r'data-cfemail=["\']([a-fA-F0-9]+)["\']', raw_html):
            dec = self._decode_cf_email(cf)
            if dec:
                emails.append(dec)

        for cf in re.findall(r'email-protection#([a-fA-F0-9]+)', raw_html):
            dec = self._decode_cf_email(cf)
            if dec:
                emails.append(dec)

        for m in re.findall(r'href=["\']mailto:([^"?\'\s]+)', raw_html, re.I):
            m_clean = m.strip().lower()
            if "@" in m_clean:
                emails.append(m_clean)

        visible_text = self._html_to_clean_text(raw_html)

        # 2. 纯文本邮箱
        raw_emails = re.findall(r'\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b', visible_text)
        for em in raw_emails:
            em_lower = em.lower().strip('.')
            if not any(em_lower.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.js', '.css', '.svg']):
                emails.append(em_lower)

        clean_emails = []
        for e in emails:
            if e not in clean_emails:
                clean_emails.append(e)

        # 3. 电话号码
        phones = []

        # 手机号 (排除 14x 物联网与假号)
        mobile_pat = (
            r'(?:(?:\+|00)?86[\s\-]?)?'
            r'(1(?:3\d|5[0-35-9]|6[2567]|7[0-35-8]|8\d|9[0-35-9])'
            r'[\s\-]?\d{3,4}[\s\-]?\d{4})\b'
        )
        for m in re.finditer(mobile_pat, visible_text):
            raw_num = m.group(1)
            digits = re.sub(r'\D', '', raw_num)
            if len(digits) == 11 and len(set(digits)) > 3 and not digits.startswith(('1900', '202')):
                phones.append(digits)

        # 固话座机 (兼容分机号)
        landline_pat = (
            r'(?:(?:TEL|Tel|Phone|电话|座机|TEL\.|Tel\.)\s*[:：.]?\s*)?'
            r'(?:(?:\+|00)?86[\s\-]?)?'
            r'(\(?0?\d{2,3}\)?[\s\-]?(?:\d{7,8}|\d{3,4}[\s\-]\d{3,4}))'
            r'((?:\s*[/,]\s*\d{7,8})*)'
        )
        for m in re.finditer(landline_pat, visible_text, re.I):
            main_num = m.group(1).strip()
            sub_nums = m.group(2).strip()
            digits = re.sub(r'\D', '', main_num)
            if 9 <= len(digits) <= 12 and not digits.startswith('1'):
                cleaned = re.sub(r'[\s\-]+', '-', main_num)
                if sub_nums:
                    cleaned += " " + re.sub(r'\s+', '', sub_nums)
                phones.append(cleaned)

        # 400/800 电话
        for m in re.finditer(r'(?:(?:\+|00)?86[\s\-]?)?([48]00[\-\s]?\d{3,4}[\-\s]?\d{3,4})\b', visible_text):
            h = re.sub(r'[\s\-]+', '-', m.group(1))
            phones.append(h)

        clean_phones = []
        for p in phones:
            if p not in clean_phones:
                clean_phones.append(p)

        phone_str = " / ".join(clean_phones[:2]) if clean_phones else ""
        email_str = clean_emails[0] if clean_emails else ""
        return phone_str, email_str

    async def _fetch_html(self, client: httpx.AsyncClient, url: str) -> tuple[str, str, bool]:
        try:
            resp = await client.get(
                url,
                headers=self.headers,
                timeout=15.0,
                follow_redirects=True
            )
            html_text = self._safe_decode(resp)
            return html_text, str(resp.url), True
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError):
            return "", "", False
        except Exception:
            return "", "", False

    async def enrich_lead(self, client: httpx.AsyncClient, website_url: str) -> dict:
        """主探测入口方法：必须保留在 class WebsiteEnricher 内部"""
        result = {"site_phone": "", "email": "", "icp": "无"}
        if not website_url or len(website_url.strip()) < 4:
            return result

        candidate_urls = self._normalize_url(website_url)
        home_html = ""
        final_url = ""
        site_is_alive = False

        async with self.semaphore:
            # 1. 首页探测
            for u in candidate_urls:
                h_text, f_url, alive = await self._fetch_html(client, u)
                if alive:
                    site_is_alive = True
                if h_text and len(h_text) > 150:
                    home_html = h_text
                    final_url = f_url
                    break

            if not site_is_alive and not home_html:
                result["icp"] = "网址打不开"
                return result

            if not home_html:
                result["icp"] = "无"
                return result

            icp = self._extract_icp(home_html)
            if icp:
                result["icp"] = icp

            phone, email = self._extract_contact_info(home_html)
            if phone:
                result["site_phone"] = phone
            if email:
                result["email"] = email

            # 2. 子页面探测
            if not result["site_phone"] or not result["email"]:
                base_url = final_url or candidate_urls[0]
                base_domain = self._get_base_domain(urlparse(base_url).netloc)
                scored_candidates = []

                a_pattern = r'<a\s+[^>]*href=["\']([^"\'#\s]+)["\'][^>]*>(.*?)</a>'
                for m in re.finditer(a_pattern, home_html, re.I | re.S):
                    raw_href = m.group(1).strip()
                    href = html.unescape(raw_href)
                    raw_text = m.group(2).strip()
                    clean_text = re.sub(r'<[^>]+>', '', raw_text).strip().lower()
                    href_lower = href.lower()

                    score = 0
                    if any(k in clean_text for k in ["contact", "联系", "call", "touch", "tel", "phone"]):
                        score += 20
                    if any(k in href_lower for k in ["contact", "lianxi", "lxwm"]):
                        score += 15
                    if any(k in clean_text for k in ["about", "关于"]):
                        score += 10
                    if "catid" in href_lower or "lists" in href_lower:
                        score += 5

                    if score > 0:
                        full_sub = urljoin(base_url, href)
                        if self._get_base_domain(urlparse(full_sub).netloc) == base_domain:
                            scored_candidates.append((score, full_sub))

                scored_candidates.sort(key=lambda x: x[0], reverse=True)
                subpage_urls = []
                for _, u in scored_candidates:
                    if u not in subpage_urls:
                        subpage_urls.append(u)

                clean_base = base_url.rstrip("/")
                for p in self.fallback_paths:
                    full_p = f"{clean_base}{p}"
                    if full_p not in subpage_urls:
                        subpage_urls.append(full_p)

                async def probe_sub(sub_url: str):
                    s_html, _, _ = await self._fetch_html(client, sub_url)
                    if not s_html:
                        return "", "", ""
                    s_icp = self._extract_icp(s_html)
                    s_phone, s_email = self._extract_contact_info(s_html)
                    return s_icp, s_phone, s_email

                tasks = [probe_sub(su) for su in subpage_urls[:5]]
                sub_results = await asyncio.gather(*tasks)

                for s_icp, s_phone, s_email in sub_results:
                    if result["icp"] == "无" and s_icp:
                        result["icp"] = s_icp
                    if not result["site_phone"] and s_phone:
                        result["site_phone"] = s_phone
                    if not result["email"] and s_email:
                        result["email"] = s_email
                    if result["site_phone"] and result["email"] and result["icp"] != "无":
                        break

        return result