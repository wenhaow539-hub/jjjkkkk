import asyncio

from pipeline import run_pipeline

TARGET_PLATFORM = "globalsources"
SEARCH_KEYWORD = "phone"
SCRAPE_LIMIT = 120          # ⚠️ **最终入库**的目标家数（不是 GS 采集数）
BATCH_SIZE = 30            # 每批 GS 采多少家；每批查完立即入库并写指纹
OUTPUT_FILE = "suppliers_leads.xlsx"
RESET_PAGES = False        # True = 清掉跨批翻页进度、从第 1 页重扫
# ⚡ 异步预取：开着时，本批的「两家工商补全」会与下一批的「GS 采集 + 独立站 + LLM」
#    **并行**跑（各自独立标签页）。补全大半时间在 3~6s 冷却 / 45~60s 大休眠里等待，
#    浏览器本来就空转，正好用来备下一批 —— 实测每批能省下这几分钟。
#    关掉（False）= 退回严格串行，日志更整齐、浏览器不并发，但慢。
ASYNC_PREFETCH = True

# —— 工商补全数据源 ——
# ⚠️ 2026-09-17 的教训：这里**不传就等于 "tianyancha"**（run_pipeline 的默认值），
#    于是全部商户都给了天眼查、完全不分流 —— 日志里只有一行
#    「🧩 工商补全数据源: 天眼查」，很容易被忽略。
#   hybrid     = 天眼查 / 爱企查 **按家轮流**（每批各分一半，天眼查先跑）← 当前口径
#   tianyancha = 全部天眼查
#   aiqicha    = 全部爱企查
#   both       = 爱企查优先，关键字段仍缺失的再用天眼查兜底
ENRICH_SOURCE = "hybrid"

if __name__ == "__main__":
    print(f"🔥 [启动流水线] 目标平台: {TARGET_PLATFORM} | 关键词: {SEARCH_KEYWORD} | 计划数: {SCRAPE_LIMIT}")
    asyncio.run(
        run_pipeline(
            keyword=SEARCH_KEYWORD,
            platform=TARGET_PLATFORM,
            max_count=SCRAPE_LIMIT,
            output_file=OUTPUT_FILE,
            enrich_websites=True,
            enrich_tianyancha=True,
            enrich_source=ENRICH_SOURCE,
            batch_size=BATCH_SIZE,
            reset_pages=RESET_PAGES,
            async_prefetch=ASYNC_PREFETCH,
        )
    )
