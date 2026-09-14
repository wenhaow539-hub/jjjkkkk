import asyncio
from datetime import datetime
import re

import httpx
from playwright.async_api import async_playwright

import config
from core.browser import ensure_chrome_running
from core.factory import CrawlerFactory
import crawlers  # 激活各平台爬虫注册
from enrichers.evaluator import EvaluatedSupplier, evaluate_supplier_icp
from enrichers.tianyancha import TianyanchaEnricher
from enrichers.website import WebsiteEnricher
from exporters.excel import export_leads_to_excel
from models import RawSupplierLead
from utils.logger import get_logger

logger = get_logger("pipeline")


def _default_eval(lead: RawSupplierLead) -> EvaluatedSupplier:
    return EvaluatedSupplier(
        clean_company_name=lead.registered_company or "",
        is_factory=True,
        confidence_score=0.8,
        summary="未执行大模型质检或降级回退",
    )


def _new_record(lead: RawSupplierLead) -> dict:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "store_url": lead.store_url,
        "company": lead.company,
        "platform": lead.platform,
        "lead": lead.model_dump(),
        "website_enriched": False,
        "enrich": {
            "site_phone": "",
            "tyc_phone": "",
            "email": "",
            "icp": "无",
            "contact_person": "",
            "contact_title": "",
            "registered_capital": "",
            "paid_in_capital": "",
            "insured_count": "",
            "created_at": now_str,
        },
        "llm_evaluated": False,
        "eval": None,
        "tyc_enriched": False,
    }


def _record_key(store_url: str, company: str = "") -> str:
    return (store_url or company or "").strip().rstrip("/")


