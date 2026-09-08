import os
import time
from openpyxl import Workbook, load_workbook
from evaluator import QualifiedLead


def append_lead_to_excel(lead: QualifiedLead, file_path: str):
    """追加线索到 Excel，包含独立官网与公安备案列"""
    headers = [
        "平台展示名称",
        "法定注册公司",
        "公司注册地址",
        "联系人",
        "联系人职位",
        "独立官网",
        "网站备案(ICP/网安)",
        "是否合格",
        "匹配分",
        "主营品类",
        "评估理由"
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
        lead.company_name,
        lead.registered_company or "未找到",
        lead.registered_address or "未找到",
        lead.contact_person or "未找到",
        lead.contact_title or "未找到",
        lead.official_website or "未找到",
        lead.police_record or "无",
        "是" if lead.is_target else "否",
        lead.score,
        lead.main_products or "未提及",
        lead.review_reason
    ])

    try:
        wb.save(target_path)
        print(f"✅ [成功入库] {lead.company_name}")
        print(f"   ├─ 法定公司: {lead.registered_company}")
        print(f"   ├─ 注册地址: {lead.registered_address}")
        print(f"   ├─ 联系人: {lead.contact_person} ({lead.contact_title})")
        print(f"   ├─ 独立官网: {lead.official_website or '未找到'}")
        print(f"   └─ 公安备案: {lead.police_record}")
    except PermissionError:
        backup_path = f"target_leads_{int(time.time())}.xlsx"
        wb.save(backup_path)
        print(f"⚠️ 提示: '{target_path}' 被 Excel 占用，数据已写入备用文件: {backup_path}")