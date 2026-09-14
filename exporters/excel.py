from datetime import datetime
import os
from openpyxl.styles import Alignment, PatternFill
import pandas as pd

TARGET_COLUMNS = [
    "平台名称", "公司英文名", "平台网址", "公司中文名", "公司注册地址",
    "天眼查联系人", "天眼查联系人职位", "独立站", "网址是否有备案",
    "官网联系方式", "天眼查联系方式", "搜索的关键词", "email", "入库时间"
]

def export_leads_to_excel(
    leads_data: list,
    enriched_results: list,
    eval_results: list,
    keyword: str,
    output_file: str = "suppliers_leads.xlsx"
) -> str:
    print(f"\n📊 [数据整理与导出] 正在写入 Excel 报表...")
    new_rows = []
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for lead, enrich_res, eval_res in zip(leads_data, enriched_results, eval_results):
        official_site = lead.official_website or ""
        icp_status = enrich_res.get("icp", "无") if official_site else "无"

        company_chinese = (
            enrich_res.get("registered_company")
            or getattr(eval_res, "clean_company_name", "")
            or lead.registered_company
            or ""
        )
        company_address = lead.registered_address or enrich_res.get("registered_address") or ""

        new_rows.append({
            "平台名称": lead.platform,
            "公司英文名": lead.company,
            "平台网址": lead.store_url,
            "公司中文名": company_chinese,
            "公司注册地址": company_address,
            "天眼查联系人": enrich_res.get("contact_person", ""),
            "天眼查联系人职位": enrich_res.get("contact_title", ""),
            "独立站": official_site,
            "网址是否有备案": icp_status,
            "官网联系方式": enrich_res.get("site_phone", ""),
            "天眼查联系方式": enrich_res.get("tyc_phone", ""),
            "搜索的关键词": keyword,
            "email": enrich_res.get("email", ""),
            "入库时间": current_time
        })

    new_df = pd.DataFrame(new_rows)[TARGET_COLUMNS]

    if os.path.exists(output_file):
        try:
            old_df = pd.read_excel(output_file, dtype=str)
            if "联系方式" in old_df.columns and "官网联系方式" not in old_df.columns:
                old_df.rename(columns={"联系方式": "官网联系方式"}, inplace=True)
            for col in TARGET_COLUMNS:
                if col not in old_df.columns:
                    old_df[col] = ""
            merged_df = pd.concat([old_df[TARGET_COLUMNS], new_df], ignore_index=True)
            merged_df.drop_duplicates(subset=["平台网址"], keep="last", inplace=True)
            final_df = merged_df[TARGET_COLUMNS]
        except Exception:
            final_df = new_df
    else:
        final_df = new_df

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
        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        for cell in ws[1]:
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

        text_cols = [
            "官网联系方式", "天眼查联系方式", "email", "入库时间",
            "公司注册地址", "平台网址", "独立站", "网址是否有备案"
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
    print(f"📈 累计总商户: {len(final_df)} 条 (本次追加: {len(new_df)} 条)")
    return target_path