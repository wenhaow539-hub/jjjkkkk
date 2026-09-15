import json
import re
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from models import RawSupplierLead


class EvaluatedSupplier(BaseModel):
    clean_company_name: str = Field(default="", description="规范后的中国大陆法定工商全称")
    industry: str = Field(default="", description="归纳总结的细分行业标签(4-8字，如：LED照明、办公家具等)")
    is_factory: bool = Field(default=True, description="是否属于实体生产制造型工厂")
    confidence_score: float = Field(default=0.8, description="可信度评分")
    summary: str = Field(default="", description="简短评析")


SYSTEM_PROMPT = """你是一个专业的跨境供应链分析师。
你的任务是根据供应商的英文名、现有中文名、主营产品(Main Products)或搜索词，完成两项核心工作：
1. 【公司名规范】：推断其在国家工信部/工商局的中国大陆法定全称（例如 "Dongguan Huaruida Hardware Co., Ltd." 规范为 "东莞市华瑞达五金有限公司"）。若为海外离岸公司则保留原英文。
2. 【所属行业归纳】：根据主营产品与品类，提炼出精准、专业的细分行业（如：LED商业照明、五金冲压件、3C数码配件、办公家具等，字数控制在4-8字以内）。

必须且仅输出标准的 JSON 格式：
{
    "clean_company_name": "规范的中文公司名",
    "industry": "细分行业名称",
    "is_factory": true,
    "confidence_score": 0.9,
    "summary": "判定依据"
}
"""


def _heuristic_industry_fallback(lead: RawSupplierLead) -> str:
    """当大模型不可用时的规则词库兜底"""
    text = f"{lead.raw_products} {lead.card_product} {lead.company}".lower()
    if any(k in text for k in ["led", "light", "lamp", "bulb", "lighting"]):
        return "LED照明设备"
    if any(k in text for k in ["chair", "desk", "table", "furniture"]):
        return "家具办公用品"
    if any(k in text for k in ["audio", "speaker", "headphone", "earphone"]):
        return "音频电子设备"
    if any(k in text for k in ["metal", "hardware", "casting", "machining"]):
        return "五金机械制造"
    if any(k in text for k in ["solar", "battery", "energy"]):
        return "新能源与电气"
    return "电子科技制造"


async def evaluate_supplier_icp(
    lead: RawSupplierLead,
    api_key: str,
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-chat"
) -> EvaluatedSupplier:
    # 1. 基础 Key 校验
    if not api_key or "sk-" not in api_key or "86fe" in api_key:
        print(f"      ❌ [DeepSeek Key 异常] 当前 API Key 无效或未配置，已触发规则兜底！")
        return EvaluatedSupplier(
            clean_company_name=lead.registered_company or "",
            industry=_heuristic_industry_fallback(lead),
            is_factory=True,
            confidence_score=0.5,
            summary="API Key无效，规则兜底",
        )

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    products_info = lead.raw_products or lead.card_product or "未提供具体产品列表"
    user_prompt = f"""
供应商英文名: {lead.company}
平台现有中文名: {lead.registered_company or '无'}
主营产品(Main Products): {products_info}
搜索关键词: {lead.card_product}

请判断其中国大陆法定全称，并归纳其所属行业。
"""

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            timeout=20.0
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        return EvaluatedSupplier(**data)
    except Exception as e:
        err_msg = str(e)
        if "401" in err_msg:
            print(f"      🚨 [DeepSeek 401 认证失败] Key 已过期或错误: {err_msg}")
        elif "402" in err_msg:
            print(f"      🚨 [DeepSeek 402 余额不足] 账户额度已耗尽，请充值！")
        else:
            print(f"      ⚠️ [DeepSeek 请求超时/错误] {err_msg}")

        # 出错时不再写死“通用制造业”，走关键词规则匹配
        return EvaluatedSupplier(
            clean_company_name=lead.registered_company or "",
            industry=_heuristic_industry_fallback(lead),
            is_factory=True,
            confidence_score=0.5,
            summary=f"质检降级: {err_msg[:30]}"
        )