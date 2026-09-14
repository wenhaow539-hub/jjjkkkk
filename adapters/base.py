"""Adapter 模式统一接口。

分层设计（这是本模块存在的意义）：
    handle(context)  —— "怎么取"：把 Crawlee 的各种上下文（浏览器页 / HTTP 响应 / BS4 soup）
                        归一成 AdapterResponse，不关心业务字段；
    parse(response)  —— "怎么解"：纯解析逻辑，只依赖 AdapterResponse，
                        因此可以脱离网络与浏览器离线单测（把页面 HTML 存下来直接喂进来）。

渐进式迁移：现有爬虫可以先只实现 start_urls() + parse()，其余交给底座；
需要浏览器上下文的适配器声明 requires_browser = True 即可。

最小示例：
    class DemoAdapter(BaseAdapter):
        platform_name = "Demo"

        def start_urls(self):
            return ["https://example.com/list"]

        def parse(self, response):
            return [{"company": t} for t in re.findall(r"<h2>(.*?)</h2>", response.html)]
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from utils.logger import get_logger

logger = get_logger("adapter")


@dataclass
class AdapterResponse:
    """归一化后的响应对象：浏览器 / HTTP / BS4 三种来源统一成这一个结构。"""

    url: str = ""
    final_url: str = ""
    status: int = 0
    html: str = ""
    json_data: Any = None
    headers: dict = field(default_factory=dict)
    soup: Any = None
    page: Any = None
    session_id: str = ""
    source: str = ""
    meta: dict = field(default_factory=dict)
    """随请求携带的元信息（来自 Request.user_data / label），用于区分页面类型、承载 company_key 等。

    声明为普通 dict，因此 parse() 可以完全离线构造 AdapterResponse 做单测：
        AdapterResponse(html=..., meta={"page_type": "company"})
    """

    @property
    def text(self) -> str:
        """html 的别名（兼容习惯写法）。"""
        return self.html

    @property
    def ok(self) -> bool:
        if self.source == "browser":
            return self.page is not None
        return 200 <= self.status < 400

    def brief(self) -> str:
        return f"{self.status or '-'} {self.source} {len(self.html)}B {self.final_url or self.url}"


class BaseAdapter:
    """所有平台适配器的基类（统一接口：start_urls / handle / parse）。"""

    platform_name: str = "Base"
    # 需要 context.page 的适配器请覆盖为 True：CrawlRunner 会据此避免误用 http/soup 模式
    requires_browser: bool = False
    # 可选：声明该适配器偏好的执行模式（http / soup / browser / browser-cdp），None = 跟随全局配置
    engine_mode: Optional[str] = None
    # 可选：把解析结果同时推送到 Crawlee Dataset（便于在 Web UI / 存储目录中查看）
    push_to_dataset: bool = False

    def __init__(self) -> None:
        self.items: list = []
        self.errors: list = []
        self.pages_handled: int = 0

    # ------------------------------------------------------------------ #
    # 必须实现
    # ------------------------------------------------------------------ #
    def start_urls(self) -> Sequence[str]:
        """起始 URL 列表（同步返回即可；返回协程也会被 CrawlRunner 等待）。"""
        raise NotImplementedError(f"{self.__class__.__name__} 必须实现 start_urls()")

    # ------------------------------------------------------------------ #
    # 默认实现：取内容 -> 解析 -> 收集
    # ------------------------------------------------------------------ #
    async def handle(self, context) -> None:
        """请求处理器（默认实现，一般无需覆盖）。

        覆盖场景：需要多步骤交互（翻页、点击、等接口返回）时再重写，
        并仍然把结果交给 self.add_items() 以便底座统一收集。
        """
        response = await self.build_response(context)
        self.pages_handled += 1

        if type(self).parse is BaseAdapter.parse:
            logger.warning(
                f"⚠️ [{self.platform_name}] {self.__class__.__name__} 既未覆盖 handle() 也未实现 parse()，"
                f"本次不会产出任何数据。"
            )
            return

        parsed = self.parse(response)
        if parsed:
            self.add_items(parsed)

        if self.push_to_dataset and parsed:
            push = getattr(context, "push_data", None)
            if push is not None:
                try:
                    await push(parsed)
                except Exception as e:
                    logger.debug(f"[{self.platform_name}] push_data 失败(忽略): {e!r}")

    def parse(self, response: AdapterResponse):
        """解析逻辑（建议实现为纯函数：只依赖 response，便于离线单测）。

        返回：None / 单条记录 / 记录列表 均可，骨架阶段返回空列表即可。
        """
        return None

    # ------------------------------------------------------------------ #
    # 响应归一化
    # ------------------------------------------------------------------ #
    async def build_response(self, context) -> AdapterResponse:
        """把 Crawlee 上下文归一成 AdapterResponse（浏览器 / HTTP / soup 三种来源）。"""
        request = getattr(context, "request", None)
        request_url = getattr(request, "url", "") if request is not None else ""
        session = getattr(context, "session", None)
        session_id = getattr(session, "id", "") if session is not None else ""

        # 把 Request 上承载的元信息（user_data / label）透传给 parse()，
        # 使 parse 能区分页面类型，同时保持纯解析、可离线构造。
        # 注意：crawlee 的 request.user_data 是 pydantic 模型 UserData（实现了 Mapping），
        # 并不是普通 dict —— 用 isinstance(x, dict) 判断会漏掉它，必须按 Mapping 处理。
        meta: dict = {}
        user_data = getattr(request, "user_data", None) if request is not None else None
        if isinstance(user_data, Mapping):
            meta.update({k: v for k, v in dict(user_data).items() if k != "crawlee_data"})
        elif isinstance(user_data, dict):
            meta.update(user_data)
        label = getattr(request, "label", None) if request is not None else None
        if label:
            meta.setdefault("label", label)
            meta.setdefault("page_type", label)

        response = AdapterResponse(
            url=request_url,
            final_url=request_url,
            session_id=session_id,
            meta=meta,
        )

        # 1) 浏览器上下文
        page = getattr(context, "page", None)
        if page is not None:
            response.source = "browser"
            response.page = page
            try:
                response.html = await page.content()
                response.final_url = getattr(page, "url", request_url) or request_url
                response.status = 200
            except Exception as e:
                logger.warning(f"      ⚠️ [{self.platform_name}] 读取页面内容失败: {e!r}")
            return response

        # 2) BS4 上下文
        soup = getattr(context, "soup", None)
        if soup is not None:
            response.source = "soup"
            response.soup = soup
            response.html = str(soup)
            http_response = getattr(context, "http_response", None)
            response.status = getattr(http_response, "status_code", 200) or 200

        # 3) HTTP 上下文
        http_response = getattr(context, "http_response", None)
        if http_response is not None:
            response.source = response.source or "http"
            response.status = getattr(http_response, "status_code", 0) or 0
            response.headers = dict(getattr(http_response, "headers", {}) or {})
            if not response.html:
                response.html = await self._read_http_body(http_response)

        if not response.html:
            logger.debug(f"      [{self.platform_name}] 响应为空: {request_url}")

        return response

    @staticmethod
    async def _read_http_body(http_response) -> str:
        """读取响应体并解码（委托 utils.http_body，引擎与适配器共用同一实现）。"""
        from utils.http_body import decode_response_body

        return await decode_response_body(http_response)

    # ------------------------------------------------------------------ #
    # 结果收集
    # ------------------------------------------------------------------ #
    def add_items(self, parsed: Any) -> int:
        """收集解析结果：支持 None / 单条 / 可迭代，返回本次新增条数。"""
        if parsed is None:
            return 0
        records: Iterable
        if isinstance(parsed, (list, tuple, set)):
            records = parsed
        elif isinstance(parsed, dict):
            records = [parsed]
        elif hasattr(parsed, "__iter__") and not isinstance(parsed, (str, bytes)):
            records = list(parsed)
        else:
            records = [parsed]

        added = 0
        for record in records:
            if record is None:
                continue
            self.items.append(record)
            added += 1
        if added:
            logger.info(f"      📥 [{self.platform_name}] 本页新增 {added} 条，累计 {len(self.items)} 条")
        return added

    def to_leads(self) -> list:
        """把 items 中的 dict 转换为 RawSupplierLead（字段不匹配的原样保留）。"""
        from models import RawSupplierLead

        leads = []
        for item in self.items:
            if isinstance(item, RawSupplierLead):
                leads.append(item)
            elif isinstance(item, dict):
                try:
                    leads.append(RawSupplierLead(**{k: v for k, v in item.items()
                                                    if k in RawSupplierLead.model_fields}))
                except Exception:
                    leads.append(item)
            else:
                leads.append(item)
        return leads

    def reset(self) -> None:
        self.items = []
        self.errors = []
        self.pages_handled = 0

    def flush(self) -> int:
        """爬取结束时的补出钩子（默认无操作）。

        需要跨页面累积的适配器（例如"列表页找到候选 + 详情页补字段"）可在此
        输出未凑齐的累积记录，由 CrawlRunner 在 crawl 结束后调用。
        返回补出的记录条数。
        """
        return 0

    def summary(self) -> str:
        parts = [f"adapter={self.__class__.__name__}", f"处理页数={self.pages_handled}", f"记录数={len(self.items)}"]
        if self.errors:
            parts.append(f"错误={len(self.errors)}")
        return " | ".join(parts)
