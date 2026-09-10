import asyncio
from datetime import datetime
import os
import re
from urllib.parse import urljoin, urlparse
import httpx
from openpyxl.styles import Alignment, PatternFill
import pandas as pd
from scraper import scrape_globalsources_suppliers

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', re.IGNORECASE)
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.css', '.js', '.ico')
JUNK_EMAIL_DOMAINS = ('wixpress.com', 'sentry.io', 'example.com', 'domain.com', 'google.com', 'myshopify.com')


class WebsiteEnricher:
    """利用 HTTPX 异步探测官网首页与 Contact 页，提取真实商业邮箱"""
    def __init__(self, concurrency: int = 5, timeout: float = 10.0):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
            if len(e_lower) <= 60:
                valid_emails.add(e_lower)
        return list(valid_emails)

    async def _fetch_html(self, client: httpx.AsyncClient, url: str) -> str:
        try:
            resp = await client.get(url, headers=self.headers, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.text
        except Exception:
            pass
        return ""

    async def enrich_lead(self, client: httpx.AsyncClient, website: str) -> dict:
        result = {"email": ""}
        if not website or not website.startswith("http"):
            return result

        async with self.semaphore:
            home_html = await self._fetch_html(client, website)
            emails = self._extract_valid_emails(home_html)

            if not emails and home_html:
                contact_links = re.findall(r'href=["\']([^"\']*(?:contact|about)[^"\']*)["\']', home_html, re.I)
                if contact_links:
                    sub_url = urljoin(website, contact_links[0])
                    if urlparse(sub_url).netloc == urlparse(website).netloc:
                        contact_html = await self._fetch_html(client, sub_url)
                        emails = self._extract_valid_emails(contact_html)

            if emails:
                priority_emails = [e for e in emails if any(k in e for k in ["sales", "info", "contact", "service"])]
                selected = priority_emails[0] if priority_emails else emails[0]
                result["email"] = selected
                print(f"      📧 [官网补全] 挖掘到有效商业邮箱: {selected} ({website})")

        return result


class LeadEvaluator:
    @staticmethod
    def evaluate(item: dict) -> tuple[int, str]:
        score = 0
        if item.get("email"):
            score += 30
        if item.get("phone"):
            score += 25
        if item.get("official_website"):
            score += 15
        if item.get("registered_company"):
            score += 15
        if item.get("contact_person"):
            score += 10
        if item.get("registered_address"):
            score += 5

        if score >= 80:
            level = "S (极高价值)"
        elif score >= 60:
            level = "A (优质线索)"
        elif score >= 40:
            level = "B (普通线索)"
        else:
            level = "C (缺乏触点)"

        return score, level


def compact_person_text(name_str: str) -> str:
    """压缩历史数据中多余的空格与跳格"""
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
    print(f"🚀 [Pipeline] 启动自动化线索采集流水线")
    print(f"📌 关键词: {keyword} | 计划采集量: {max_count} | 目标输出: {output_file}")
    print(f"=======================================================")

    leads_data = await scrape_globalsources_suppliers(keyword=keyword, max_count=max_count)
    if not leads_data:
        print("\n💡 未采集到任何新增有效商户，全流程结束。")
        return

    if enrich_websites:
        print(f"\n🌐 [Enrichment] 正在异步探测官网外链补全商业邮箱...")
        enricher = WebsiteEnricher(concurrency=5, timeout=10.0)
        async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
            tasks = [enricher.enrich_lead(client, lead.get("official_website", "")) for lead in leads_data]
            enrich_results = await asyncio.gather(*tasks)

        for lead, enrich_res in zip(leads_data, enrich_results):
            lead["email"] = enrich_res.get("email", "")

    print(f"\n📊 [Evaluation] 正在进行线索清洗与多维打分...")
    new_rows = []
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for item in leads_data:
        score, level = LeadEvaluator.evaluate(item)
        new_rows.append({
            "company": str(item.get("company", "") or "").strip(),
            "registered_company": str(item.get("registered_company", "") or "").strip(),
            "contact_person": compact_person_text(str(item.get("contact_person", "") or "")),
            "contact_title": str(item.get("contact_title", "") or "").strip(),
            "official_website": str(item.get("official_website", "") or "").strip(),
            "phone": str(item.get("phone", "") or "").strip(),
            "email": str(item.get("email", "") or "").strip(),
            "lead_score": score,
            "lead_level": level,
            "registered_address": str(item.get("registered_address", "") or "").strip(),
            "card_product": str(item.get("card_product", "") or "").strip(),
            "platform": str(item.get("platform", "Global Sources") or "").strip(),
            "store_url": str(item.get("store_url", "") or "").strip(),
            "created_at": current_time
        })

    new_df = pd.DataFrame(new_rows)

    # -------------------------------------------------------------
    # 历史 Excel 清洗与时间正序重构（彻底解决上方插入和混乱空行问题）
    # -------------------------------------------------------------
    if os.path.exists(output_file):
        try:
            old_df = pd.read_excel(output_file, dtype=str)
            # 清理历史废弃字段
            for drop_col in ["raw_products", "detail_content"]:
                if drop_col in old_df.columns:
                    old_df.drop(columns=[drop_col], inplace=True)

            # 剔除无效空行
            if "company" in old_df.columns:
                old_df = old_df[old_df["company"].astype(str).str.strip().str.len() > 2]

            # 批量压缩历史数据中的松散联系人姓名
            if "contact_person" in old_df.columns:
                old_df["contact_person"] = old_df["contact_person"].apply(compact_person_text)

            # 合并本次采集的新增行
            merged_df = pd.concat([old_df, new_df], ignore_index=True)

            # 店铺防重处理
            subset_key = ["store_url"] if "store_url" in merged_df.columns else ["company"]
            merged_df.drop_duplicates(subset=subset_key, keep="first", inplace=True)

            # 核心：按采集时间从小到大（时间正序）排列，保证新抓取的记录永远排在最下方
            if "created_at" in merged_df.columns:
                merged_df["_dt_sort"] = pd.to_datetime(merged_df["created_at"], errors="coerce")
                merged_df.sort_values(by="_dt_sort", ascending=True, inplace=True)
                merged_df.drop(columns=["_dt_sort"], inplace=True)

            final_df = merged_df
            print(f"📂 历史数据同步完毕: 原有 {len(old_df)} 条，本次末尾追加 {len(new_df)} 条，总计: {len(final_df)} 条。")
        except Exception as e:
            print(f"⚠️ 读取旧文件异常，将以本次新增直接覆盖: {e}")
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
        timestamp_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_path = f"suppliers_leads_{timestamp_suffix}.xlsx"
        print(f"⚠️ [文件占用警告] {output_file} 正被打开！已重定向安全保存至: {target_path}")

    with pd.ExcelWriter(target_path, engine="openpyxl") as writer:
        final_df.to_excel(writer, index=False, sheet_name="Suppliers")
        ws = writer.sheets["Suppliers"]

        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        for cell in ws[1]:
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

        text_cols = ["phone", "email", "created_at", "registered_address", "store_url", "official_website"]
        for col_idx, col_name in enumerate(final_df.columns, start=1):
            is_text = col_name in text_cols
            for row_idx in range(2, len(final_df) + 2):
                cell = ws.cell(row=row_idx, column=col_idx)
                if is_text:
                    cell.number_format = "@"
                    cell.data_type = "s"
                    if cell.value is not None:
                        cell.value = str(cell.value)

    print(f"\n🎉 [Pipeline 全部就绪] 数据报表已成功导出: {target_path}")
    print(f"📈 累计总记录: {len(final_df)} 条 (本次追加: {len(new_df)} 条)")


if __name__ == "__main__":
    asyncio.run(run_pipeline(keyword="monitor", max_count=3, enrich_websites=True))