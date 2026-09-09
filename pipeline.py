import asyncio
import os
from datetime import datetime
import pandas as pd
from scraper import scrape_globalsources_suppliers


async def run_pipeline(keyword: str = "monitor", max_count: int = 5, output_file: str = "suppliers_leads.xlsx"):
    print(f"🚀 [Pipeline] 启动采集任务 | 关键词: {keyword} | 计划采集: {max_count}")

    # 1. 执行真实浏览器 CDP 采集（哈希去重已由 scraper.py 和 dedup.py 在内存/txt中完成）
    leads_data = await scrape_globalsources_suppliers(keyword=keyword, max_count=max_count)
    if not leads_data:
        print("💡 未采集到新的有效店铺，流程结束。")
        return

    # 2. 组装本次新增的数据（12 个原始字段 + created_at）
    new_rows = []
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for item in leads_data:
        new_rows.append({
            "company": str(item.get("company", "") or ""),
            "platform": str(item.get("platform", "Global Sources") or ""),
            "store_url": str(item.get("store_url", "") or ""),
            "registered_company": str(item.get("registered_company", "") or ""),
            "registered_address": str(item.get("registered_address", "") or ""),
            "contact_person": str(item.get("contact_person", "") or ""),
            "contact_title": str(item.get("contact_title", "") or ""),
            "official_website": str(item.get("official_website", "") or ""),
            "phone": str(item.get("phone", "") or ""),
            "raw_products": str(item.get("raw_products", "") or ""),
            "detail_content": str(item.get("detail_content", "") or ""),
            "card_product": str(item.get("card_product", "") or ""),
            "created_at": current_time
        })

    new_df = pd.DataFrame(new_rows)

    # 3. 读取历史 Excel 并追加合并（关键：防止旧数据被冲刷覆盖）
    if os.path.exists(output_file):
        try:
            # 强制按字符串类型读取，防止已有号码被解析破坏
            old_df = pd.read_excel(output_file, dtype=str)
            final_df = pd.concat([old_df, new_df], ignore_index=True)
            # 根据 store_url 兜底去除潜在重复行，保留首次记录
            if "store_url" in final_df.columns:
                final_df.drop_duplicates(subset=["store_url"], keep="first", inplace=True)
            print(f"📂 读取到历史数据 {len(old_df)} 条，合并本次新增 {len(new_df)} 条，总计 {len(final_df)} 条。")
        except Exception as e:
            print(f"⚠️ 读取历史数据文件失败，将以全新数据保存: {e}")
            final_df = new_df
    else:
        final_df = new_df

    # 4. 严格以纯文本格式写入 Excel，确保联系电话不失真
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        final_df.to_excel(writer, index=False, sheet_name="Suppliers")
        worksheet = writer.sheets["Suppliers"]

        # 重点保护文本列
        text_columns = ["phone", "created_at", "registered_address", "store_url", "detail_content"]

        for col_idx, col_name in enumerate(final_df.columns, start=1):
            is_text_col = col_name in text_columns
            for row_idx in range(2, len(final_df) + 2):
                cell = worksheet.cell(row=row_idx, column=col_idx)
                if is_text_col:
                    cell.number_format = "@"
                    cell.data_type = "s"
                    if cell.value is not None:
                        cell.value = str(cell.value)

    print(f"\n🎯 [Pipeline 完成] 成功更新: {output_file}，历史数据已保留，表格现有记录: {len(final_df)} 条！")


if __name__ == "__main__":
    asyncio.run(run_pipeline(keyword="monitor", max_count=3))