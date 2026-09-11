import asyncio
from datetime import datetime
import os
import re
from urllib.parse import urljoin, urlparse
import httpx
from openpyxl.styles import Alignment, PatternFill
import pandas as pd

import config
from evaluator import evaluate_supplier_icp
from models import RawSupplierLead
from scraper import scrape_globalsources_suppliers

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', re.IGNORECASE)
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.css', '.js', '.ico')
JUNK_EMAIL_DOMAINS = ('wixpress.com', 'sentry.io', 'example.com', 'domain.com', 'google.com', 'myshopify.com')
INVALID_PREFIXES = ('noreply', 'no-reply', 'mailer-daemon', 'donotreply')

# 匹配工信部 ICP 备案号（如 闽ICP备20014183号）及公安网安备案号
ICP_REGEX = re.compile(
    r'([京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Za-z]?ICP备\s*\d+\s*号?(?:-\d+)?|'
    r'[A-Za-z\u4e00-\u9fa5]*ICP[备证]\s*\d+\s*号?(?:-\d+)?|'
    r'[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]\s*公网安备\s*\d+\s*号)',
    re.IGNORECASE
)

TARGET_COLUMNS = [
    "平台名称",
    "公司英文名",
    "平台网址",
    "公司中文名",
    "公司注册地址",
    "联系人",
    "联系人职位",
    "独立站",
    "网址是否有备案",
    "联系方式",
    "搜索的关键词",
    "email",
    "入库时间"
]


# ==========================================
# 电话归一与智能排重模块
# ==========================================
def get_canonical_phone(raw_phone: str) -> str:
    """提取号码的核心数字指纹，剥离 +86、0086、前导0与标点空格差异"""
    if not raw_phone:
        return ""
    main_part = re.split(r'(?:ext|分机|转)', raw_phone, flags=re.I)[0]
    digits = re.sub(r'\D', '', main_part)

    # 剥离国家代码 0086 / 86
    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) >= 11:
        digits = digits[2:]

    # 剥离国内固话区号前导 0 (例如 0755 -> 755, 0519 -> 519)
    if digits.startswith("0") and len(digits) >= 10:
        digits = digits[1:]

    return digits


def score_phone_format(val: str) -> int:
    """对同一物理号码的不同展示排版打分，优先保留人类可读性最规范的版本"""
    score = 0
    if "+" in val:
        score += 5
    if "-" in val:
        score += 3
    if " " in val:
        score += 2
    # 扣分：+86 紧跟 0 是不规范的国际写法 (+86 0755...)
    if re.search(r'(?:\+86|86)[\s\-]*0\d', val):
        score -= 4
    # 纯无分隔符长串减分
    if re.match(r'^\+?\d+$', val.strip()):
        score -= 1
    return score


def deduplicate_phone_list(phones: list[str]) -> list[str]:
    """根据核心数字指纹去重，同号码优选排版质量最高的版本"""
    seen_dict: dict[str, str] = {}

    for p in phones:
        clean_p = re.sub(r'^[^\d+]+|[^\d]+$', '', p.strip())
        fingerprint = get_canonical_phone(clean_p)

        if not fingerprint or len(fingerprint) < 7:
            continue

        if fingerprint not in seen_dict:
            seen_dict[fingerprint] = clean_p
        else:
            existing_val = seen_dict[fingerprint]
            if score_phone_format(clean_p) > score_phone_format(existing_val):
                seen_dict[fingerprint] = clean_p

    return list(seen_dict.values())


