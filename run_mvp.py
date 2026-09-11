import asyncio
from pipeline import run_pipeline

# 核心采集运行配置
SEARCH_KEYWORD = "packaging"   # 采购品类关键词，例如: led, charger, apple, packaging
SCRAPE_LIMIT = 32             # 计划获取商户量
OUTPUT_FILE = "suppliers_leads.xlsx"

if __name__ == "__main__":
    print(f"🔥 [启动寻客流水线] 目标品类: {SEARCH_KEYWORD} | 计划采集数: {SCRAPE_LIMIT}")
    asyncio.run(
        run_pipeline(
            keyword=SEARCH_KEYWORD,
            max_count=SCRAPE_LIMIT,
            output_file=OUTPUT_FILE,
            enrich_websites=True
        )
    )