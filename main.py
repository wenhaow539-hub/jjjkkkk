"""项目统一入口：main.py → CrawlRunner → Adapter → Crawlee

四条路径
--------
1) 默认（Adapter + Crawlee）
       python main.py
       python main.py --platform globalsources --keyword phone -n 30
       python main.py --adapter adapters.globalsources:GlobalSourcesAdapter --keyword phone -n 30
       python main.py --adapter ... --cdp-url http://127.0.0.1:9222 --excel leads.xlsx

2) 原有数据处理流程（委派未改动的 pipeline.run_pipeline，含独立站探测 / 天眼查 / AI 质检 / Excel 落盘）
       python main.py --pipeline --keyword phone -n 30
       python main.py --pipeline --output suppliers_leads.xlsx --no-enrich --no-resume

3) 接口发现与模板重放（转发给引擎 CLI，参数不在本文件重复实现）
       python main.py --discover "https://www.globalsources.com/searchList/suppliers?keyWord=phone"
       python main.py --replay .crawlee_storage/api_templates.json --via crawlee --out api_results.jsonl

4) 自检（职责边界 + Crawlee 能力接线）
       python main.py --selfcheck

职责说明：本文件是"编排层/组合根"，允许同时认识引擎与业务模块；
引擎（crawler_engine）与适配器（adapters）之间仍严格遵守各自边界，由 --selfcheck 校验。
注意：run_mvp.py 保留不动，仍可直接运行（走的也是第 2 条路径）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Sequence

from utils.logger import get_logger

logger = get_logger("main")

DEFAULT_PLATFORM = "globalsources"
DEFAULT_KEYWORD = "phone"
DEFAULT_LIMIT = 30
DEFAULT_OUTPUT = "suppliers_leads.xlsx"

ADAPTER_REGISTRY = {
    "globalsources": "adapters.globalsources:GlobalSourcesAdapter",
    "alibaba": "adapters.alibaba:AlibabaAdapter",
    "made-in-china": "adapters.madeinchina:MadeInChinaAdapter",
}
"""平台 -> Adapter 类路径（按平台名直接跑，无需写全类路径）。"""

ENGINE_FORWARD_FLAGS = ("--selfcheck", "--discover", "--replay")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="统一入口：默认走 Adapter + Crawlee；--pipeline 走原有数据处理流程",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py                                  # 默认：GlobalSources Adapter + Crawlee\n"
            "  python main.py --platform alibaba -n 20          # 按平台名选适配器\n"
            "  python main.py --pipeline -n 30                  # 委派 pipeline.run_pipeline()\n"
            "  python main.py --discover <url> --save-templates api_templates.json\n"
            "  python main.py --replay api_templates.json --via fetcher\n"
            "  python main.py --selfcheck\n"
        ),
    )

    target = parser.add_mutually_exclusive_group(required=False)
    target.add_argument("--adapter", default=None, help="Adapter 类路径（默认按 --platform 解析）")
    target.add_argument("--pipeline", action="store_true",
                        help="委派给未改动的 pipeline.run_pipeline()（原有 Excel 产出链路）")
    target.add_argument("--selfcheck", action="store_true", help="运行自检（职责边界 + 能力接线）")
    target.add_argument("--discover", default=None, help="发现目标页的接口（xhr/fetch/graphql/json）")
    target.add_argument("--replay", default=None, help="按模板文件批量调用接口")

    parser.add_argument("--platform", default=None,
                        help=f"平台名（adapter 模式用于选适配器；--pipeline 模式用于选 legacy 爬虫）。默认 {DEFAULT_PLATFORM}")
    parser.add_argument("--keyword", default=DEFAULT_KEYWORD, help=f"检索关键词（默认 {DEFAULT_KEYWORD}）")
    parser.add_argument("-n", "--limit", type=int, default=DEFAULT_LIMIT, help=f"采集上限（默认 {DEFAULT_LIMIT}）")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"--pipeline 模式的 Excel 输出（默认 {DEFAULT_OUTPUT}）")
    parser.add_argument("--excel", default=None, help="adapter 模式把线索导出到该 Excel 文件")

    parser.add_argument("--engine", default="auto",
                        choices=["auto", "http", "soup", "browser", "browser-cdp", "legacy"],
                        help="执行模式（默认 auto：有 --cdp-url 走 browser-cdp，否则 browser）")
    parser.add_argument("--concurrency", type=int, default=None, help="并发数（默认取引擎配置）")
    parser.add_argument("--queue", default=None, help="Request Queue 名称（命名队列可跨运行续跑）")
    parser.add_argument("--cdp-url", default=None, help="附着到已登录的 Chrome，例如 http://127.0.0.1:9222")
    parser.add_argument("--headless", action="store_true", help="无头模式（默认有头，便于人工过验证码）")
    parser.add_argument("--no-robots", action="store_true", help="关闭 robots.txt 检查（请自行确认合规）")
    parser.add_argument("--proxy", action="append", default=None, help="代理 URL，可重复传入")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    # --pipeline 专用
    parser.add_argument("--no-enrich", action="store_true", help="--pipeline：跳过独立站探测与天眼查补全")
    parser.add_argument("--no-resume", action="store_true", help="--pipeline：不使用断点续采")

    # 转发给引擎 CLI 的发现/重放参数
    parser.add_argument("--save-templates", default=None, help="发现完成后保存模板的路径")
    parser.add_argument("--store-sample", action="store_true", help="响应原始样本一并落盘（默认只存结构）")
    parser.add_argument("--via", default="crawlee", choices=["crawlee", "fetcher"], help="重放通路（默认 crawlee）")
    parser.add_argument("--replay-limit", type=int, default=None, help="重放请求数上限")
    parser.add_argument("--out", default=None, help="重放结果写入的 JSONL 路径")
    return parser


def _engine_config(args, *, mode: str | None = None):
    from crawler_engine import EngineConfig

    return EngineConfig.from_env(
        mode=mode or args.engine,
        concurrency=args.concurrency,
        cdp_url=args.cdp_url,
        headless=True if args.headless else None,
        respect_robots=False if args.no_robots else None,
        proxy_urls=tuple(args.proxy) if args.proxy else None,
        queue_name=args.queue,
        log_level=args.log_level,
    )


# --------------------------------------------------------------------------- #
# 路径 1：Adapter + Crawlee
# --------------------------------------------------------------------------- #
async def run_adapter(args) -> int:
    from crawler_engine import CrawlRunner
    from crawler_engine.runner import load_adapter

    adapter_path = args.adapter
    if not adapter_path:
        platform = (args.platform or DEFAULT_PLATFORM).lower()
        adapter_path = ADAPTER_REGISTRY.get(platform)
        if not adapter_path:
            logger.error(f"未知平台 '{platform}'；可用: {sorted(ADAPTER_REGISTRY)}，或显式传 --adapter")
            return 2

    config = _engine_config(args)
    adapter = load_adapter(adapter_path, keyword=args.keyword)
    logger.info(f"🚀 [main] Adapter + Crawlee | {adapter_path} | keyword={args.keyword} | 上限={args.limit}")

    runner = CrawlRunner(config)
    result = await runner.run(adapter)

    print()
    print(f"Adapter : {adapter_path}")
    print(f"结果    : {result.summary()}")

    if args.excel:
        from models import RawSupplierLead

        raw_items = adapter.to_leads() if hasattr(adapter, "to_leads") else (result.items_data or [])
        leads = [item for item in raw_items if isinstance(item, RawSupplierLead)]
        if len(leads) != len(raw_items):
            logger.warning(
                f"⚠️ [main] {len(raw_items) - len(leads)} 条记录字段不符合 RawSupplierLead（缺少 company/store_url 等），"
                f"导出时已跳过；请检查 adapter.parse() 返回的字段名。"
            )
        if not leads:
            logger.warning("⚠️ [main] 没有可导出的线索，跳过 Excel 落盘。")
        else:
            from exporters.excel import export_leads_to_excel  # 组合层使用业务模块（合规）

            export_leads_to_excel(
                leads_data=leads,
                enriched_results=[{"site_phone": "", "tyc_phone": "", "email": "", "icp": "无",
                                   "contact_person": "", "contact_title": ""} for _ in leads],
                eval_results=[None] * len(leads),
                keyword=args.keyword,
                output_file=args.excel,
            )
    for err in result.errors[:10]:
        print(f"  ⚠️ {err}")
    return 0 if result.status != "failed" else 1


# --------------------------------------------------------------------------- #
# 路径 2：原有数据处理流程（不改动 pipeline 模块）
# --------------------------------------------------------------------------- #
async def run_pipeline_entry(args) -> int:
    from pipeline import run_pipeline  # 懒加载；pipeline.py 保持原样未改动

    platform = (args.platform or DEFAULT_PLATFORM).lower()
    logger.info(
        f"🚀 [main] pipeline 模式（委派 run_pipeline）| 平台={platform} | 关键词={args.keyword} | "
        f"上限={args.limit} | 输出={args.output} | 增强={'关' if args.no_enrich else '开'} | "
        f"断点续采={'关' if args.no_resume else '开'}"
    )
    await run_pipeline(
        keyword=args.keyword,
        platform=platform,
        max_count=args.limit,
        output_file=args.output,
        enrich_websites=not args.no_enrich,
        enrich_tianyancha=not args.no_enrich,
        resume=not args.no_resume,
    )
    return 0


# --------------------------------------------------------------------------- #
# 路径 3：转发给引擎 CLI（发现 / 重放 / 自检）
# --------------------------------------------------------------------------- #
def forward_to_engine(args) -> int:
    from crawler_engine.runner import main as engine_main

    argv: list[str] = []
    if args.selfcheck:
        argv.append("--selfcheck")
    if args.discover:
        argv += ["--discover", args.discover]
    if args.replay:
        argv += ["--replay", args.replay]

    if args.save_templates:
        argv += ["--save-templates", args.save_templates]
    if args.store_sample:
        argv.append("--store-sample")
    if args.via and args.via != "crawlee":
        argv += ["--via", args.via]
    if args.replay_limit:
        argv += ["--replay-limit", str(args.replay_limit)]
    if args.out:
        argv += ["--out", args.out]

    if args.cdp_url:
        argv += ["--cdp-url", args.cdp_url]
    if args.headless:
        argv.append("--headless")
    if args.no_robots:
        argv.append("--no-robots")
    if args.concurrency:
        argv += ["--concurrency", str(args.concurrency)]
    if args.queue:
        argv += ["--queue", args.queue]
    if args.engine and args.engine != "auto":
        argv += ["--engine", args.engine]
    for proxy in args.proxy or ():
        argv += ["--proxy", proxy]
    if args.log_level:
        argv += ["--log-level", args.log_level]

    logger.info(f"↪️ [main] 转发引擎 CLI: {' '.join(argv)}")
    return engine_main(argv)


# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if any((args.selfcheck, args.discover, args.replay)):
        return forward_to_engine(args)

    if args.pipeline:
        return asyncio.run(run_pipeline_entry(args))

    # 默认路径：Adapter + Crawlee（对应 main.py → CrawlRunner → Adapter → Crawlee）
    return asyncio.run(run_adapter(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
