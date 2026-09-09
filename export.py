import os
import time
from openpyxl import Workbook, load_workbook
from evaluator import QualifiedLead


def append_lead_to_excel(lead: QualifiedLead, file_path: str):
    headers = [
        "数据来源",
        "平台展示名称",
        "法定注册公司(Registered Company)",
        "公司注册地址(Company Registration Address)",
        "联系人",
        "联系人职位",
        "联系电话",
        "独立官网",
        "网站备案(ICP/网安)",
        "主营品类"
    ]

    target_path = file_path
    if not os.path.exists(target_path):
        wb = Workbook()
        ws = wb.active
        ws.title = "客户线索池"
        ws.append(headers)
    else:
        try:
            wb = load_workbook(target_path)
            ws = wb.active
        except Exception:
            wb = Workbook()
            ws = wb.active
            ws.title = "客户线索池"
            ws.append(headers)

    ws.append([
        lead.data_source or "Global Sources",
        lead.company_name,
        lead.registered_company or "未找到",
        lead.registered_address or "未找到",
        lead.contact_person or "未找到",
        lead.contact_title or "未找到",
        lead.phone or "未找到",
        lead.official_website or "未找到",
        lead.police_record or "无",
        lead.main_products or "未提及"
    ])

    try:
        wb.save(target_path)
        print(f"✅ [成功入库] [{lead.data_source}] {lead.company_name}")
        print(f"   ├─ 法定公司: {lead.registered_company}")
        print(f"   ├─ 联系人: {lead.contact_person} ({lead.contact_title})")
        print(f"   ├─ 电话: {lead.phone or '未找到'}")
        print(f"   ├─ 独立官网: {lead.official_website or '未找到'}")
        print(f"   └─ 主营品类: {lead.main_products}")
    except PermissionError:
        backup_path = f"target_leads_{int(time.time())}.xlsx"
        wb.save(backup_path)
        print(f"⚠️ 提示: '{target_path}' 被 Excel 占用，数据已写入备用文件: {backup_path}")