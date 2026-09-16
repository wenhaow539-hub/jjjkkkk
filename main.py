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
import os
import sys
from pathlib import Path
from typing import Sequence

from utils.logger import get_logger

logger = get_logger("main")

DEFAULT_PLATFORM = "globalsources"
DEFAULT_KEYWORD = "phone"
DEFAULT_LIMIT = 10
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
            "  python main.py --doctor                            # 密钥/依赖/浏览器体检\n"
            "  python main.py --selfcheck\n"
        ),
    )

    target = parser.add_mutually_exclusive_group(required=False)
    target.add_argument("--adapter", default=None, help="Adapter 类路径（默认按 --platform 解析）")
    target.add_argument("--pipeline", action="store_true",
                        help="委派给未改动的 pipeline.run_pipeline()（原有 Excel 产出链路）")
    target.add_argument("--selfcheck", action="store_true", help="运行自检（职责边界 + 能力接线）")
    target.add_argument("--doctor", action="store_true",
                        help="环境体检：密钥有效性与依赖/浏览器/存储可用性（含真实连通性探测）")
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
    parser.add_argument("--interval", type=float, default=None,
                        help="相邻请求最小间隔（秒）。默认 3.0，并自动服从 robots.txt 声明的 Crawl-delay")
    parser.add_argument("--no-crawl-delay", action="store_true",
                        help="不按 robots.txt 的 Crawl-delay 放慢（默认服从；关闭会显著提高被封风险）")
    parser.add_argument("--proxy", action="append", default=None, help="代理 URL，可重复传入")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    # --pipeline 专用
    parser.add_argument("--no-enrich", action="store_true", help="--pipeline：跳过独立站探测与工商补全")
    parser.add_argument("--no-resume", action="store_true", help="--pipeline：不使用断点续采")
    parser.add_argument("--enrich-source", choices=["aiqicha", "tianyancha", "both"], default="aiqicha",
                        help="--pipeline：工商补全数据源（默认 aiqicha 爱企查；both=爱企查优先+天眼查兜底）")
    parser.add_argument("--captcha-mode", choices=["auto", "manual", "off"], default="auto",
                        help="--pipeline：验证码策略。auto=先自动打码（需 .env 配打码平台）失败转人工；"
                             "manual=仅人工等待（改动前行为）；off=无人值守，命中即跳过该家（默认 auto）")
    parser.add_argument("--captcha-provider", choices=["auto", "ttshitu", "yunma", "none"], default="auto",
                        help="--pipeline：打码平台。auto=按 .env 里哪家凭据齐全自动选（默认）；none=禁用自动打码")
    parser.add_argument("--keep-missing-name", action="store_true",
                        help="--pipeline：保留环球资源未爬到中文工商名的商户（默认重爬一次仍无则剔除/删除）")
    parser.add_argument("--captcha-wait", type=float, default=None,
                        help="--pipeline：人工等待验证码的秒数（默认取 .env 的 CAPTCHA_WAIT_SECONDS，即 240；0=不等待）")
    parser.add_argument("--refresh", action="store_true",
                        help="忽略历史指纹库，强制重采已采集过的公司（默认会跳过并给出提示）")
    parser.add_argument("--all-browser", action="store_true",
                        help="不用两阶段加速：连资料页也走浏览器（默认资料页改走 HTTP，快约 5 倍）")
    parser.add_argument("--pages", type=int, default=None,
                        help="翻页上限（默认 25）。按需翻页：本页没有新公司才继续往后翻，"
                             "凑够 -n 目标家数即停")
    parser.add_argument("--max-empty-pages", type=int, default=None,
                        help="连续多少页没有新公司就停止翻页（默认 5），避免关键词采尽后白跑")

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
        min_request_interval=args.interval,
        respect_crawl_delay=False if args.no_crawl_delay else None,
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
    adapter = load_adapter(adapter_path, keyword=args.keyword, skip_seen=not args.refresh,
                           max_count=args.limit, pages=args.pages,
                           max_empty_pages=args.max_empty_pages,
                           detail_via_http=not args.all_browser)
    logger.info(
        f"🚀 [main] Adapter + Crawlee | {adapter_path} | keyword={args.keyword} | "
        f"上限={args.limit} | 指纹去重={'关(--refresh)' if args.refresh else '开'} | "
        f"翻页=按需（最多 {args.pages or '25'} 页，连续 {args.max_empty_pages or 5} 页无新公司即停） | "
        f"资料页={'走浏览器(--all-browser)' if args.all_browser else '走 HTTP 加速'}"
    )

    runner = CrawlRunner(config)
    result = await runner.run(adapter)

    print()
    print(f"Adapter : {adapter_path}")
    print(f"结果    : {result.summary()}")
    if hasattr(adapter, "_skipped_seen") and adapter._skipped_seen and result.items == 0:
        print(f"提示    : 本次解析到的公司都已在历史指纹库中（共 {adapter._skipped_seen} 家），"
              f"因此没有新增采集。要强制重采请加 --refresh，或换一个 --keyword。")
    if hasattr(adapter, "_empty_streak") and adapter._empty_streak >= getattr(adapter, "max_empty_pages", 5):
        print(f"提示    : 连续 {adapter._empty_streak} 页没有新公司，已停止翻页 —— 该关键词基本采尽，"
              f"建议换关键词（或加 --refresh 重采）。")
    if result.status == "partial":
        print("提示    : 爬取中途异常终止，但已采集的记录仍然有效（详见上方 ❌ 错误行）。")

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
        f"工商数据源={args.enrich_source} | 验证码={args.captcha_mode}({args.captcha_provider}) | "
        f"断点续采={'关' if args.no_resume else '开'}"
    )
    await run_pipeline(
        keyword=args.keyword,
        platform=platform,
        max_count=args.limit,
        output_file=args.output,
        enrich_websites=not args.no_enrich,
        enrich_tianyancha=not args.no_enrich,
        enrich_source=args.enrich_source,
        resume=not args.no_resume,
        captcha_mode=args.captcha_mode,
        captcha_provider=args.captcha_provider,
        captcha_wait=args.captcha_wait,   # None → 由 config.CAPTCHA_WAIT_SECONDS 决定
        drop_missing_name=not args.keep_missing_name,
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
    if args.interval is not None:
        argv += ["--interval", str(args.interval)]
    if args.no_crawl_delay:
        argv.append("--no-crawl-delay")
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
# 体检：密钥有效性 + 依赖 / 浏览器 / 存储可用性
# --------------------------------------------------------------------------- #
def run_doctor(args) -> int:
    """环境体检。重点是把"静默降级"变成显式可见（例如密钥失效导致 LLM 质检全员跳过）。"""
    import httpx

    import config as app_config
    from crawler_engine.architecture import run_checks
    from crawler_engine.capabilities import audit as audit_capabilities

    failures: list[str] = []
    print()
    print("环境体检")

    # 1) 密钥
    env_file = Path(__file__).resolve().parent / ".env"
    print(f"    .env 文件      : {'存在' if env_file.exists() else '不存在（可复制 .env.example）'}")
    print(f"    密钥配置       : {app_config.api_key_status()}")
    if app_config.OPENAI_API_KEY:
        try:
            resp = httpx.get(
                f"{app_config.OPENAI_BASE_URL}/models",
                headers={"Authorization": f"Bearer {app_config.OPENAI_API_KEY}"},
                timeout=20,
            )
            if resp.status_code == 200:
                models = [m.get("id") for m in resp.json().get("data", [])][:3]
                print(f"    密钥连通性     : ✅ 200（可用模型示例 {models}）")
            elif resp.status_code in (401, 403):
                print(f"    密钥连通性     : ❌ {resp.status_code} 密钥无效/已被吊销 → 大模型质检会全员降级")
                failures.append("大模型密钥无效，请到 DeepSeek 控制台重新签发并写入 .env")
            else:
                print(f"    密钥连通性     : ⚠️ HTTP {resp.status_code} {resp.text[:60]}")
        except Exception as e:
            print(f"    密钥连通性     : ⚠️ 无法连通（{type(e).__name__}）")
    else:
        print("    密钥连通性     : ⚠️ 未配置密钥，大模型质检走内置降级（采集与导出不受影响）")

    # 2) 依赖
    try:
        import importlib.metadata as md
        import crawlee

        versions = {p: md.version(p) for p in ("crawlee", "playwright", "httpx", "parsel")}
        print(f"    引擎依赖       : ✅ " + " / ".join(f"{k}={v}" for k, v in versions.items()))
    except Exception as e:
        print(f"    引擎依赖       : ❌ {type(e).__name__}: {e}")
        failures.append("引擎依赖缺失（crawlee/playwright 等）")

    # 3) 浏览器
    ms_playwright = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    chromium_ok = ms_playwright.exists() and any(ms_playwright.glob("chromium-*"))
    chrome_paths = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    chrome_ok = any(Path(p).exists() for p in chrome_paths)
    print(f"    Playwright 内置: {'✅ 已安装' if chromium_ok else '⚠️ 未安装（python -m playwright install chromium）'}")
    print(f"    本机 Chrome    : {'✅ 已安装' if chrome_ok else '⚠️ 未找到（托管模式将退回内置浏览器）'}")

    # 4) CDP 连通性（仅当配置了 cdp_url）
    if args.cdp_url:
        from crawler_engine import cdp_is_reachable

        ok = cdp_is_reachable(args.cdp_url)
        print(f"    CDP {args.cdp_url:<22}: {'✅ 可达' if ok else '❌ 不可达（Chrome 未以调试端口启动？）'}")
        if not ok:
            failures.append(f"CDP 不可达：{args.cdp_url}")
    else:
        print("    CDP            : 未配置（加 --cdp-url http://127.0.0.1:9222 可附着已登录 Chrome）")

    # 5) 引擎自检
    errors, notes = run_checks()
    wired = sum(1 for c in audit_capabilities() if c.wired)
    print(f"    职责边界       : {'✅ 无越界' if not errors else f'❌ {len(errors)} 项越界'}（已登记例外 {len(notes)}）")
    print(f"    能力接线       : {wired}/9")
    if errors:
        failures.append("引擎职责边界越界（运行 python -m crawler_engine --selfcheck 查看）")

    print()
    if failures:
        print(f"体检未通过：{len(failures)} 项")
        for item in failures:
            print(f"  ❌ {item}")
        return 1
    print("体检通过")
    return 0


# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.doctor:
        return run_doctor(args)

    if any((args.selfcheck, args.discover, args.replay)):
        return forward_to_engine(args)

    if args.pipeline:
        return asyncio.run(run_pipeline_entry(args))

    # 默认路径：Adapter + Crawlee（对应 main.py → CrawlRunner → Adapter → Crawlee）
    return asyncio.run(run_adapter(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
