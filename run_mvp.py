import asyncio
from scraper import scrape_alibaba_suppliers
from enricher import search_company_info
from evaluator import analyze_and_screen_lead
from export import append_lead_to_excel
from config import OUTPUT_FILE

TARGET_CRITERIA = """
- 目标品类：3C数码周边、无线充/快充设备、智能硬件或穿戴制造。
- 业务特征：具备外贸出海实力的工贸一体企业、工厂或品牌出海商。
- 淘汰规则：纯个人代购、杂货铺、无生产研发能力的贸易中介判定为 False。
"""


async def main():
    SEARCH_KEYWORD = "wireless charger manufacturer"
    SCRAPE_LIMIT = 5

    print(f"🚀 开始从阿里巴巴国际站抓取【{SEARCH_KEYWORD}】前台供应商...")
    sellers = await scrape_alibaba_suppliers(keyword=SEARCH_KEYWORD, max_count=SCRAPE_LIMIT)

    if not sellers:
        print("⚠️ 未能获取到供应商，请确认网络环境或是否被反爬拦截。")
        return

    print(f"\n🔄 进入反查、质检与建档流水线（共 {len(sellers)} 条有效线索）...\n")

    for seller in sellers:
        comp_name = seller["company"]
        card_product = seller.get("card_product", "")
        print(f"🔍 正在处理供应商: {comp_name}")

        # 步骤 1: 外部反查
        bg_info = search_company_info(comp_name)

        # 融合平台页面产品信息作为上下文保底
        combined_text = f"平台展示核心产品: {card_product}\n外部反查信息: {bg_info}"

        # 步骤 2: LLM 质检打分
        lead = analyze_and_screen_lead(
            company_name=comp_name,
            raw_text=combined_text,
            target_criteria=TARGET_CRITERIA
        )

        # 步骤 3: 写入 Excel
        append_lead_to_excel(lead, OUTPUT_FILE)
        print("-" * 50)

    print(f"\n🎉 全链路闭环完成！线索已追加保存在: {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())