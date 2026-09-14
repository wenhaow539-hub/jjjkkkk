import asyncio
import re
import httpx
from playwright.async_api import async_playwright

import config
from core.browser import ensure_chrome_running
from core.factory import CrawlerFactory
import crawlers  # 激活各平台爬虫注册
from enrichers.evaluator import evaluate_supplier_icp
from enrichers.tianyancha import TianyanchaEnricher
from enrichers.website import WebsiteEnricher
from exporters.excel import export_leads_to_excel
from models import RawSupplierLead

async def run_pipeline(
    keyword: str = "monitor",
    platform: str = "globalsources",
    max_count: int = 5,
    output_file: str = "suppliers_leads.xlsx",
    enrich_websites: bool = True,
    enrich_tianyancha: bool = True
):
    print(f"\n=======================================================")
    print(f"🚀 [Pipeline] 启动自动化多平台采集流水线")
    print(f"🌐 目标平台: {platform} | 关键词: {keyword} | 计划采集: {max_count}")
    print(f"=======================================================")

    # 1. 爬取商户初筛数据
    crawler = CrawlerFactory.get_crawler(platform)
    leads_data: list[RawSupplierLead] = await crawler.scrape(keyword=keyword, max_count=max_count)

    if not leads_data:
        print("\n💡 未采集到任何有效商户，流程结束。")
        return

    enriched_results = [
        {"site_phone": "", "tyc_phone": "", "email": "", "icp": "无", "contact_person": "", "contact_title": ""}
        for _ in leads_data
    ]

    # 2. 独立站穿透探测
    if enrich_websites:
        print(f"\n🌐 [Enrichment 1/2] 异步穿透独立站探测商业邮箱、官网联系方式与备案...")
        enricher = WebsiteEnricher(concurrency=5)
        async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
            tasks = [enricher.enrich_lead(client, lead.official_website) for lead in leads_data]
            site_results = await asyncio.gather(*tasks)

        for i, s_res in enumerate(site_results):
            enriched_results[i]["email"] = s_res.get("email", "")
            enriched_results[i]["site_phone"] = s_res.get("site_phone", "")
            enriched_results[i]["icp"] = s_res.get("icp", "无")

    # 3. 大模型工商质检
    print(f"\n🧠 [LLM 质检] 调用 DeepSeek 模型规范工商全称...")
    eval_results = []
    for idx, lead in enumerate(leads_data, 1):
        try:
            eval_res = await evaluate_supplier_icp(
                lead=lead,
                api_key=config.OPENAI_API_KEY,
                base_url=config.OPENAI_BASE_URL,
                model=config.MODEL_NAME
            )
            display_name = eval_res.clean_company_name or '无官方中文名/离岸主体'
            print(f"      ✨ [{idx}/{len(leads_data)}] 质检提纯: {lead.company} -> {display_name}")
        except Exception as e:
            print(f"      ⚠️ [{idx}/{len(leads_data)}] 质检跳过异常: {lead.company} ({e})")
            class FallbackEval:
                clean_company_name = lead.registered_company or ""
            eval_res = FallbackEval()

        eval_results.append(eval_res)

    # 4. 天眼查自动化触点补全
    if enrich_tianyancha:
        needing_indices = []
        for i, (lead, enrich_res, eval_res) in enumerate(zip(leads_data, enriched_results, eval_results)):
            target = (getattr(eval_res, "clean_company_name", "") or lead.registered_company or "").strip()
            has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', target))
            if has_chinese and (not enrich_res.get("site_phone") or not lead.registered_address):
                needing_indices.append(i)

        if needing_indices:
            print(f"\n🏢 [Enrichment 2/2] 启动天眼查检索 (共 {len(needing_indices)} 家商户需补充天眼查联系方式/地址)...")
            ensure_chrome_running(port=crawler.cdp_port, platform_name="Tianyancha")
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{crawler.cdp_port}")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                tyc_page = await context.new_page()
                tyc_enricher = TianyanchaEnricher()

                try:
                    for idx in needing_indices:
                        lead = leads_data[idx]
                        eval_res = eval_results[idx]
                        search_target = getattr(eval_res, "clean_company_name", "") or lead.registered_company

                        tyc_info = await tyc_enricher.search_and_enrich(tyc_page, search_target)

                        if tyc_info.get("phone"):
                            enriched_results[idx]["tyc_phone"] = tyc_info["phone"]
                        if not enriched_results[idx].get("email") and tyc_info.get("email"):
                            enriched_results[idx]["email"] = tyc_info["email"]
                        if tyc_info.get("contact_person"):
                            enriched_results[idx]["contact_person"] = tyc_info["contact_person"]
                            enriched_results[idx]["contact_title"] = tyc_info.get("contact_title", "法定代表人")
                        if tyc_info.get("registered_company"):
                            enriched_results[idx]["registered_company"] = tyc_info["registered_company"]
                        if not lead.registered_address and tyc_info.get("registered_address"):
                            lead.registered_address = tyc_info["registered_address"]

                        await asyncio.sleep(1.0)
                finally:
                    await tyc_page.close()
        else:
            print(f"\n🏢 [Enrichment 2/2] 无需天眼查补全或商户无大陆主体，安全跳过。")

    # 5. 落盘报表
    export_leads_to_excel(
        leads_data=leads_data,
        enriched_results=enriched_results,
        eval_results=eval_results,
        keyword=keyword,
        output_file=output_file
    )

if __name__ == "__main__":
    asyncio.run(run_pipeline(keyword="monitor", max_count=3, enrich_websites=True, enrich_tianyancha=True))