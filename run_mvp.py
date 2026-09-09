import asyncio
from scraper import scrape_globalsources_suppliers
from enricher import check_website_filing
from evaluator import analyze_and_screen_lead
from export import append_lead_to_excel
from config import OUTPUT_FILE

async def main():
    SEARCH_KEYWORD = "apple"
    SCRAPE_LIMIT = 5

    print(f"🚀 启动环球资源寻客 Agent（关键词: {SEARCH_KEYWORD}）...")
    sellers = await scrape_globalsources_suppliers(keyword=SEARCH_KEYWORD, max_count=SCRAPE_LIMIT)

    if not sellers:
        print("⚠️ 未能获取到供应商，请确认网络环境或是否被反爬拦截。")
        return

    print(f"\n🔄 进入信息汇总与品类提炼流水线（共 {len(sellers)} 家供应商）...\n")

    for seller in sellers:
        comp_name = seller["company"]
        platform = seller.get("platform", "Global Sources")
        reg_comp = seller.get("registered_company", "")
        reg_addr = seller.get("registered_address", "")
        c_person = seller.get("contact_person", "")
        c_title = seller.get("contact_title", "")
        phone = seller.get("phone", "")
        off_site = seller.get("official_website", "")
        raw_prods = seller.get("raw_products", "")
        detail_content = seller.get("detail_content", "")

        print(f"🏢 正在处理: {comp_name}")

        # 1. 独立官网备案检测
        filing_status = "无独立官网"
        if off_site:
            filing_status = check_website_filing(off_site)

        # 2. 构建上下文
        combined_text = f"""
【平台企业与联系人信息】：
- 供应商: {comp_name}
- 法定注册公司: {reg_comp}
- 原始英文产品分组: {raw_prods}
- 详情文本: {detail_content[:1500]}
"""

        # 3. LLM 提炼中文主营品类
        lead = analyze_and_screen_lead(
            company_name=comp_name,
            raw_text=combined_text
        )

        # 4. 组装最终字段
        lead.data_source = platform
        if reg_comp and "inquiry" not in reg_comp.lower():
            lead.registered_company = reg_comp
        if reg_addr and "inquiry" not in reg_addr.lower():
            lead.registered_address = reg_addr
        if c_person and "inquiry" not in c_person.lower():
            lead.contact_person = c_person
        if c_title and "inquiry" not in c_title.lower():
            lead.contact_title = c_title
        if phone:
            lead.phone = phone
        if off_site:
            lead.official_website = off_site
        lead.police_record = filing_status

        # 5. 追加写入 Excel
        append_lead_to_excel(lead, OUTPUT_FILE)
        print("-" * 50)

    print(f"\n🎉 全链路完成！已更新表格: {OUTPUT_FILE}")

if __name__ == "__main__":
    asyncio.run(main())