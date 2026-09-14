"""Global Sources Adapter —— 从 crawlers/globalsources.py 迁移而来。

迁移原则（对应 req 5）：
    以前 GlobalSourcesCrawler：Playwright 启动 + httpx 请求 + 数据解析 三件事混在一起；
    现在 GlobalSourcesAdapter：只做 ①提供 URL ②页面解析 ③返回结构化数据。

本文件内**禁止**出现：
    - async_playwright / p.chromium.launch / connect_over_cdp  （浏览器由 Crawlee 管）
    - httpx.AsyncClient / requests                             （请求由 Crawlee 发）
    - browser.new_page / context.new_page                     （页面由 Crawlee 开）
需要新页面时只能通过 Crawlee 的 context.add_requests(...) 入队，由底座去抓。

legacy 实现仍保留在 crawlers/globalsources.py，未删除、未改动；
两者可并存对比，确认 Adapter 产出稳定后再决定是否切换/下线 legacy。

页面类型（由 Request.label / user_data 携带，parse 通过 response.meta 读取）：
    search  —— 检索结果页：解析候选商户 -> 入队其 company-profile / contact-us 页
    company —— 公司资料页：解析工商名 / 注册地址 / 独立站 -> 合并后输出结构化记录
"""

import re
from urllib.parse import quote_plus

from adapters.base import AdapterResponse, BaseAdapter
from utils.dedup import dedup
from utils.logger import get_logger
from utils.parsing import find_labeled_value, soup_from_html
from utils.text import clean_token, is_valid_company_name, normalize_website

logger = get_logger("adapter.gs")

LABEL_SEARCH = "search"
LABEL_COMPANY = "company"

# 与 legacy 完全一致的字段标签与停用词（迁移期保持解析行为一致）
COMPANY_LABELS = [
    r'Registered\s*Company(?:\s*Name)?', r'Company\s*Name', r'Legal\s*Business\s*Name', r'Business\s*Name',
]
COMPANY_STOPS = ["registration number", "business type", "year established", "country", "view more", "undefined", "null"]
ADDR_LABELS = [
    r'Company\s*Registration\s*Address', r'Registered\s*Address', r'Registration\s*Address',
    r'Operational\s*Address', r'Factory\s*Address', r'Business\s*Address', r'Office\s*Address', r'Address',
]
ADDR_STOPS = ["zip code", "country/region", "view more", "view less", "null", "production capacity", "* in china"]

CANDIDATE_SELECTOR = (
    'a[href*="manufacturer.globalsources.com/homepage_"], a[href*="/si/"], a.company-name, a.supplier-name'
)
PRODUCT_URL_MARKERS = ("/pdtl/", "/product_", "productdetail", "/product/")

# 已知字段名清单：用于截断"同行串到下个标签"的粘连值。
# legacy 的 (.+)$ 贪婪捕获在 <span> 等行内布局下会把后续字段一起吞掉
# （实测 registered_company 会变成 "XXX Co., Ltd Company Registration Address : ... Other website : ..."），
# 迁移到 Adapter 时一并修正。legacy 文件保持原样不动。
LABEL_HINTS = [
    r'Registered\s*Company(?:\s*Name)?', r'Company\s*Name', r'Legal\s*Business\s*Name', r'Business\s*Name',
    r'Company\s*Registration\s*Address', r'Registered\s*Address', r'Registration\s*Address',
    r'Operational\s*Address', r'Factory\s*Address', r'Business\s*Address', r'Office\s*Address', r'Address',
    r'Company\s*Registration\s*Number', r'Registered\s*Capital', r'Business\s*Type', r'Year\s*Established',
    r'Country\s*/?\s*Region', r'Main\s*Markets', r'Total\s*Employees', r'Production\s*Capacity',
    r'Contact\s*Person', r'Job\s*Title', r'Telephone', r'Phone', r'Mobile', r'Fax', r'E-?mail',
    r'Website', r'Other\s+(?:homepage\s+)?website', r'Zip\s*Code', r'Postal\s*Code',
    r'Export\s*(?:Percentage|Ratio)', r'OEM\s*(?:Experience|Service)', r'R&D\s*Capacity',
]
_NEXT_LABEL_RE = re.compile(r'\s+(?:' + "|".join(LABEL_HINTS) + r')\s*[:：]', re.I)


