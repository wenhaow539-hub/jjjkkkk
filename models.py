from typing import Optional
from pydantic import BaseModel, Field

class RawSupplierLead(BaseModel):
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
    card_product: str = ""

class LLMEvalResult(BaseModel):
    is_direct_factory: bool = Field(default=True, description="是否为源头实体工厂")
    icp_score: int = Field(default=5, ge=1, le=10, description="匹配打分(1-10分)")
    disqualify_reason: Optional[str] = Field(default=None, description="淘汰原因")
    core_competence: str = Field(default="", description="核心优势总结(30字内)")
    clean_company_name: str = Field(default="", description="清洗后的法定名称")

class QualifiedLead(BaseModel):
    company_name: str
    data_source: str = "Global Sources"
    registered_company: Optional[str] = ""
    registered_address: Optional[str] = ""
    contact_person: Optional[str] = ""
    contact_title: Optional[str] = ""
    phone: Optional[str] = ""
    email: Optional[str] = ""
    official_website: Optional[str] = ""
    police_record: Optional[str] = ""
    main_products: Optional[str] = ""
    icp_score: int = 0
    lead_level: str = "B"
    is_direct_factory: bool = True
    core_competence: str = ""