# ==========================================
# 独立站深度挖掘与解析器
# ==========================================
class WebsiteEnricher:
    """利用 HTTPX 穿透独立站，提取商业邮箱、联系方式（去重规范）与 ICP 备案"""
    def __init__(self, concurrency: int = 5):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.timeout = httpx.Timeout(connect=6.0, read=12.0, write=6.0, pool=6.0)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1"
        }

    def _extract_valid_emails(self, text: str) -> list[str]:
        raw_matches = EMAIL_REGEX.findall(text)
        valid_emails = set()
        for e in raw_matches:
            e_lower = e.lower().strip()
            if any(e_lower.endswith(ext) for ext in IMAGE_EXTS):
                continue
            if any(junk in e_lower for junk in JUNK_EMAIL_DOMAINS):
                continue
            prefix = e_lower.split('@')[0]
            if any(prefix == inv for inv in INVALID_PREFIXES):
                continue
            if len(e_lower) <= 50:
                valid_emails.add(e_lower)
        return list(valid_emails)

    def _extract_valid_phones(self, html: str) -> list[str]:
        found_phones = []

        # 1. 点击拨号链接: a href="tel:..."（包括移动端吸底悬浮按钮）
        for tel in re.findall(r'href=["\']tel:([^"\'>]+)["\']', html, re.I):
            clean_p = re.sub(r'[^\d+\-\s()]', '', tel.strip())
            if len(re.sub(r'\D', '', clean_p)) >= 7:
                found_phones.append(clean_p)

        # 2. WhatsApp 链接提取（兼容任意参数顺序，例如 ?l=en&phone=...）
        wa_pattern = r'(?:wa\.me/(?:send\?.*?[?&]phone=)?|api\.whatsapp\.com/send\?.*?[?&]phone=)([+\d]+)'
        for wa in re.findall(wa_pattern, html, re.I):
            clean_wa = wa.strip()
            if not clean_wa.startswith("+") and clean_wa.startswith("86"):
                clean_wa = "+" + clean_wa
            if len(re.sub(r'\D', '', clean_wa)) >= 8:
                found_phones.append(clean_wa)

        # 3. 剥离 script 与 style 代码块，防止匹配到底层开发模板代码与测试噪音
        clean_visible_text = re.sub(
            r'<(script|style|svg|noscript)[^>]*>.*?</\1>', '', html,
            flags=re.DOTALL | re.IGNORECASE
        )
        clean_visible_text = re.sub(r'<[^>]+>', ' ', clean_visible_text)

        # 4. 正文中带显式标签的号码 (Phone / Tel / Mobile / WhatsApp)
        labeled_matches = re.findall(
            r'(?:phone|telephone|mobile|tel|whatsapp|cell)\s*[:：]?\s*([+\d\s\-\(\)\.]{7,25})',
            clean_visible_text, re.I
        )
        for p in labeled_matches:
            digits = re.sub(r'\D', '', p)
            if 7 <= len(digits) <= 16 and not digits.startswith("202") and not digits.startswith("201"):
                found_phones.append(re.sub(r'\s+', ' ', p).strip())

        # 5. 国际格式号码兜底匹配 (+86)
        for intl in re.findall(r'\+86[\s\-]?(?:1[3-9]\d[\s\-]?\d{4}[\s\-]?\d{4}|\d{2,4}[\s\-]?\d{7,8})', clean_visible_text):
            found_phones.append(re.sub(r'\s+', ' ', intl).strip())

        # 过滤虚假连号
        clean_results = []
        for ph in found_phones:
            if not any(bad in ph for bad in ["123456", "000000", "888888"]):
                clean_results.append(ph)

        return clean_results

    async def _fetch_url_attempt(self, client: httpx.AsyncClient, target_url: str) -> tuple[int, str]:
        try:
            resp = await client.get(target_url, headers=self.headers, timeout=self.timeout)
            return resp.status_code, resp.text
        except Exception as e:
            return 0, str(e)

    async def _fetch_html_resilient(self, client: httpx.AsyncClient, url: str) -> tuple[int, str, str]:
        candidates = [url]
        if url.startswith("https://"):
            candidates.append("http://" + url[8:])
        elif url.startswith("http://"):
            candidates.append("https://" + url[7:])

        parsed = urlparse(url)
        if parsed.netloc and not parsed.netloc.startswith("www."):
            www_netloc = f"www.{parsed.netloc}"
            candidates.append(parsed._replace(netloc=www_netloc).geturl())

        last_error = "未知错误"
        for cand in dict.fromkeys(candidates):
            code, content = await self._fetch_url_attempt(client, cand)
            if code == 200:
                return 200, content, cand
            elif code != 0:
                last_error = f"HTTP_{code}"
            else:
                last_error = content or "连接超时/异常"

        return 0, "", last_error

    async def enrich_lead(self, client: httpx.AsyncClient, website: str) -> dict:
        result = {"email": "", "phone": "", "icp": "无"}
        if not website or not website.startswith("http"):
            return result

        async with self.semaphore:
            status_code, home_html, final_url_or_err = await self._fetch_html_resilient(client, website)

            if status_code != 200 or not home_html:
                result["icp"] = "网址打不开"
                print(f"      ❌ [独立站打不开] {website} (原因: {final_url_or_err})")
                return result

            # 1. 提取 ICP 备案号
            icp_match = ICP_REGEX.search(home_html)
            if icp_match:
                result["icp"] = re.sub(r'\s+', '', icp_match.group(1))
            else:
                result["icp"] = "无"

            # 2. 挖掘邮箱与联系电话
            raw_emails = self._extract_valid_emails(home_html)
            raw_phones = self._extract_valid_phones(home_html)

            # 穿透 contact / about 页面补全
            contact_links = re.findall(r'href=["\']([^"\']*(?:contact|about)[^"\']*)["\']', home_html, re.I)
            if contact_links:
                sub_url = urljoin(website, contact_links[0])
                if urlparse(sub_url).netloc == urlparse(website).netloc:
                    code, contact_html, _ = await self._fetch_html_resilient(client, sub_url)
                    if code == 200:
                        raw_emails.extend(self._extract_valid_emails(contact_html))
                        raw_phones.extend(self._extract_valid_phones(contact_html))

            if raw_emails:
                priority_emails = [e for e in raw_emails if any(k in e for k in ["sales", "info", "contact", "export", "service"])]
                selected = priority_emails[0] if priority_emails else raw_emails[0]
                result["email"] = selected
                print(f"      📧 [独立站邮箱] 提取成功: {selected} ({website})")

            # 号码指纹排重与格式优选
            clean_phones = deduplicate_phone_list(raw_phones)
            if clean_phones:
                result["phone"] = " / ".join(clean_phones[:3])
                print(f"      📞 [独立站电话] 提取成功(已排重): {result['phone']} ({website})")

            if result["icp"] != "无":
                print(f"      🛡️ [备案信息] 提取到备案号: {result['icp']} ({website})")

        return result