async def run_pipeline(
    keyword: str = "monitor",
    platform: str = "globalsources",
    max_count: int = 5,
    output_file: str = "suppliers_leads.xlsx",
    enrich_websites: bool = True,
    enrich_tianyancha: bool = True,
):
    logger.info("=======================================================")
    logger.info("🚀 [Pipeline] 启动自动化多平台采集流水线")
    logger.info(f"🌐 目标平台: {platform} | 关键词: {keyword} | 本次计划采集: {max_count}")
    logger.info("=======================================================")

    # 1. 爬取商户初筛数据（只处理本次抓取到的商户，不加载任何历史断点）
    crawler = CrawlerFactory.get_crawler(platform)
    fresh_leads: list[RawSupplierLead] = await crawler.scrape(keyword=keyword, max_count=max_count)

    if not fresh_leads:
        logger.info("💡 未采集到任何有效商户，流程结束。")
        return

    records: dict = {}
    for lead in fresh_leads:
        key = _record_key(lead.store_url, lead.company)
        if key not in records:
            records[key] = _new_record(lead)

    # 2. 独立站穿透探测
    if enrich_websites:
        pending = list(records.values())
        logger.info(f"\n🌐 [Enrichment 1/2] 异步穿透独立站探测商业邮箱、官网联系方式与备案... (本次处理 {len(pending)} 家)")
        enricher = WebsiteEnricher(concurrency=5)
        async with httpx.AsyncClient(follow_redirects=True) as client:

            async def enrich_one(rec: dict):
                lead = RawSupplierLead(**rec["lead"])
                try:
                    s_res = await enricher.enrich_lead(client, lead.official_website)
                except Exception as e:
                    logger.warning(f"      ⚠️ [独立站探测异常] {lead.company} ({e!r})")
                    s_res = {"email": "", "site_phone": "", "icp": "网址探测异常"}
                rec["enrich"]["email"] = s_res.get("email", "")
                rec["enrich"]["site_phone"] = s_res.get("site_phone", "")
                rec["enrich"]["icp"] = s_res.get("icp", "无")
                rec["website_enriched"] = True

            await asyncio.gather(*[enrich_one(rec) for rec in pending])

    # 3. 大模型工商质检
    eval_pending = list(records.values())
    logger.info(f"\n🧠 [LLM 质检] 调用 DeepSeek 模型规范工商全称... (本次处理 {len(eval_pending)} 家)")
    for idx, rec in enumerate(eval_pending, 1):
        lead = RawSupplierLead(**rec["lead"])
        try:
            eval_res = await evaluate_supplier_icp(
                lead=lead,
                api_key=config.OPENAI_API_KEY,
                base_url=config.OPENAI_BASE_URL,
                model=config.MODEL_NAME,
            )
        except Exception as e:
            logger.warning(f"      ⚠️ [{idx}/{len(eval_pending)}] 质检跳过异常: {lead.company} ({e})")
            eval_res = _default_eval(lead)
        display_name = eval_res.clean_company_name or "无官方中文名/离岸主体"
        logger.info(f"      ✨ [{idx}/{len(eval_pending)}] 质检提纯: {lead.company} -> {display_name}")
        rec["eval"] = eval_res.model_dump()
        rec["llm_evaluated"] = True

    # 4. 天眼查自动化触点与工商数据补全
    if enrich_tianyancha:
        needing = []
        for rec in records.values():
            lead = RawSupplierLead(**rec["lead"])
            eval_data = rec.get("eval") or {}
            target = (eval_data.get("clean_company_name", "") or lead.registered_company or "").strip()
            has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', target))

            if has_chinese:
                needing.append(rec)

        if not needing:
            logger.info("\n🏢 [Enrichment 2/2] 商户无大陆主体，安全跳过天眼查。")
        else:
            logger.info(f"\n🏢 [Enrichment 2/2] 启动天眼查检索 (共 {len(needing)} 家企业全量补全资本、人数及触点)...")
            ensure_chrome_running(port=crawler.cdp_port, platform_name="Tianyancha")
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{crawler.cdp_port}")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                tyc_page = await context.new_page()
                tyc_enricher = TianyanchaEnricher()

                try:
                    for rec in needing:
                        lead = RawSupplierLead(**rec["lead"])
                        eval_data = rec.get("eval") or {}
                        search_target = (eval_data.get("clean_company_name", "") or lead.registered_company).strip()

                        tyc_info = await tyc_enricher.search_and_enrich(tyc_page, search_target)

                        if tyc_info.get("phone"):
                            rec["enrich"]["tyc_phone"] = tyc_info["phone"]
                        if not rec["enrich"].get("email") and tyc_info.get("email"):
                            rec["enrich"]["email"] = tyc_info["email"]
                        if tyc_info.get("contact_person"):
                            rec["enrich"]["contact_person"] = tyc_info["contact_person"]
                            rec["enrich"]["contact_title"] = tyc_info.get("contact_title", "法定代表人")
                        if tyc_info.get("registered_company"):
                            rec["enrich"]["registered_company"] = tyc_info["registered_company"]
                        if not lead.registered_address and tyc_info.get("registered_address"):
                            lead.registered_address = tyc_info["registered_address"]
                            rec["lead"] = lead.model_dump()

                        if tyc_info.get("registered_capital"):
                            rec["enrich"]["registered_capital"] = tyc_info["registered_capital"]
                        if tyc_info.get("paid_in_capital"):
                            rec["enrich"]["paid_in_capital"] = tyc_info["paid_in_capital"]
                        if tyc_info.get("insured_count"):
                            rec["enrich"]["insured_count"] = tyc_info["insured_count"]

                        rec["tyc_enriched"] = True
                        await asyncio.sleep(0.5)
                finally:
                    await tyc_page.close()

    # 5. 落盘报表导出
    leads_data = [RawSupplierLead(**rec["lead"]) for rec in records.values()]
    enriched_results = [rec["enrich"] for rec in records.values()]
    eval_results = [
        EvaluatedSupplier(**rec["eval"]) if rec.get("eval") else _default_eval(RawSupplierLead(**rec["lead"]))
        for rec in records.values()
    ]
    export_leads_to_excel(
        leads_data=leads_data,
        enriched_results=enriched_results,
        eval_results=eval_results,
        keyword=keyword,
        output_file=output_file,
    )


if __name__ == "__main__":
    asyncio.run(run_pipeline(keyword="monitor", max_count=3, enrich_websites=True, enrich_tianyancha=True))