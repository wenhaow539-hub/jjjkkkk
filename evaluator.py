import json
import re
from typing import Optional
from pydantic import BaseModel, Field
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


class QualifiedLead(BaseModel):
    data_source: Optional[str] = Field(default="Global Sources", description="数据来源渠道/平台")
    company_name: str = Field(default="未知公司", description="平台展示名称")
    registered_company: Optional[str] = Field(default="未找到",
                                              description="官方 Registered Company 法定注册公司名称/中文名")
    registered_address: Optional[str] = Field(default="未找到",
                                              description="官方 Company Registration Address 公司注册地址")
    contact_person: Optional[str] = Field(default="未找到", description="联系人姓名")
    contact_title: Optional[str] = Field(default="未找到", description="联系人职位")
    official_website: Optional[str] = Field(default=None, description="公司独立官网")
    police_record: Optional[str] = Field(default="无", description="独立官网工信部/公安备案情况")
    main_products: Optional[str] = Field(default="未提及", description="由AI提炼翻译的纯中文主营品类")
    score: Optional[int] = Field(default=0, description="客户价值匹配度评分(0-100)")
    is_target: Optional[bool] = Field(default=False, description="是否属于目标潜在客户(True/False)")
    review_reason: Optional[str] = Field(default="无详细评估说明", description="判定合格或淘汰的核心原因")


def force_translate_products_to_chinese(raw_english_text: str) -> str:
    """如果模型偷懒输出了英文，触发极简轻量翻译强制转换为纯中文品类"""
    if not raw_english_text or raw_english_text == "未提及":
        return "未提及"

    # 检查是否包含英文字符
    if not re.search(r'[a-zA-Z]{3,}', raw_english_text):
        return raw_english_text

    try:
        res = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "你是资深外贸品类专家。把输入的所有外贸产品词严格翻译并归纳为地道中文主营产品词，用顿号（、）连接。严禁输出任何英文字母或括号数字，只输出中文结果。"
                },
                {"role": "user", "content": raw_english_text}
            ],
            temperature=0.0
        )
        translated = res.choices[0].message.content.strip()
        # 清除模型可能带的标点杂质
        translated = re.sub(r'^(主营产品|翻译结果|产品列表)[:：]\s*', '', translated)
        return translated
    except Exception:
        return raw_english_text


def analyze_and_screen_lead(company_name: str, raw_text: str, target_criteria: str) -> QualifiedLead:
    system_prompt = f"""
你是一名资深的跨境出海外贸分析师。
你的任务是评估目标供应商，并严格对照【客户画像标准】进行打分：
{target_criteria}

【绝对最高优先级约束 - 语言锁定】：
1. main_products 字段【严禁输出任何英文字母】！
   - 严禁原样抄写英文！必须 100% 翻译并归纳为地道的中文工业品类词，词与词用顿号（、）隔开。
   - 示例禁止：Gaming monitor, car camera, car monitor, parking sensor, car dvr, BSD system
   - 示例必须：电竞显示器、车载摄像头、车载显示屏、倒车雷达、行车记录仪、盲区监测系统
2. 彻底去除括号及数字（如去除 '(879)'、'(19)'、'Others'）。
3. 严格按以下 JSON 格式返回，未知项填 null：
{{
  "company_name": "{company_name}",
  "registered_company": "中文注册公司全称",
  "registered_address": "详细注册地址",
  "contact_person": "联系人姓名",
  "contact_title": "联系人职位",
  "official_website": null,
  "police_record": "无",
  "main_products": "电竞显示器、车载显示屏、商业大屏（必须全中文，顿号隔开）",
  "score": 85,
  "is_target": true,
  "review_reason": "判定合格或淘汰的核心原因"
}}
"""

    user_content = f"""
【目标公司】：{company_name}
【深度背景与官方页面资料】：
{raw_text if raw_text.strip() else "暂无资料，请基于常识评估。"}
"""

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            temperature=0.1
        )

        data = json.loads(response.choices[0].message.content)

        data["company_name"] = data.get("company_name") or company_name
        if not data.get("registered_company"):
            data["registered_company"] = "未找到"
        if not data.get("registered_address"):
            data["registered_address"] = "未找到"
        if not data.get("contact_person"):
            data["contact_person"] = "未找到"
        if not data.get("contact_title"):
            data["contact_title"] = "未找到"
        if not data.get("police_record"):
            data["police_record"] = "无"
        if not data.get("main_products"):
            data["main_products"] = "综合显示设备制造"
        if data.get("score") is None:
            data["score"] = 50
        if data.get("is_target") is None:
            data["is_target"] = False

        lead = QualifiedLead(**data)

        # 哨兵防御：如果大模型依然输出了英文，强制进行中文重译
        if re.search(r'[a-zA-Z]{3,}', lead.main_products):
            lead.main_products = force_translate_products_to_chinese(lead.main_products)

        return lead

    except Exception as e:
        print(f"⚠️ 模型返回解析异常: {e}，启用基础兜底。")
        return QualifiedLead(
            data_source="Global Sources",
            company_name=company_name,
            registered_company="未找到",
            registered_address="未找到",
            contact_person="未找到",
            contact_title="未找到",
            police_record="无",
            main_products="自动识别",
            score=50,
            is_target=True,
            review_reason="基础线索录入"
        )