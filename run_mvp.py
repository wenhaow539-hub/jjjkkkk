import asyncio
from pipeline import run_pipeline

# 核心采集配置
SEARCH_KEYWORD = "monitor"    # 搜索品类关键词
SCRAPE_LIMIT = 5              # 本次计划抓取商家数量
OUTPUT_FILE = "suppliers_leads.xlsx"

if __name__ == "__main__":
    print(f"🔥 [启动寻客 Pipeline] 目标关键词: {SEARCH_KEYWORD} | 计划采集数: {SCRAPE_LIMIT}")
    asyncio.run(
        run_pipeline(
            keyword=SEARCH_KEYWORD,
            max_count=SCRAPE_LIMIT,
            output_file=OUTPUT_FILE
        )
    )