"""「环球资源没爬到中文工商名 → 按平台网址重爬 → 仍无则删除」的离线回归。

全部用替身，**不发真实请求、不碰用户的 suppliers_leads.xlsx**（用 tmp 目录）。

覆盖的四层：
  A. _parse_detail      中文名三级兜底：profile → contact → **平台网址原始页**
  B. refetch_company_names  按 URL 批量回锅（只认 http、去重、异常不炸）
  C. _existing_rows_missing_name / _backfill_company_names  存量报表的定位与回填
  D. export_leads_to_excel(drop_urls=...)  删除生效，且"本轮零新增"时不报错

跑法：`python tests/test_missing_name_policy.py`
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from crawlers.globalsources import GlobalSources  # noqa: E402
from exporters.excel import TARGET_COLUMNS, export_leads_to_excel  # noqa: E402
from models import RawSupplierLead  # noqa: E402
import pipeline  # noqa: E402

# 详情页里"带 Registered Company Name 标签"的样本，够 _extract_field_from_text 命中即可
PAGE_WITH_NAME = """
<html><body>
<div class="profile-label">Registered Company Name:</div>
<div class="profile-value">东莞市诚远箱包有限公司</div>
<div class="profile-label">Business Type:</div>
</body></html>
"""
PAGE_WITHOUT_NAME = "<html><body><div>No such field here</div></body></html>"

_FAILED = []


def check(cond, desc):
    print(f"  {'OK  ' if cond else 'FAIL'} {desc}")
    if not cond:
        _FAILED.append(desc)
    return cond


# --------------------------------------------------------------------------- #
# A. 中文名兜底：profile/contact 都没取到时，直接用平台网址原始页再解析
# --------------------------------------------------------------------------- #
def test_parse_detail_falls_back_to_store_url():
    print("\n[A] _parse_detail 中文名兜底（profile → contact → 平台网址原始页）")

    async def run(pages: dict):
        """pages: url → html。没配置的 URL 返回空（模拟 404/403）。"""
        gs = GlobalSources()

        async def fake_fetch(client, url, retries=2):
            return pages.get(url, "")

        gs._fetch_html = fake_fetch
        return await gs._parse_detail(client=None, store_url="https://www.globalsources.com/si/123.html")

    store = "https://www.globalsources.com/si/123.html"
    profile = "https://www.globalsources.com/company-profile_123.htm"
    contact = "https://www.globalsources.com/contact-us_123.htm"

    # A1. 只有平台网址页有中文名 → 必须取到（这是本次新增的兜底）
    info = asyncio.run(run({store: PAGE_WITH_NAME, profile: PAGE_WITHOUT_NAME, contact: PAGE_WITHOUT_NAME}))
    check(info["registered_company"] == "东莞市诚远箱包有限公司",
          f"A1 profile/contact 无、平台网址页有 → 取到 {info['registered_company']!r}")

    # A2. 三级都没有 → 空（不能编造）
    info = asyncio.run(run({store: PAGE_WITHOUT_NAME, profile: PAGE_WITHOUT_NAME, contact: PAGE_WITHOUT_NAME}))
    check(info["registered_company"] == "", "A2 三级全无 → 空，不编造")

    # A3. profile 有 → 走主路径，不该被 home 覆盖
    info = asyncio.run(run({profile: PAGE_WITH_NAME, contact: PAGE_WITHOUT_NAME, store: PAGE_WITHOUT_NAME}))
    check(info["registered_company"] == "东莞市诚远箱包有限公司", "A3 profile 有 → 主路径取到")

    # A4. profile/contact 全部 404（空）→ 兜底抓平台网址页
    info = asyncio.run(run({store: PAGE_WITH_NAME}))
    check(info["registered_company"] == "东莞市诚远箱包有限公司",
          "A4 profile/contact 全 404，仅平台网址页可用 → 取到")


# --------------------------------------------------------------------------- #
# B. refetch_company_names：按平台网址批量回锅
# --------------------------------------------------------------------------- #
def test_refetch_company_names():
    print("\n[B] refetch_company_names 按平台网址回锅")
    gs = GlobalSources()
    u1 = "https://www.globalsources.com/si/1.html"
    u2 = "https://www.globalsources.com/si/2.html"

    async def fake_parse(client, store_url):
        return {"registered_company": "广州市金五环城市箱包制品有限公司" if store_url == u1 else "",
                "registered_address": "", "official_website": "", "raw_products": ""}

    gs._parse_detail = fake_parse
    out = asyncio.run(gs.refetch_company_names([u1, u2]))
    check(out.get(u1) == "广州市金五环城市箱包制品有限公司", f"B1 有中文名的 URL → {out.get(u1)!r}")
    check(out.get(u2) == "", "B2 取不到的 URL → 空串（调用方据此判定删除）")

    check(asyncio.run(gs.refetch_company_names([])) == {}, "B3 空列表 → {}，不发请求")
    check(asyncio.run(gs.refetch_company_names(["not-a-url", ""])) == {}, "B4 非 http / 空 → 被过滤")

    # B5 单个 URL 抛异常不能拖垮整批
    async def boom(client, store_url):
        raise RuntimeError("timeout")

    gs2 = GlobalSources()
    gs2._parse_detail = boom
    out2 = asyncio.run(gs2.refetch_company_names([u1]))
    check(out2.get(u1) == "", "B5 抓取异常 → 记为空串，不抛异常、不中断整批")


# --------------------------------------------------------------------------- #
# C. 存量报表：定位缺中文名的行 + 回填
# --------------------------------------------------------------------------- #
def _make_excel(path, rows):
    df = pd.DataFrame(rows, columns=TARGET_COLUMNS)
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Suppliers")
    return path


def _row(url, cn_name, en_name="Some Co"):
    r = {c: "" for c in TARGET_COLUMNS}
    r.update({"平台网址": url, "公司中文名": cn_name, "公司英文名": en_name})
    return r


def test_legacy_rows_and_backfill():
    print("\n[C] 存量报表的定位与回填")
    with tempfile.TemporaryDirectory() as td:
        path = _make_excel(os.path.join(td, "leads.xlsx"), [
            _row("https://www.globalsources.com/si/1.html", "", "NoName Co"),       # 缺中文名 → 应命中
            _row("https://www.globalsources.com/si/2.html", "已有中文名", "HasName Co"),  # 有 → 不命中
            _row("https://www.alibaba.com/x.html", "", "Ali Co"),                    # 非 GS → 不命中
        ])

        found = pipeline._existing_rows_missing_name(path)
        check(len(found) == 1, f"C1 只命中 1 行（缺中文名且是 GS）→ 实际 {len(found)}")
        check(found and found[0][0].endswith("/si/1.html"), "C2 命中的正是那个缺中文名的 GS 行")
        check(found and found[0][1] == "NoName Co", "C3 一并返回公司英文名，便于日志说明删了谁")

        # 回填：只填空，不覆盖已有值
        n = pipeline._backfill_company_names(path, {"https://www.globalsources.com/si/1.html": "补回的名字"})
        check(n == 1, f"C4 回填 1 处 → 实际 {n}")
        after = pd.read_excel(path, dtype=str)
        got = after.loc[after["平台网址"].astype(str).str.endswith("/si/1.html"), "公司中文名"].iloc[0]
        check(got == "补回的名字", f"C5 缺的行已填上 → {got!r}")

        # 再回填一次不该覆盖
        pipeline._backfill_company_names(path, {"https://www.globalsources.com/si/1.html": "别覆盖我"})
        after2 = pd.read_excel(path, dtype=str)
        got2 = after2.loc[after2["平台网址"].astype(str).str.endswith("/si/1.html"), "公司中文名"].iloc[0]
        check(got2 == "补回的名字", f"C6 已有值不被覆盖 → {got2!r}")

    check(pipeline._existing_rows_missing_name(os.path.join(td, "nope.xlsx")) == [],
          "C7 文件不存在 → []，不抛异常")


# --------------------------------------------------------------------------- #
# D. 导出时删除：drop_urls 生效，且"本轮零新增"也要能跑
# --------------------------------------------------------------------------- #
def _lead(url, company):
    return RawSupplierLead(company=company, platform="Global Sources", store_url=url,
                           registered_company="某中文名")


def _enrich():
    return {"site_phone": "", "tyc_phone": "", "email": "", "icp": "无",
            "contact_person": "", "contact_title": "", "created_at": "2026-09-16 00:00:00",
            "registered_company": ""}


def test_export_drop_urls():
    print("\n[D] export_leads_to_excel(drop_urls=...) 删除生效")
    with tempfile.TemporaryDirectory() as td:
        path = _make_excel(os.path.join(td, "leads.xlsx"), [
            _row("https://www.globalsources.com/si/1.html", "", "DropMe Co"),
            _row("https://www.globalsources.com/si/2.html", "保留", "KeepMe Co"),
        ])

        # D1 本轮有新增 + 删除一个历史行
        out = export_leads_to_excel(
            leads_data=[_lead("https://www.globalsources.com/si/3.html", "New Co")],
            enriched_results=[_enrich()],
            eval_results=[None],
            keyword="bag",
            output_file=path,
            drop_urls={"https://www.globalsources.com/si/1.html"},
        )
        df = pd.read_excel(out, dtype=str)
        urls = set(df["平台网址"].astype(str))
        check("https://www.globalsources.com/si/1.html" not in urls, "D1 待删的历史行已消失")
        check("https://www.globalsources.com/si/2.html" in urls, "D2 不该删的历史行仍在")
        check("https://www.globalsources.com/si/3.html" in urls, "D3 本轮新入库的行在")

    with tempfile.TemporaryDirectory() as td:
        # D4 本轮零新增（全部因缺中文名被剔除）—— 仍要能完成删除，不能炸
        path = _make_excel(os.path.join(td, "leads.xlsx"), [
            _row("https://www.globalsources.com/si/1.html", "", "DropMe Co"),
            _row("https://www.globalsources.com/si/2.html", "保留", "KeepMe Co"),
        ])
        out = export_leads_to_excel(
            leads_data=[], enriched_results=[], eval_results=[],
            keyword="bag", output_file=path,
            drop_urls={"https://www.globalsources.com/si/1.html"},
        )
        df = pd.read_excel(out, dtype=str)
        urls = set(df["平台网址"].astype(str))
        check(len(df) == 1 and "https://www.globalsources.com/si/2.html" in urls,
              f"D4 零新增也能删除，剩 {len(df)} 行")


if __name__ == "__main__":
    test_parse_detail_falls_back_to_store_url()
    test_refetch_company_names()
    test_legacy_rows_and_backfill()
    test_export_drop_urls()
    print(f"\n失败 {len(_FAILED)} 项")
    for d in _FAILED:
        print(f"   - {d}")
    sys.exit(1 if _FAILED else 0)