class GlobalSourcesAdapter(BaseAdapter):

    platform_name = "Global Sources"
    # 检索页为 JS 渲染，链接需浏览器执行后才有；如后续发现链接为服务端渲染，可改 False 走 soup 模式
    requires_browser = True

    SEARCH_URL = "https://www.globalsources.com/searchList/suppliers?keyWord={kw}&pageNum={page}"
    MAX_SEARCH_PAGES = 25

    def __init__(self, keyword: str = "", pages: int = 1):
        super().__init__()
        self.keyword = (keyword or "").lower().replace("manufacturer", "").strip()
        self.pages = min(max(int(pages), 1), self.MAX_SEARCH_PAGES)
        # 跨页面累积：company_key -> {"expect": n, "handled": n, "emitted": bool, "record": {...}}
        self._companies: dict = {}
        self._seen_names: set = set()

    # ------------------------------------------------------------------ #
    # ① 提供 URL
    # ------------------------------------------------------------------ #
    def start_urls(self):
        if not self.keyword:
            # 保持原 adapter 的默认行为
            return ["https://www.globalsources.com"]
        return [
            self.SEARCH_URL.format(kw=quote_plus(self.keyword), page=page)
            for page in range(1, self.pages + 1)
        ]

    # ------------------------------------------------------------------ #
    # ② 页面解析（纯函数：只依赖 AdapterResponse，可离线单测）
    # ------------------------------------------------------------------ #
    def parse(self, response: AdapterResponse):
        page_type = response.meta.get("page_type") or self.page_type_of(response.url)
        if page_type == LABEL_COMPANY:
            return self.parse_company(response)
        return self.parse_search(response)

    def parse_search(self, response: AdapterResponse) -> list:
        """解析检索结果页，返回候选商户（结构化，尚未成为线索）。

        迁移自 legacy 的候选提取逻辑：选择器 -> 过滤商品链接 -> 名称校验 -> 去重。
        """
        soup = response.soup or soup_from_html(response.html)
        if soup is None:
            return []

        candidates = []
        for el in soup.select(CANDIDATE_SELECTOR):
            href = (el.get("href") or "").strip()
            if not href:
                continue
            if any(marker in href.lower() for marker in PRODUCT_URL_MARKERS):
                continue

            text = el.get_text(" ", strip=True)
            title_attr = (el.get("title") or "").strip()
            name = title_attr if is_valid_company_name(title_attr) else text
            if not is_valid_company_name(name):
                continue

            store_url = self.format_clean_url(href).rstrip("/")
            if not store_url:
                continue
            if dedup.is_seen(name) or name in self._seen_names:
                continue
            self._seen_names.add(name)

            profile_url, contact_url = self.resolve_target_urls(store_url)
            candidates.append(
                {
                    "company": name,
                    "store_url": store_url,
                    "profile_url": profile_url,
                    "contact_url": contact_url,
                    "card_product": self.keyword,
                }
            )

        if not candidates:
            logger.info(f"      ℹ️ [GS] 检索页未解析到候选商户: {response.url}")
        return candidates

    def parse_company(self, response: AdapterResponse) -> dict:
        """解析公司资料页（company-profile / contact-us），返回字段字典。

        迁移自 legacy 的 _parse_detail：先按"标签: 值"行匹配，再用 BeautifulSoup 兜底，
        最后从 "Other website" 提取独立站。
        """
        html = response.html or ""
        soup = response.soup or soup_from_html(html)
        text = self.html_to_text(html)

        record = {
            "registered_company": "",
            "registered_address": "",
            "official_website": "",
        }

        record["registered_company"] = self.extract_field_from_text(text, COMPANY_LABELS, COMPANY_STOPS)
        if not record["registered_company"]:
            record["registered_company"] = self.clean_field_value(find_labeled_value(soup, COMPANY_LABELS, COMPANY_STOPS))

        record["registered_address"] = self.extract_field_from_text(text, ADDR_LABELS, ADDR_STOPS)
        if not record["registered_address"]:
            record["registered_address"] = self.clean_field_value(find_labeled_value(soup, ADDR_LABELS, ADDR_STOPS))

        record["official_website"] = self.extract_official_website(text, html)
        return record

    # ------------------------------------------------------------------ #
    # ③ 请求处理：入队（交给 Crawlee 抓） + 累积与输出
    # ------------------------------------------------------------------ #
    async def handle(self, context) -> None:
        response = await self.build_response(context)
        self.pages_handled += 1
        page_type = response.meta.get("page_type") or self.page_type_of(response.url)

        if page_type == LABEL_COMPANY:
            record = self.parse(response)
            if record:
                self._merge_company(response.meta, record, response)
            return

        candidates = self.parse(response)
        await self.enqueue_companies(context, candidates)
        await self.enqueue_next_search_page(context, response)

    async def enqueue_companies(self, context, candidates: list) -> None:
        """把候选商户的资料页/联系页入队（交给 Crawlee 抓取）。

        只传纯 URL 字符串：适配器不构造 Request、不导入 crawlee，
        页面类型由 URL 形态推导（page_type_of），商户归属由 supplier_key_of 推导，
        因此不依赖任何传输层元数据（元数据丢失也不会串号）。
        """
        if not candidates:
            return
        urls = []
        for cand in candidates:
            key = self.supplier_key_of(cand["store_url"])
            self._companies[key] = {
                "expect": 2,
                "handled": 0,
                "emitted": False,
                "record": {
                    "company": cand["company"],
                    "platform": self.platform_name,
                    "store_url": cand["store_url"],
                    "card_product": cand.get("card_product", self.keyword),
                    "registered_company": "",
                    "registered_address": "",
                    "official_website": "",
                },
            }
            urls.extend(u for u in (cand["profile_url"], cand["contact_url"]) if u)

        if not urls:
            return
        try:
            await context.add_requests(urls)
            logger.info(f"      🔗 [GS] 入队 {len(urls)} 个公司资料页（来自 {len(candidates)} 家候选）")
        except Exception as e:
            self.errors.append(f"enqueue_companies: {type(e).__name__}: {e}")
            logger.warning(f"      ⚠️ [GS] 入队公司资料页失败: {e!r}")

    async def enqueue_next_search_page(self, context, response: AdapterResponse) -> None:
        """按 self.pages 上限入队下一页检索结果。"""
        if not self.keyword or self.pages <= 1:
            return
        current = self.page_num_of(response.url)
        if current is None or current >= self.pages:
            return

        next_url = self.SEARCH_URL.format(kw=quote_plus(self.keyword), page=current + 1)
        try:
            await context.add_requests([next_url])
        except Exception as e:
            logger.debug(f"      [GS] 入队下一页失败: {e!r}")

    def _merge_company(self, meta: dict, record: dict, response: AdapterResponse) -> None:
        # 优先用 URL 推导的稳定 key；user_data 里的 company_key 仅作兜底
        key = self.supplier_key_of(response.url) or meta.get("company_key") or response.url
        state = self._companies.get(key)
        if state is None:
            # 直接访问详情页（无列表页上下文）时补建状态，保证仍能产出记录
            state = {
                "expect": 1,
                "handled": 0,
                "emitted": False,
                "record": {
                    "company": meta.get("company", ""),
                    "platform": self.platform_name,
                    "store_url": response.url,
                    "card_product": self.keyword,
                    "registered_company": "",
                    "registered_address": "",
                    "official_website": "",
                },
            }
            self._companies[key] = state

        state["handled"] += 1
        target = state["record"]
        for field in ("registered_company", "registered_address", "official_website"):
            value = record.get(field, "")
            # 已有的非空值不被覆盖（先出现的页面优先，与 legacy 的 profile -> contact 顺序一致）
            if value and not target.get(field):
                target[field] = value

        if not state["emitted"] and state["handled"] >= state["expect"]:
            self._emit_company(key)

    def _emit_company(self, key: str) -> int:
        state = self._companies.get(key)
        if not state or state["emitted"]:
            return 0
        state["emitted"] = True
        record = state["record"]

        company = record.get("company") or ""
        registered = record.get("registered_company") or ""
        if not (company or registered):
            logger.info(f"      ℹ️ [GS] 资料页无有效字段，跳过: {record.get('store_url')}")
            return 0

        # 与 legacy 一致：详情解析成功后才写入指纹库，避免列表阶段提前占位
        if company:
            dedup.add(company)
        if registered:
            dedup.add(registered)

        self.add_items([record])
        return 1

    def flush(self) -> int:
        """爬取结束时的补出：部分页面失败导致未凑齐的商户，按已有字段输出。"""
        emitted = 0
        for key, state in self._companies.items():
            if not state["emitted"] and state["handled"] > 0:
                emitted += self._emit_company(key)
        if emitted:
            logger.info(f"      🧾 [GS] flush 补出 {emitted} 条（部分资料页未成功抓取）")
        return emitted

    # ------------------------------------------------------------------ #
    # 纯工具函数（从 legacy 迁移，逻辑保持一致）
    # ------------------------------------------------------------------ #
    @staticmethod
    def company_key(store_url: str, company: str) -> str:
        return (store_url or company or "").strip().rstrip("/") or company

    @staticmethod
    def supplier_key_of(url: str) -> str:
        """从店铺/资料页 URL 推导稳定的商户标识。

        同一家公司的 company-profile_123.htm 与 contact-us_123.htm 会得到同一个 key（gs:123），
        因此跨页累积不依赖 Request.user_data 是否完整回传（传输层元数据可能丢失/被包装）。
        """
        m = re.search(r'/(?:homepage|contact-us|company-profile|showroom)_(\d+)\.htm', url or "", re.I)
        if m:
            return f"gs:{m.group(1)}"
        m = re.search(r'/si/(\d+)', url or "", re.I)
        if m:
            return f"gs:{m.group(1)}"
        return (url or "").split("?")[0].strip().rstrip("/")

    @staticmethod
    def page_num_of(url: str):
        m = re.search(r'[?&]pageNum=(\d+)', url or "", re.I)
        return int(m.group(1)) if m else None

    @staticmethod
    def page_type_of(url: str) -> str:
        """由 URL 形态推导页面类型（不依赖 Request 元数据，元数据丢失也能正确路由）。"""
        if re.search(r'(?:company-profile|contact-us|homepage|showroom)[_./]', url or "", re.I):
            return LABEL_COMPANY
        return LABEL_SEARCH

    @staticmethod
    def format_clean_url(href: str) -> str:
        """迁移自 legacy 的 _format_clean_url。"""
        if not href:
            return ""
        href = href.strip()
        if href.startswith("//"):
            return f"https:{href}"
        if href.startswith("/"):
            return f"https://www.globalsources.com{href}"
        if not href.startswith("http"):
            return f"https://www.globalsources.com/{href}"
        return href

    @staticmethod
    def resolve_target_urls(store_url: str):
        """迁移自 legacy 的 _resolve_target_urls：由任意店铺 URL 推导出资料页/联系页 URL。

        注意：这里只做 URL 推导，不做请求；其中 /si/ 路径本身被 GS robots.txt 禁止抓取，
        我们不会请求它，只会请求推导出的 company-profile_*.htm / contact-us_*.htm。
        """
        if not store_url:
            return "", ""
        if re.search(r'/(?:homepage|contact-us|company-profile|showroom)_(\d+)\.htm', store_url, re.I):
            profile_url = re.sub(r'/(?:homepage|contact-us|company-profile|showroom)_', '/company-profile_',
                                 store_url, flags=re.I)
            contact_url = re.sub(r'/(?:homepage|contact-us|company-profile|showroom)_', '/contact-us_',
                                 store_url, flags=re.I)
            return profile_url, contact_url

        si_match = re.search(r'/si/(\d+)', store_url, re.I)
        if si_match:
            supplier_id = si_match.group(1)
            base_site = store_url.split('/si/')[0]
            return f"{base_site}/company-profile_{supplier_id}.htm", f"{base_site}/contact-us_{supplier_id}.htm"

        clean_base = store_url.rstrip('/')
        return f"{clean_base}/company-profile.htm", f"{clean_base}/contact-us.htm"

    @classmethod
    def clean_field_value(cls, value: str) -> str:
        """清洗字段值：先截断"同行粘连到下一个标签"的部分，再做通用清洗。

        legacy 的 (.+)$ 贪婪捕获在行内布局（<span> 同行）下会把后续字段吞进当前字段，
        这是真实缺陷，迁移到 Adapter 时修正；legacy 文件保持不动。
        """
        if not value:
            return ""
        m = _NEXT_LABEL_RE.search(value)
        if m:
            value = value[:m.start()]
        return clean_token(value)

    @classmethod
    def extract_field_from_text(cls, text: str, label_patterns: list, stop_words: list) -> str:
        """迁移自 legacy 的 _extract_field_from_text：按"标签: 值"逐行匹配，标签独占一行时取下一行。

        与 legacy 的差异：命中值一律经 clean_field_value 截断粘连，避免同行情景串字段。
        """
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            for pat in label_patterns:
                m = re.match(rf'^{pat}\s*[:：]\s*(.+)$', line, re.I)
                if m:
                    cand = cls.clean_field_value(m.group(1).strip())
                    if cand and not any(sw.lower() in cand.lower() for sw in stop_words):
                        return cand

                if re.match(rf'^{pat}\s*[:：]?$', line, re.I):
                    if i + 1 < len(lines):
                        next_line = lines[i + 1].strip()
                        if (
                            next_line
                            and not any(sw.lower() in next_line.lower() for sw in stop_words)
                            and not re.search(r'[:：]$', next_line)
                        ):
                            cand = cls.clean_field_value(next_line)
                            if cand:
                                return cand

                # 行内标签（非行首）：应对 <span> / 表格合并单元格等"标签不在行首"的布局。
                # legacy 只认行首，遇到这类布局会整段取不到值（实测地址缺失）。
                m_inline = re.search(rf'(?:^|\s){pat}\s*[:：]\s*(.+)$', line, re.I)
                if m_inline:
                    cand = cls.clean_field_value(m_inline.group(1).strip())
                    if cand and not any(sw.lower() in cand.lower() for sw in stop_words):
                        return cand
        return ""

    @staticmethod
    def extract_official_website(text: str, html: str) -> str:
        """迁移自 legacy 的独立站提取：先查文本中的 "Other website:"，再查 HTML 中的链接。"""
        other_m = re.search(r'Other\s+(?:homepage\s+)?website\s*[:：]?\s*([^\s\r\n<"\'>]+)', text or "", re.I)
        if other_m:
            cand = normalize_website(other_m.group(1), exclude_domain="globalsources.com")
            if cand:
                return cand

        if html:
            html_m = re.search(
                r'Other\s+(?:homepage\s+)?website[\s\S]*?(?:href=["\']([^"\']+)["\']|>([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}[^<\s]*))',
                html, re.I,
            )
            if html_m:
                cand = normalize_website(html_m.group(1) or html_m.group(2), exclude_domain="globalsources.com")
                if cand:
                    return cand
        return ""

    @staticmethod
    def html_to_text(html: str) -> str:
        """把 HTML 转成按行组织的纯文本（复用 utils.text 的实现，保持与 legacy 一致）。"""
        from utils.text import html_to_clean_text

        return html_to_clean_text(html)

    def summary(self) -> str:
        base = super().summary()
        return f"{base} | 候选池={len(self._companies)}"
