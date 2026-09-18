import asyncio

import config
from pipeline import run_dual_pipeline, run_pipeline

TARGET_PLATFORM = "globalsources"
SEARCH_KEYWORD = "led"
SCRAPE_LIMIT = 120         # ⚠️ **每条流各自**的最终入库目标家数（不是 GS 采集数）
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

# —— 双浏览器并行 ——
# 2 = 一个进程里同时跑两条完整流水线，各用一台 Chrome（9222 直连 / 9223 代理），
#     各持一套天眼查+爱企查账号（= 4 个账号）。总产出翻倍，墙钟时间基本不变。
#     两条流扫**不相交的列表页**（A 扫 1/3/5…、B 扫 2/4/6…），不重复劳动。
# 1 = 退回单浏览器（与改造前完全一致）。
#
# ⚠️ **首次用双浏览器前，必须先给浏览器 B 建立登录态**：
#        python main.py --login-browser B
#    在弹出的窗口里登录天眼查 + 爱企查。没做这一步，B 那条流的工商补全会全线失败
#    （启动时会检测并提示，超时只跳过 B，不影响 A）。
#
# ⚠️ `SCRAPE_LIMIT` 是**每条流各自**的目标，所以 2 台时总入库 ≈ 2 × SCRAPE_LIMIT。
#    想总量 120 就把 SCRAPE_LIMIT 设成 60。
#
# 代理地址在 .env 里配 `BROWSER_B_PROXY`（现在留空 = B 也走本机直连）。
NUM_STREAMS = 2


def _main():
    streams = max(1, min(NUM_STREAMS, len(config.BROWSER_PROFILES)))
    common = dict(
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
    if streams > 1:
        print(f"🔥 [启动流水线] {streams} 台浏览器并行 | 目标平台: {TARGET_PLATFORM} | "
              f"关键词: {SEARCH_KEYWORD} | 每条流计划入库: {SCRAPE_LIMIT} "
              f"（合计约 {SCRAPE_LIMIT * streams}）")
        asyncio.run(run_dual_pipeline(profiles=list(config.BROWSER_PROFILES)[:streams], **common))
    else:
        print(f"🔥 [启动流水线] 单浏览器 | 目标平台: {TARGET_PLATFORM} | "
              f"关键词: {SEARCH_KEYWORD} | 计划数: {SCRAPE_LIMIT}")
        asyncio.run(run_pipeline(**common))


if __name__ == "__main__":
    _main()
