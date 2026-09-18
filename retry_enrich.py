"""工商补全「缺口重试」—— 给报表里工商字段全空的历史行补数据。

## 为什么需要它

跑完流水线后，表里会留一批**只有 GS 字段、工商字段全空**的行。它们不是"没采到"，
而是**补全没跑成**，代码故意保留了它们（「没查成」≠「查不到」，删掉等于把环境问题
算成数据问题）。典型来源：

- **熔断跳过**：某趟连撞 3 次环境异常（验证码/反爬/`net::ERR_EMPTY_RESPONSE`）→ 中止；
  修了 `_enrich_business` 的熔断语义后，熔断只中止当前趟、不再连坐另一源，但
  **同一趟里被跳过的家**仍然是空的。
- **补全阶段整体失败**：`connect_over_cdp` 超时（浏览器里有卡死标签页）。

这些行的指纹**已经在库**（它们是"保留入库"、不是"被剔除"，所以没被回滚），
因此正常采集永远碰不到它们 —— 只能拿**报表里的中文名**回锅重查。

## 判定口径

「待补全」= `BUSINESS_RESULT_FIELDS` 里**一个都没有真值**（占位符算空）。
与 `pipeline._business_result_empty()` 同口径，避免两边判定不一致。

用法：
    python retry_enrich.py --dry-run          # 先看有多少行、是哪几家
    python retry_enrich.py --limit 10         # 先补 10 家试水
    python retry_enrich.py                    # 全量补
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime

import config
import pipeline as P
from core.browser import ensure_chrome_running
from enrichers.aiqicha import AiQiChaEnricher
from enrichers.tianyancha import TianyanchaEnricher
from utils.logger import get_logger

logger = get_logger("retry_enrich")

# 报表里「公司中文名」列 —— 用名字去查，而不是靠指纹库
COL_NAME = "公司中文名"
COL_URL = "平台网址"
COL_SOURCE = "数据来源"


def _load_targets(xlsx: str, *, limit: int | None, only_missing_source: bool) -> list[dict]:
    """扫出需要补全的行，返回 [{row, name, url}]。**只读，不改表。**"""
    import openpyxl

    if not os.path.exists(xlsx):
        logger.error(f"❌ 报表不存在: {xlsx}")
        return []
    wb = openpyxl.load_workbook(xlsx, read_only=True)
    ws = wb.active
    headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    try:
        i_name, i_url = headers.index(COL_NAME), headers.index(COL_URL)
    except ValueError:
        logger.error(f"❌ 报表缺少必要列（需要「{COL_NAME}」「{COL_URL}」）: {headers}")
        return []
    i_source = headers.index(COL_SOURCE) if COL_SOURCE in headers else None
    # 工商字段列下标（与 pipeline.BUSINESS_RESULT_FIELDS 对齐的**表头名**）
    biz_headers = {
        "注册资本": "registered_capital", "实缴资本": "paid_in_capital",
        "参保人数": "insured_count", "经营状态": "business_status",
        "天眼查联系人": "contact_person", "天眼查联系方式": "tyc_phone",
    }
    biz_idx = {headers.index(h): h for h in biz_headers if h in headers}

    targets: list[dict] = []
    for i, row in enumerate(ws.iter_rows(min_row=2), start=2):
        name = str(row[i_name].value or "").strip()
        if not name:
            continue
        if only_missing_source and i_source is not None:
            if str(row[i_source].value or "").strip():
                continue   # 有「数据来源」= 补全成功过，跳过
        if any(P._real_value(row[k].value) for k in biz_idx):
            continue       # 工商字段有真值 → 不是缺口
        targets.append({"row": i, "name": name,
                        "url": str(row[i_url].value or "").strip()})
    if limit:
        targets = targets[:limit]
    return targets


def _write_back(xlsx: str, results: list[tuple[int, dict]], *, source_label_by_name: dict) -> int:
    """把补全结果写回报表。**只填空，不覆盖已有值。**

    与导出口径一致（见 `exporters/excel.py` 的字段映射），否则重跑一次导出就会
    把这里填的值冲掉。
    """
    import openpyxl

    if not results:
        return 0
    wb = openpyxl.load_workbook(xlsx)
    ws = wb.active
    headers = [c.value for c in ws[1]]

    def col(name: str):
        return headers.index(name) + 1 if name in headers else None

    MAP = {
        "registered_capital": "注册资本",
        "paid_in_capital": "实缴资本",
        "insured_count": "参保人数",
        "business_status": "经营状态",
        "customs_code": "海关注册编码",
        "customs_reg_date": "海关注册日期",
        "contact_person": "天眼查联系人",
        "tyc_phone": "天眼查联系方式",
        "email": "email",
    }
    filled = 0
    for row_no, info in results:
        if not info:
            continue
        touched = False
        for key, header in MAP.items():
            ci = col(header)
            if ci is None:
                continue
            val = P._real_value(info.get(key))
            if not val:
                continue
            cell = ws.cell(row=row_no, column=ci)
            if str(cell.value or "").strip():
                continue          # 已有值 → 不覆盖
            cell.value = val
            cell.number_format = "@"   # 电话/编码保持文本，别被 Excel 变科学计数
            touched = True
        # 地址只在原为空时补（GS 详情页已带地址的情况占多数）
        ci_addr = col("公司注册地址")
        addr = P._real_value(info.get("registered_address") or info.get("reg_address"))
        if ci_addr and addr and not str(ws.cell(row=row_no, column=ci_addr).value or "").strip():
            ws.cell(row=row_no, column=ci_addr).value = addr
            touched = True
        # 「数据来源」只在真的拿到了工商字段时写，避免造出"看似补全过"的假象
        ci_src = col(COL_SOURCE)
        src = source_label_by_name.get(row_no)
        if ci_src and src and touched:
            if not str(ws.cell(row=row_no, column=ci_src).value or "").strip():
                ws.cell(row=row_no, column=ci_src).value = src
        if touched:
            filled += 1

    backup = os.path.join(
        os.path.dirname(os.path.abspath(xlsx)),
        f"{os.path.splitext(os.path.basename(xlsx))[0]}.bak-retryenrich-"
        f"{datetime.now():%Y%m%d_%H%M%S}.xlsx",
    )
    try:
        wb.save(backup)
        logger.info(f"💾 已备份原表 → {backup}")
        wb.save(xlsx)
    except PermissionError:
        logger.error(f"❌ 报表被占用（大概率正被 Excel 打开），未保存。备份留在 {backup}")
        return 0
    return filled


async def _run(xlsx: str, *, limit: int | None, source: str, dry_run: bool,
               captcha_mode: str, captcha_provider: str | None,
               stream_name: str) -> int:
    targets = _load_targets(xlsx, limit=limit, only_missing_source=True)
    if not targets:
        logger.info("✅ 没有需要补全的行（全部已有工商数据）")
        return 0

    logger.info(f"🔧 [缺口重试] 找到 {len(targets)} 行待补全：")
    for t in targets[:20]:
        logger.info(f"   · 第 {t['row']} 行  {t['name']}")
    if len(targets) > 20:
        logger.info(f"   · …另有 {len(targets) - 20} 行")

    if dry_run:
        logger.info("🧪 --dry-run：只列出，不实际补全。")
        return 0

    profile = next((p for p in config.BROWSER_PROFILES if p.name.upper() == stream_name.upper()),
                   config.BROWSER_PROFILES[0] if config.BROWSER_PROFILES else None)
    if profile is None:
        logger.error("❌ 没有可用的浏览器 profile（检查 config.BROWSER_PROFILES）")
        return 2

    port = ensure_chrome_running(
        port=profile.port, platform_name="缺口重试",
        profile_dir=profile.profile_dir, proxy=profile.proxy,
        reuse_nearby=False, owner=profile.name,
    )
    logger.info(f"🔌 附着浏览器 {profile.name} (CDP {port})…")

    from playwright.async_api import async_playwright

    results: list[tuple[int, dict]] = []
    source_label_by_name: dict[int, str] = {}
    ok = 0

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=30000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        try:
            # 两家交替，与流水线同口径（hybrid）
            tyc = TianyanchaEnricher(captcha_mode=captcha_mode, captcha_provider=captcha_provider)
            aiq = AiQiChaEnricher(captcha_mode=captcha_mode, captcha_provider=captcha_provider)
            for idx, t in enumerate(targets, 1):
                use_tyc = (idx % 2 == 1) if source == "hybrid" else (source == "tianyancha")
                enr = tyc if use_tyc else aiq
                label = "天眼查" if use_tyc else "爱企查"
                try:
                    info = await enr.search_and_enrich(page, t["name"])
                except Exception as e:
                    logger.warning(f"   ⚠️ [{idx}/{len(targets)}] {t['name']} 补全异常: {e!r}")
                    continue
                verdict = P._classify_result(info, False)
                if verdict == "ok":
                    ok += 1
                    source_label_by_name[t["row"]] = label
                    logger.info(f"   🏢 [{idx}/{len(targets)}] {t['name']} → 已补全({label})")
                else:
                    logger.info(f"   ✗ [{idx}/{len(targets)}] {t['name']} → "
                                f"{'站内查无此企业' if verdict == 'not_found' else '未取到'}"
                                f"（{info.get('last_error') or '无错误信息'}）")
                results.append((t["row"], info))
                await asyncio.sleep(3.5)
        finally:
            await page.close()

    if not results:
        logger.warning("⚠️ 一条都没查到（浏览器/验证码问题？），报表未改动。")
        return 1
    filled = _write_back(xlsx, results, source_label_by_name=source_label_by_name)
    logger.info(f"🎉 [缺口重试] 完成：{ok}/{len(targets)} 家取得数据，回填 {filled} 行")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="给报表里工商字段全空的行补数据")
    ap.add_argument("--output", default="suppliers_leads.xlsx", help="报表路径")
    ap.add_argument("--limit", type=int, default=None, help="本次最多补多少家（默认全部）")
    ap.add_argument("--source", choices=["hybrid", "tianyancha", "aiqicha"], default="hybrid")
    ap.add_argument("--captcha-mode", choices=["auto", "manual", "off"], default="auto")
    ap.add_argument("--captcha-provider", default=None)
    ap.add_argument("--stream", default="A", help="用哪台浏览器（A/B）")
    ap.add_argument("--dry-run", action="store_true", help="只列出待补全的行，不改动报表")
    args = ap.parse_args(argv)

    return asyncio.run(_run(
        args.output, limit=args.limit, source=args.source, dry_run=args.dry_run,
        captcha_mode=args.captcha_mode, captcha_provider=args.captcha_provider,
        stream_name=args.stream,
    ))


if __name__ == "__main__":
    sys.exit(main())
