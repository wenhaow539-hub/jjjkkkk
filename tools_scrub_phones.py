"""一次性清理：把报表里已落盘的假号码清空（占位符 / 天眼查客服热线 / UI 文本）。

这些值是**修复前**的旧数据，`utils.phone` 的清洗升级管不到历史行，需要单独擦一次。
⚠️ 只清「清洗后为空」的单元格，不动任何真实号码；改前自动备份。
"""
import os
import sys
from datetime import datetime

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.phone import sanitize_phone_string  # noqa: E402

XLSX = "suppliers_leads.xlsx"
PHONE_COLS = ("官网联系方式", "天眼查联系方式")

wb = openpyxl.load_workbook(XLSX)
ws = wb.active
headers = [c.value for c in ws[1]]
cols = {headers.index(h) + 1: h for h in PHONE_COLS if h in headers}
print("待清理列:", cols)

cleared = []
for row in range(2, ws.max_row + 1):
    for ci, hname in cols.items():
        cell = ws.cell(row=row, column=ci)
        raw = str(cell.value or "").strip()
        if not raw:
            continue
        if not sanitize_phone_string(raw):
            name = ws.cell(row=row, column=headers.index("公司中文名") + 1).value
            cleared.append((row, hname, raw, str(name or "")[:22]))
            cell.value = None

print(f"\n将清空 {len(cleared)} 个单元格：")
for r, h, raw, nm in cleared:
    print(f"  第 {r} 行  {nm}  [{h}]  {raw!r}")

if not cleared:
    print("无需清理。")
    sys.exit(0)

backup = f"suppliers_leads.bak-phonescrub-{datetime.now():%Y%m%d_%H%M%S}.xlsx"
wb.save(backup)
print(f"\n💾 备份 → {backup}")
try:
    wb.save(XLSX)
    print(f"✅ 已清理并写回 {XLSX}")
except PermissionError:
    print(f"❌ {XLSX} 被占用（Excel 打开着？）。备份已留：{backup}")
    sys.exit(1)
