import asyncio
import json
from openai import AsyncOpenAI
from pydantic import BaseModel
from models import RawSupplierLead


class EvaluatedSupplier(BaseModel):
    clean_company_name: str = ""
    is_factory: bool = True
    confidence_score: float = 1.0
    summary: str = ""


async def evaluate_supplier_icp(
    lead: RawSupplierLead,
    api_key: str,
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-chat"
) -> EvaluatedSupplier:
    """
    大模型质检：规范已有中文名，严禁根据共享办公地址臆测公司名
    """
    fallback_name = lead.registered_company or ""
    default_result = EvaluatedSupplier(
        clean_company_name=fallback_name,
        is_factory=True,
        confidence_score=0.8,
        summary="未执行大模型质检或降级回退"
    )

    if not api_key or "your" in api_key.lower() or api_key.strip() == "":
        return default_result

    prompt = f"""你是一个严谨的外贸B2B工商数据审计员。请根据提供的商户信息，提炼其中国大陆工商局登记的标准法定中文全称。

【输入信息】：
- 商户英文名: {lead.company}
- 平台登记中文名: {lead.registered_company}
- 注册地址: {lead.registered_address}
- 官网网址: {lead.official_website}

【严格执行规则】：
1. 如果【平台登记中文名】已有内容，仅做规范化清洗（去除多余空格与标点）。
2. 如果【平台登记中文名】为空：
   - 严禁根据【注册地址】推测、联想或编造任何公司！因为写字楼/孵化器地址存在成百上千家共用企业！
   - 除非英文名是极其明确的汉语拼音（如 "Shenzhen BYD Technology" -> "比亚迪"），否则必须直接输出空字符串 ""！
3. 宁可留空，绝不能张冠李戴。

请返回严格的 JSON 格式：
{{
  "clean_company_name": "清洗后的标准中文全称，若无法100%确定必须输出空字符串\"\"",
  "is_factory": true或false,
  "confidence_score": 0.0到1.0的置信度,
  "summary": "判定依据"
}}
"""

    try:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=12.0)
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一个严谨的工商实体画像提取专家，只输出合法 JSON。"},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        return EvaluatedSupplier(**data)
    except Exception as e:
        print(f"      ⚠️ [LLM 质检跳过] {lead.company} 请求异常: {e}")
        return default_result