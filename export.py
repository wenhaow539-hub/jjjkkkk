from openpyxl import Workbook, load_workbook
import os
from evaluator import QualifiedLead


def append_lead_to_excel(lead: QualifiedLead, file_path: str):
    """将清洗评估后的线索追加到 Excel 表格"""
    headers = ["公司名称", "是否合格", "匹配分", "联系邮箱", "联系电话/WhatsApp", "独立官网", "主营品类", "评估理由"]

    if not os.path.exists(file_path):
        wb = Workbook()
        ws = wb.active
        ws.title = "潜在客户线索池"
        ws.append(headers)
    else:
        wb = load_workbook(file_path)
        ws = wb.active

    ws.append([
        lead.company_name,
        "是" if lead.is_target else "否",
        lead.score,
        lead.contact_email or "未找到",
        lead.contact_phone or "未找到",
        lead.official_website or "未找到",
        lead.main_products,
        lead.review_reason
    ])

    wb.save(file_path)
    print(f"✅ 已写入表格: {lead.company_name} (评分: {lead.score})")