import asyncio
from scraper import scrape_globalsources_suppliers
from enricher import check_website_filing
from evaluator import analyze_and_screen_lead
from export import append_lead_to_excel
from config import OUTPUT_FILE

TARGET_CRITERIA = """
- 目标品类：LED照明、显示器、商业屏幕、电竞周边、3C数码显示设备。
- 业务特征：具备独立制造能力或工贸一体的出海商。
- 淘汰规则：非目标品类、纯个人中介判定为 False。
"""


async def main():
    SEARCH_KEYWORD = "monitor"
    SCRAPE_LIMIT = 10

    print(f"🚀 启动环球资源深度寻客 Agent（关键词: {SEARCH_KEYWORD}）...")
    sellers = await scrape_globalsources_suppliers(keyword=SEARCH_KEYWORD, max_count=SCRAPE_LIMIT)

    if not sellers:
        print("⚠️ 未能获取到供应商，请确认网络环境或是否被反爬拦截。")
        return

    print(f"\n🔄 进入质检、品类提炼与建档流水线（共 {len(sellers)} 家供应商）...\n")

    for seller in sellers:
        comp_name = seller["company"]
        platform = seller.get("platform", "Global Sources")
        reg_comp = seller.get("registered_company", "")
        reg_addr = seller.get("registered_address", "")
        c_person = seller.get("contact_person", "")
        c_title = seller.get("contact_title", "")
        off_site = seller.get("official_website", "")
        raw_prods = seller.get("raw_products", "")
        detail_content = seller.get("detail_content", "")

        print(f"🏢 正在处理: {comp_name}")

        # 1. 探测独立官网备案
        filing_status = "无独立官网"
        if off_site:
            filing_status = check_website_filing(off_site)

        # 2. 整合上下文（包含原始 Product Groups 列表）
        combined_text = f"""
【平台直出企业与联系人信息】：
- 数据来源: {platform}
- Registered Company: {reg_comp or '页面未直接显示'}
- Company Registration Address: {reg_addr or '页面未直接显示'}
- Contact Person: {c_person or '未直接显示'}
- Contact Title: {c_title or '未直接显示'}
- Official Website: {off_site or '未直接显示'}
- 网站备案情况: {filing_status}

【原始英文产品分组列表 (Product Groups)】：
{raw_prods if raw_prods else "未直接获取到明确分组，请参考店铺详情文本推断"}

【店铺详情页其他文本】：
{detail_content}
"""

        # 3. LLM 综合评估与品类中文提炼
        lead = analyze_and_screen_lead(
            company_name=comp_name,
            raw_text=combined_text,
            target_criteria=TARGET_CRITERIA
        )

        # 4. 强制覆写抓取到的硬核信息
        lead.data_source = platform
        if reg_comp and "inquiry" not in reg_comp.lower():
            lead.registered_company = reg_comp
        if reg_addr and "inquiry" not in reg_addr.lower():
            lead.registered_address = reg_addr
        if c_person and "inquiry" not in c_person.lower():
            lead.contact_person = c_person
        if c_title and "inquiry" not in c_title.lower():
            lead.contact_title = c_title
        if off_site:
            lead.official_website = off_site

        lead.police_record = filing_status

        # 5. 追加保存至 Excel
        append_lead_to_excel(lead, OUTPUT_FILE)
        print(f"   🏷️ AI 提炼中文主营品类: {lead.main_products}")
        print("-" * 50)

    print(f"\n🎉 全链路完成！已更新表格: {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())