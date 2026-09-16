import json
import re
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from models import RawSupplierLead


class EvaluatedSupplier(BaseModel):
    clean_company_name: str = Field(default="", description="规范后的中国大陆法定工商全称")
    industry: str = Field(default="", description="归纳总结的细分行业标签(4-8字，如：电子元器件、集成电路、LED照明等)")
    is_factory: bool = Field(default=True, description="是否属于实体生产制造型工厂")
    confidence_score: float = Field(default=0.8, description="可信度评分")
    summary: str = Field(default="", description="简短评析与推断依据")


SYSTEM_PROMPT = """你是一个专业的工业制造与跨境供应链分析专家。
你的任务是根据供应商的企业名称及业务线索，完成两项核心工作：

1. 【公司名称规范与法定全称推断】：
   - 若输入为英文公司名（如环球资源外贸供应商）：必须根据其拼音及英文含义，推断并还原其在中国工信部/市场监督管理局注册的“中国大陆法定中文全称”（例如 "Dongguan Huaruida Hardware Co., Ltd." 规范为 "东莞市华瑞达五金有限公司"；"Shenzhen Senrong Handbag Co., Ltd." 规范为 "深圳市森荣手袋有限公司"）。若确属中国香港或海外离岸公司，则保留原名。
   - 若输入已是中文名（如爱采购商家）：剔除页面残留的徽章、年限杂质，保持规范的法定工商全称。

2. 【细分行业精准归纳】：
   - 【核心准则：公司名称权重最高】：优先分析公司名称中的核心行业定性词（如“芯/半导体/集成电路/电子/光电/五金/机械/皮具/箱包/服装/塑料”等）。
   - 【排除型号与搜索词噪音】：搜索词（如 bag、led）经常是芯片封装或元器件型号（如 P2703BAG、BGA封装芯片）。严禁因搜索词为 bag 就将半导体、电子元器件企业判定为【箱包制造】！
   - 【行业标签规范】：字数严格控制在 4~8 个字（如：电子元器件、集成电路与芯片、LED商业照明、箱包皮具制造、五金机械制造等）。

必须且仅输出标准的 JSON 格式：
{
    "clean_company_name": "规范的中文法定公司名",
    "industry": "细分行业名称",
    "is_factory": true,
    "confidence_score": 0.9,
    "summary": "判定依据"
}
"""


def _heuristic_industry_fallback(lead: RawSupplierLead) -> str:
    """当大模型不可用或降级时的关键词加权规则库"""
    name_text = f"{lead.registered_company} {lead.company}".lower()
    prod_text = f"{lead.raw_products}".lower()

    # 1. 优先按公司名称中的核心实体词定性
    if any(k in name_text for k in ["芯", "半导体", "集成电路", "微电子"]):
        return "集成电路与芯片"
    if any(k in name_text for k in ["电子", "元器件", "科技", "electronic"]):
        if any(k in prod_text for k in ["芯片", "ic", "二极管", "电容", "模块"]):
            return "电子元器件"
    if any(k in name_text for k in ["照明", "光电", "灯", "lighting", "led"]):
        return "LED照明设备"
    if any(k in name_text for k in ["皮具", "箱包", "手袋", "bag", "leather"]):
        return "箱包皮具制造"
    if any(k in name_text for k in ["五金", "机械", "模具", "精密", "hardware", "metal"]):
        return "五金机械制造"
    if any(k in name_text for k in ["家具", "办公", "furniture", "chair"]):
        return "家具办公用品"

    # 2. 依据抓取到的真实产品文本辅助判定
    if any(k in prod_text for k in ["芯片", "ic", "元器件", "单片机", "存储器"]):
        return "电子元器件"
    if any(k in prod_text for k in ["手袋", "背包", "皮包", "拉杆箱"]):
        return "箱包皮具制造"

    return "电子科技制造"


async def evaluate_supplier_icp(
    lead: RawSupplierLead,
    api_key: str,
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-chat"
) -> EvaluatedSupplier:
    """结合 DeepSeek 大模型对中英文供应商执行主体合规提纯与行业归纳"""
    if not api_key or "sk-" not in api_key:
        print("      ⚠️ [DeepSeek API Key 缺失或无效] 已切换至规则词库兜底")
        return EvaluatedSupplier(
            clean_company_name=lead.registered_company or lead.company,
            industry=_heuristic_industry_fallback(lead),
            is_factory=True,
            confidence_score=0.5,
            summary="API Key无效，规则库兜底",
        )

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    # 剔除用纯搜索词污染产品列表的情况
    real_products = (lead.raw_products or "").strip()
    if not real_products or len(real_products) < 2 or real_products.lower() == lead.card_product.lower():
        products_display = "未提取到独立主营列表，请重点依据企业名称定性"
    else:
        products_display = real_products

    user_prompt = f"""
供应商原始名称: {lead.company}
平台现有注册名称: {lead.registered_company or '无'}
提取到的主营业务/产品示例: {products_display}
平台搜索词: {lead.card_product}

请判断其规范的中国大陆法定工商全称，并归纳其所属行业。
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
            timeout=25.0
        )
        content = response.choices[0].message.content
        data = json.loads(content)
        return EvaluatedSupplier(**data)

    except Exception as e:
        err_msg = str(e)
        if "401" in err_msg:
            print(f"      🚨 [DeepSeek 401 鉴权失败] API Key 无效或过期: {err_msg}")
        elif "402" in err_msg:
            print(f"      🚨 [DeepSeek 402 余额不足] 账户额度耗尽，请充值！")
        else:
            print(f"      ⚠️ [DeepSeek 质检异常] {err_msg}")

        return EvaluatedSupplier(
            clean_company_name=lead.registered_company or lead.company,
            industry=_heuristic_industry_fallback(lead),
            is_factory=True,
            confidence_score=0.5,
            summary=f"降级兜底: {err_msg[:30]}"
        )