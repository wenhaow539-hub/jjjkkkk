import asyncio

from pipeline import run_pipeline
from utils.logger import get_logger

logger = get_logger("run_mvp")

TARGET_PLATFORM = "globalsources"
SEARCH_KEYWORD = "phone"
SCRAPE_LIMIT = 30
OUTPUT_FILE = "suppliers_leads.xlsx"

# 断点续采：True = 读取 checkpoints/ 历史进度，只跑未完成的阶段（中断后续跑不会重复消耗 API / 重新采集）
RESUME = True

if __name__ == "__main__":
    logger.info(f"🔥 [启动流水线] 目标平台: {TARGET_PLATFORM} | 关键词: {SEARCH_KEYWORD} | 计划数: {SCRAPE_LIMIT}")
    asyncio.run(
        run_pipeline(
            keyword=SEARCH_KEYWORD,
            platform=TARGET_PLATFORM,
            max_count=SCRAPE_LIMIT,
            output_file=OUTPUT_FILE,
            enrich_websites=True,
            enrich_tianyancha=True,
            resume=RESUME,
        )
    )
