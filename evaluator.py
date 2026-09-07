import json
from typing import Optional
from pydantic import BaseModel, Field
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


# 所有字段均设为可空（Optional）并赋予默认安全值
class QualifiedLead(BaseModel):
    company_name: str = Field(default="未知公司", description="公司或商户全称")
    official_website: Optional[str] = Field(default=None, description="公司独立官网")
    contact_email: Optional[str] = Field(default=None, description="联系邮箱")
    contact_phone: Optional[str] = Field(default=None, description="联系电话/WhatsApp")
    main_products: Optional[str] = Field(default="未提及", description="核心主营产品或品类")
    score: Optional[int] = Field(default=0, description="客户价值匹配度评分(0-100)")
    is_target: Optional[bool] = Field(default=False, description="是否属于目标潜在客户(True/False)")
    review_reason: Optional[str] = Field(default="无详细评估说明", description="判定合格或淘汰的核心原因")


def analyze_and_screen_lead(company_name: str, raw_text: str, target_criteria: str) -> QualifiedLead:
    system_prompt = f"""
你是一名经验丰富的跨境出海外贸分析师。
你的任务是评估目标公司是否符合我们的【客户画像标准】：
{target_criteria}

请严格按以下 JSON 字段结构返回，所有键必须保留。若某项未找到，直接填 null，严禁编造：
{{
  "company_name": "{company_name}",
  "official_website": null,
  "contact_email": null,
  "contact_phone": null,
  "main_products": "根据资料总结其主营产品",
  "score": 85,
  "is_target": true,
  "review_reason": "判定合格或淘汰的核心原因"
}}
"""

    user_content = f"""
【目标公司】：{company_name}
【收集到的网页与背景资料】：
{raw_text if raw_text.strip() else "暂无外部反查资料，请基于公司名及展示产品进行评估。"}
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

        # 强制数据预清洗，彻底消除任何 None 导致的不兼容
        data["company_name"] = data.get("company_name") or company_name
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
            main_products="自动识别",
            score=50,
            is_target=True,
            review_reason="基础线索录入"
        )