def compact_person_text(name_str: str) -> str:
    if not name_str or not isinstance(name_str, str):
        return ""
    val = re.sub(r'[\s\u00a0\u3000\r\n\t]+', ' ', name_str).strip()
    val = re.sub(r'\b(Mr|Mrs|Ms|Miss|Dr)\s*\.\s*', r'\1. ', val, flags=re.I)
    val = re.sub(r'\b(Mr|Mrs|Ms|Miss|Dr)\s+(?!\.)', r'\1. ', val, flags=re.I)
    return re.sub(r'\s+', ' ', val).strip()


async def run_pipeline(
    keyword: str = "monitor",
    max_count: int = 5,
    output_file: str = "suppliers_leads.xlsx",
    enrich_websites: bool = True
):
    print(f"\n=======================================================")
    print(f"🚀 [Pipeline] 启动自动化寻客与独立站深度挖掘流水线")
    print(f"📌 关键词: {keyword} | 计划采集量: {max_count} | 输出文件: {output_file}")
    print(f"=======================================================")

    leads_data = await scrape_globalsources_suppliers(keyword=keyword, max_count=max_count)
    if not leads_data:
        print("\n💡 未采集到任何有效新增商户，流程结束。")
        return

    # 独立站深度穿透
    if enrich_websites:
        print(f"\n🌐 [Enrichment] 正在异步穿透独立站探测邮箱、联系方式与备案...")
        enricher = WebsiteEnricher(concurrency=5)
        async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
            tasks = [enricher.enrich_lead(client, lead.get("official_website", "")) for lead in leads_data]
            enrich_results = await asyncio.gather(*tasks)

        for lead, enrich_res in zip(leads_data, enrich_results):
            lead["email"] = enrich_res.get("email", "")
            lead["phone"] = enrich_res.get("phone", "")
            lead["icp"] = enrich_res.get("icp", "无")

    print(f"\n🧠 [LLM 质检] 调用 DeepSeek 模型核验法定工商中文名与工厂画像...")
    new_rows = []
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for item in leads_data:
        raw_lead = RawSupplierLead(
            company=item.get("company", ""),
            store_url=item.get("store_url", ""),
            registered_company=item.get("registered_company", ""),
            registered_address=item.get("registered_address", ""),
            official_website=item.get("official_website", ""),
            card_product=keyword
        )

        eval_res = await evaluate_supplier_icp(
            lead=raw_lead,
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
            model=config.MODEL_NAME
        )

        official_site = str(item.get("official_website", "") or "").strip()
        icp_status = item.get("icp", "无") if official_site else "无"

        new_rows.append({
            "平台名称": str(item.get("platform", "Global Sources") or "").strip(),
            "公司英文名": str(item.get("company", "") or "").strip(),
            "平台网址": str(item.get("store_url", "") or "").strip(),
            "公司中文名": eval_res.clean_company_name or str(item.get("registered_company", "") or "").strip(),
            "公司注册地址": str(item.get("registered_address", "") or "").strip(),
            "联系人": "",          # 平台端已停用抓取，留空
            "联系人职位": "",      # 平台端已停用抓取，留空
            "独立站": official_site,
            "网址是否有备案": icp_status,
            "联系方式": str(item.get("phone", "") or "").strip(),  # 独立站智能排重号码
            "搜索的关键词": keyword,
            "email": str(item.get("email", "") or "").strip(),    # 独立站商业邮箱
            "入库时间": current_time
        })

    new_df = pd.DataFrame(new_rows)
    new_df = new_df[TARGET_COLUMNS]

    # 合并历史数据
    if os.path.exists(output_file):
        try:
            old_df = pd.read_excel(output_file, dtype=str)
            column_mapping = {
                "company": "公司英文名",
                "registered_company": "公司中文名",
                "registered_address": "公司注册地址",
                "contact_person": "联系人",
                "contact_title": "联系人职位",
                "official_website": "独立站",
                "phone": "联系方式",
                "card_product": "搜索的关键词",
                "platform": "平台名称",
                "store_url": "平台网址",
                "created_at": "入库时间"
            }
            old_df.rename(columns=column_mapping, inplace=True)
            for col in TARGET_COLUMNS:
                if col not in old_df.columns:
                    old_df[col] = ""

            old_df = old_df[TARGET_COLUMNS]
            merged_df = pd.concat([old_df, new_df], ignore_index=True)
            subset_key = ["平台网址"] if "平台网址" in merged_df.columns else ["公司英文名"]
            merged_df.drop_duplicates(subset=subset_key, keep="last", inplace=True)
            final_df = merged_df[TARGET_COLUMNS]
        except Exception as e:
            print(f"⚠️ 读取历史数据异常，直接覆盖: {e}")
            final_df = new_df
    else:
        final_df = new_df

    # 导出保存
    target_path = output_file
    try:
        if os.path.exists(target_path):
            with open(target_path, "a+"):
                pass
    except PermissionError:
        target_path = f"suppliers_leads_{int(datetime.now().timestamp())}.xlsx"
        print(f"⚠️ [占用警告] 原文件正在被打开，已重定向写入至: {target_path}")

    with pd.ExcelWriter(target_path, engine="openpyxl") as writer:
        final_df.to_excel(writer, index=False, sheet_name="Suppliers")
        ws = writer.sheets["Suppliers"]
        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        for cell in ws[1]:
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

        text_cols = [
            "联系方式", "email", "入库时间", "公司注册地址",
            "平台网址", "独立站", "网址是否有备案"
        ]
        for col_idx, col_name in enumerate(final_df.columns, start=1):
            is_text = col_name in text_cols
            for row_idx in range(2, len(final_df) + 2):
                cell = ws.cell(row=row_idx, column=col_idx)
                if is_text:
                    cell.number_format = "@"
                    cell.data_type = "s"
                    if cell.value is not None:
                        cell.value = str(cell.value)

    print(f"\n🎉 [流水线完成] 报表已按指定字段输出: {target_path}")
    print(f"📈 累计商户总数: {len(final_df)} 条 (本次追加: {len(new_df)} 条)")


if __name__ == "__main__":
    asyncio.run(run_pipeline(keyword="monitor", max_count=3, enrich_websites=True))