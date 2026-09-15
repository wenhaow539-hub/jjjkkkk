import asyncio
from pipeline import run_pipeline

TARGET_PLATFORM = "globalsources"
SEARCH_KEYWORD = "bag"
SCRAPE_LIMIT = 5
OUTPUT_FILE = "suppliers_leads.xlsx"

if __name__ == "__main__":
    print(f"🔥 [启动流水线] 目标平台: {TARGET_PLATFORM} | 关键词: {SEARCH_KEYWORD} | 计划数: {SCRAPE_LIMIT}")
    asyncio.run(
        run_pipeline(
            keyword=SEARCH_KEYWORD,
            platform=TARGET_PLATFORM,
            max_count=SCRAPE_LIMIT,
            output_file=OUTPUT_FILE,
            enrich_websites=True,
            enrich_tianyancha=True
        )
    )