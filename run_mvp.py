import asyncio
from scraper import scrape_globalsources_suppliers
from enricher import check_website_filing
from evaluator import analyze_and_screen_lead
from export import append_lead_to_excel
from config import OUTPUT_FILE

TARGET_CRITERIA = """
- 目标品类：LED照明、显示设备、光电制造、3C数码周边。
- 业务特征：具备独立制造能力或工贸一体的出海商。
- 淘汰规则：非目标品类、纯个人中介判定为 False。
"""


async def main():
    SEARCH_KEYWORD = "monitor"
    SCRAPE_LIMIT = 5

    print(f"🚀 启动环球资源深度寻客 Agent（关键词: {SEARCH_KEYWORD}）...")
    sellers = await scrape_globalsources_suppliers(keyword=SEARCH_KEYWORD, max_count=SCRAPE_LIMIT)

    if not sellers:
        print("⚠️ 未能获取到供应商，请确认网络环境或是否被反爬拦截。")
        return

    print(f"\n🔄 进入质检与公安备案检测流水线（共 {len(sellers)} 家深度线索）...\n")

    for seller in sellers:
        comp_name = seller["company"]
        reg_comp = seller.get("registered_company", "")
        reg_addr = seller.get("registered_address", "")
        c_person = seller.get("contact_person", "")
        c_title = seller.get("contact_title", "")
        off_site = seller.get("official_website", "")
        detail_content = seller.get("detail_content", "")

        print(f"🏢 正在处理: {comp_name}")

        # 1. 核心步骤：直接探测独立官网是否存在公安备案
        police_status = "无独立官网"
        if off_site:
            police_status = check_website_filing(off_site)

        # 2. 整合上下文给大模型
        combined_text = f"""
【环球资源官方直出信息】：
- Registered Company: {reg_comp or '页面未直接显示'}
- Company Registration Address: {reg_addr or '页面未直接显示'}
- Contact Person: {c_person or '未直接显示'}
- Contact Title: {c_title or '未直接显示'}
- Official Website: {off_site or '未直接显示'}
- 公安备案情况: {police_status}

【店铺详情页文本】：
{detail_content}
"""

        # 3. LLM 综合评估
        lead = analyze_and_screen_lead(
            company_name=comp_name,
            raw_text=combined_text,
            target_criteria=TARGET_CRITERIA
        )

        # 4. 强制覆写官方爬取真实数据
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

        # 写入公安备案检测结果
        lead.police_record = police_status

        # 5. 追加保存至 Excel
        append_lead_to_excel(lead, OUTPUT_FILE)
        print("-" * 50)

    print(f"\n🎉 全链路完成！已更新表格: {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())