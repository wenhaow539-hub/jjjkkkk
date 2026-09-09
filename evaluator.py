import json
import httpx
from models import RawSupplierLead, LLMEvalResult

PROMPT_TEMPLATE = """
你是一名资深的外贸采购供应链总监。请依据下方的供应商平台采集信息，分析该商户是否符合【源头实体制造工厂】画像，淘汰纯外贸中介/无自营产能的贸易商。

【供应商信息】：
- 店铺名称: {company}
- 法定注册公司: {registered_company}
- 经营地址: {registered_address}
- 展厅产品组: {raw_products}
- 档案详情: {detail_content}
- 目标采购类目: {card_product}

【判定原则】：
1. 实体工厂：注册公司通常带“制造、科技、五金、塑料、电子”等，地址处于工业园/工业区/厂房，展厅产品高度聚焦垂直。
2. 贸易中介：公司带“进出口、贸易、商业、商行”，地址在商业写字楼，产品线杂乱跨界。

请输出严格的 JSON 格式，字段必须与以下一致：
{{
  "is_direct_factory": bool,
  "icp_score": int (1-10),
  "disqualify_reason": "淘汰原因或null",
  "core_competence": "核心主打优势(30字内)",
  "clean_company_name": "清洗后的法定名称"
}}
"""


async def evaluate_supplier_icp(lead: RawSupplierLead, api_key: str, base_url: str = "https://api.deepseek.com") -> LLMEvalResult:
    """调用轻量大模型做 ICP 匹配与身份去杂"""
    prompt = PROMPT_TEMPLATE.format(
        company=lead.company,
        registered_company=lead.registered_company,
        registered_address=lead.registered_address,
        raw_products=lead.raw_products,
        detail_content=lead.detail_content[:1500],
        card_product=lead.card_product
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"}
                }
            )
            result = resp.json()["choices"][0]["message"]["content"]
            parsed = json.loads(result)
            return LLMEvalResult(**parsed)
    except Exception as e:
        # LLM 熔断托底策略
        return LLMEvalResult(
            is_direct_factory=True,
            icp_score=5,
            disqualify_reason=f"LLM质检异常兜底: {e}",
            core_competence=lead.card_product,
            clean_company_name=lead.registered_company or lead.company
        )