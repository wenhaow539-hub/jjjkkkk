from typing import List, Optional
from pydantic import BaseModel, Field


class RawSupplierLead(BaseModel):
    """第 1 层抓取的原始线索"""
    company: str
    platform: str = "Global Sources"
    store_url: str
    registered_company: Optional[str] = ""
    registered_address: Optional[str] = ""
    contact_person: Optional[str] = ""
    contact_title: Optional[str] = ""
    official_website: Optional[str] = ""
    phone: Optional[str] = ""
    raw_products: Optional[str] = ""
    detail_content: Optional[str] = ""
    card_product: str


class LLMEvalResult(BaseModel):
    """第 3 层大模型清洗质检结果"""
    is_direct_factory: bool = Field(description="是否为源头实体工厂，若纯为中介/贸易商/外贸公司则为False")
    icp_score: int = Field(ge=1, le=10, description="与采购需求的匹配打分(1-10分)")
    disqualify_reason: Optional[str] = Field(default=None, description="淘汰理由（非工厂/品类不符）")
    core_competence: str = Field(description="核心代工优势/主打外贸产品总结（30字内）")
    clean_company_name: str = Field(description="清洗后的标准工商全称")


class EnrichedSupplierLead(RawSupplierLead):
    """最终入库线索实体"""
    external_website: Optional[str] = ""
    discovered_emails: List[str] = []
    whatsapp_numbers: List[str] = []
    is_factory: bool = False
    icp_score: int = 0
    core_competence: str = ""
    status: str = "QUALIFIED"  # QUALIFIED / DISQUALIFIED