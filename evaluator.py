import json
from typing import Optional
from pydantic import BaseModel, Field
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


class QualifiedLead(BaseModel):
    company_name: str = Field(default="未知公司", description="平台展示名称")
    registered_company: Optional[str] = Field(default="未找到",
                                              description="官方 Registered Company 法定注册公司名称/中文名")
    registered_address: Optional[str] = Field(default="未找到",
                                              description="官方 Company Registration Address 公司注册地址")
    contact_person: Optional[str] = Field(default="未找到", description="联系人姓名")
    contact_title: Optional[str] = Field(default="未找到", description="联系人职位")
    official_website: Optional[str] = Field(default=None, description="公司独立官网")
    police_record: Optional[str] = Field(default="无", description="独立官网公安网安备案情况")
    main_products: Optional[str] = Field(default="未提及", description="核心主营产品或品类")
    score: Optional[int] = Field(default=0, description="客户价值匹配度评分(0-100)")
    is_target: Optional[bool] = Field(default=False, description="是否属于目标潜在客户(True/False)")
    review_reason: Optional[str] = Field(default="无详细评估说明", description="判定合格或淘汰的核心原因")


def analyze_and_screen_lead(company_name: str, raw_text: str, target_criteria: str) -> QualifiedLead:
    system_prompt = f"""
你是一名资深的跨境出海外贸分析师。
你的任务是核对目标供应商信息，并严格对照【客户画像标准】进行打分：
{target_criteria}

请严格按 JSON 格式返回，未知项填 null：
{{
  "company_name": "{company_name}",
  "registered_company": "中文注册公司全称",
  "registered_address": "详细注册地址",
  "contact_person": "联系人姓名",
  "contact_title": "联系人职位",
  "official_website": null,
  "police_record": "无",
  "main_products": "主营品类",
  "score": 85,
  "is_target": true,
  "review_reason": "判定合格或淘汰的核心原因"
}}
"""

    user_content = f"""
【目标公司】：{company_name}
【深度背景与官方主页资料】：
{raw_text if raw_text.strip() else "暂无资料，请基于公司名常识评估。"}
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
            data["main_products"] = "根据主营名称评估"
        if data.get("score") is None:
            data["score"] = 50
        if data.get("is_target") is None:
            data["is_target"] = False

        return QualifiedLead(**data)

    except Exception as e:
        print(f"⚠️ 模型返回解析异常: {e}，启用基础兜底。")
        return QualifiedLead(
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