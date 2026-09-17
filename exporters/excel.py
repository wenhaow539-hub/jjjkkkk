from datetime import datetime
import os
from openpyxl.styles import Alignment
import pandas as pd

TARGET_COLUMNS = [
    "平台名称",
    "公司英文名",
    "平台网址",
    "公司中文名",
    "所属行业",  # 👈 新增行业字段
    "年限",
    "注册资本",
    "实缴资本",
    "参保人数",
    "经营状态",  # 👈 新增（爱企查提供）
    "海关注册编码",
    "海关注册日期",
    "公司注册地址",
    "天眼查联系人",
    "天眼查联系人职位",
    "独立站",
    "网址是否有备案",
    "官网联系方式",
    "天眼查联系方式",
    "搜索的关键词",
    "email",
    "数据来源",  # 👈 新增：爱企查 / 天眼查
    "入库时间",
]


def export_leads_to_excel(
    leads_data: list,
    enriched_results: list,
    eval_results: list,
    keyword: str,
    output_file: str = "suppliers_leads.xlsx",
    drop_urls: set | None = None,
) -> str:
    """导出报表。

    drop_urls：需要从**存量报表**里删除的「平台网址」集合（环球资源重爬后仍无中文名的行）。
    之所以要在这里删而不是上游剔除：这些是**历史遗留行**，本轮根本没采集到它们
    （指纹已在 seen_hashes.txt 里），只能靠主键从合并结果里摘掉。
    """
    print("\n📊 [数据整理与导出] 正在写入 Excel 报表...")

    new_rows = []
    fallback_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for lead, enrich_res, eval_res in zip(leads_data, enriched_results, eval_results):
        official_site = (lead.official_website or "").strip()

        if not official_site:
            icp_status = "无"
        else:
            icp_status = enrich_res.get("icp", "无") or "无"

        company_chinese = (
            enrich_res.get("registered_company")
            or getattr(eval_res, "clean_company_name", "")
            or lead.registered_company
            or ""
        )
        company_address = lead.registered_address or enrich_res.get("registered_address") or ""
        record_time = enrich_res.get("created_at") or fallback_time

        # 提取 AI 归纳的细分行业
        industry_val = getattr(eval_res, "industry", "") or ""

        new_rows.append({
            "平台名称": lead.platform,
            "公司英文名": lead.company,
            "平台网址": lead.store_url,
            "公司中文名": company_chinese,
            "所属行业": industry_val,  # 👈 写入表格
            "年限": getattr(lead, "platform_years", "") or "",
            "注册资本": enrich_res.get("registered_capital", "未公开"),
            "实缴资本": enrich_res.get("paid_in_capital", "未公开"),
            "参保人数": enrich_res.get("insured_count", "未公开"),
            "经营状态": enrich_res.get("business_status", ""),
            "海关注册编码": enrich_res.get("customs_code", ""),
            "海关注册日期": enrich_res.get("customs_reg_date", ""),
            "公司注册地址": company_address,
            "天眼查联系人": enrich_res.get("contact_person", ""),
            "天眼查联系人职位": enrich_res.get("contact_title", ""),
            "独立站": official_site,
            "网址是否有备案": icp_status,
            "官网联系方式": enrich_res.get("site_phone", ""),
            "天眼查联系方式": enrich_res.get("tyc_phone", ""),
            "搜索的关键词": keyword,
            "email": enrich_res.get("email", ""),
            "数据来源": enrich_res.get("data_source", ""),
            "入库时间": record_time,
        })

    # 本轮可能一条都没入库（例如全部因缺中文名被剔除），此时仍要能继续跑
    # 「存量报表的删除/清理」——所以空表也要带齐列，否则后面的 concat 会炸。
    new_df = pd.DataFrame(new_rows, columns=TARGET_COLUMNS) if new_rows else pd.DataFrame(columns=TARGET_COLUMNS)

    removed_count = 0
    if os.path.exists(output_file):
        try:
            old_df = pd.read_excel(output_file, dtype=str)
            if "联系方式" in old_df.columns and "官网联系方式" not in old_df.columns:
                old_df.rename(columns={"联系方式": "官网联系方式"}, inplace=True)
            for col in TARGET_COLUMNS:
                if col not in old_df.columns:
                    old_df[col] = ""

            old_keys = set(old_df["平台网址"].dropna().astype(str))
            merged_df = pd.concat([old_df[TARGET_COLUMNS], new_df], ignore_index=True)
            merged_df.drop_duplicates(subset=["平台网址"], keep="first", inplace=True)
            if drop_urls:
                before = len(merged_df)
                merged_df = merged_df[~merged_df["平台网址"].astype(str).isin(drop_urls)]
                removed_count = before - len(merged_df)
            final_df = merged_df[TARGET_COLUMNS]
            # 旧行优先（keep=first）：真正新增数 = 本次行里“平台网址”未在旧表出现过的条数。
            # 用行数相减会在列结构变化（新增列导致旧表重排）时失真，故改为按主键集合差计算。
            added_count = sum(1 for u in new_df["平台网址"].astype(str) if u not in old_keys)
        except Exception:
            final_df = new_df
            added_count = len(new_df)
    else:
        final_df = new_df
        added_count = len(new_df)

    target_path = output_file
    try:
        if os.path.exists(target_path):
            with open(target_path, "a+"):
                pass
    except PermissionError:
        target_path = f"suppliers_leads_{int(datetime.now().timestamp())}.xlsx"
        print(f"⚠️ [占用警告] 原文件被占用，已重定向写入至: {target_path}")

    with pd.ExcelWriter(target_path, engine="openpyxl") as writer:
        final_df.to_excel(writer, index=False, sheet_name="Suppliers")
        ws = writer.sheets["Suppliers"]

        for cell in ws[1]:
            cell.alignment = Alignment(horizontal="center", vertical="center")

        text_cols = [
            "所属行业", "年限", "官网联系方式", "天眼查联系方式", "email", "入库时间",
            "公司注册地址", "平台网址", "独立站", "网址是否有备案",
            "注册资本", "实缴资本", "参保人数", "经营状态", "数据来源",
            "海关注册编码", "海关注册日期",
        ]
        for col_idx, col_name in enumerate(final_df.columns, start=1):
            is_text = col_name in text_cols
            for row_idx in range(2, len(final_df) + 2):
                cell = ws.cell(row=row_idx, column=col_idx)
                if is_text:
                    cell.number_format = "@"
                    cell.data_type = "s"
                    if cell.value is not None:
                        cell.value = str(cell.value)

    print(f"\n🎉 [流水线完成] 数据已入库: {target_path}")
    print(f"📈 累计总商户: {len(final_df)} 条 (本次真正新增入库: {max(0, added_count)} 条)")
    if removed_count:
        print(f"🗑️ 已从存量报表删除 {removed_count} 条（环球资源重爬后仍无中文工商名）")
    return target_path