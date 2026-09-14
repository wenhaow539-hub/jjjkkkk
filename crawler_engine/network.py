"""API 接口发现：从真实页面流量中捕获 xhr / fetch / graphql / json 响应，归纳可重放的接口模板。

链路：Browser 发现 API → 保存 API 模板（templates.py）→ 以后 HTTP 批量调用。

用途：
- 页面渲染依赖 JS 的站点（如 Global Sources 检索页）用 HTML 解析很脆弱，
  发现其背后的 JSON 接口后，可以改走 http 模式直连接口，更快、更稳、请求更少；
- 这是从"浏览器爬取"渐进升级到"接口爬取"的过渡工具，不强制现有爬虫立刻改造。

职责边界：本模块只负责"发现"，模板的持久化与重放见 crawler_engine/templates.py。
浏览器由 Crawlee 统一接管：discover() 通过 CrawlerRunner 的 browser/browser-cdp 模式打开页面，
再用 pre_navigation_hook 挂上监听器（本模块不再自行创建 Playwright / browser）。

安全约定：
- 请求头按敏感词脱敏（cookie/authorization/token/... → "<redacted>"），非敏感自定义头保留（重放需要）；
- 响应只保存"结构"（key→类型），原始样本默认不落盘，需 store_sample=True 显式开启。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, quote_plus, urlparse, urlunparse

from utils.http_body import as_json, json_shape
from utils.logger import get_logger

logger = get_logger("engine.network")

_STATIC_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".woff", ".woff2", ".ttf", ".mp4", ".map",
)
_NUMERIC_SEGMENT = re.compile(r"^\d{2,}$")
_UUID_SEGMENT = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.I)
_HEX_SEGMENT = re.compile(r"^[0-9a-f]{16,}$", re.I)
_GQL_OP_RE = re.compile(r"\b(?:query|mutation|subscription)\s+([A-Za-z_]\w*)")

SENSITIVE_HEADER_HINTS = (
    "cookie", "authorization", "auth", "token", "secret", "api-key", "apikey",
    "session", "csrf", "xsrf", "signature",
)
"""头名命中其一即脱敏（宁可多脱不可漏脱；`auth` 也会命中 `:authority`，而伪头本就不参与重放）。"""

REDACTED = "<redacted>"


@dataclass
class ApiEndpoint:
    """一个被发现的接口（按 method + 模板 + graphql operation 聚合）。"""

    method: str
    url_template: str
    name: str = ""
    sample_url: str = ""
    status: int = 0
    content_type: str = ""
    count: int = 1
    first_seen: str = ""
    last_seen: str = ""

    # 请求侧
    headers: dict = field(default_factory=dict)
    redacted_headers: list = field(default_factory=list)
    query_params: dict = field(default_factory=dict)
    path_params: dict = field(default_factory=dict)
    body_kind: str = "none"
    body_template: Any = None
    graphql: dict | None = None

    # 响应侧
    response_type: str = ""
    response_keys: tuple = ()
    response_shape: Any = None
    response_graphql: bool = False
    sample: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def line(self) -> str:
        keys = ", ".join(self.response_keys[:8]) if self.response_keys else "-"
        gql = f" | gql={self.graphql.get('operation_name') or self.graphql.get('query_hash')}" if self.graphql else ""
        redacted = f" | 脱敏头={self.redacted_headers}" if self.redacted_headers else ""
        return (f"[{self.method}] {self.url_template}  (命中 {self.count} 次, {self.status}, {self.response_type}{gql}{redacted})"
                f"\n      └ 字段: {keys}")


class ApiDiscovery:
    """挂载到 Playwright Page 上，被动捕获接口流量并归纳模板。"""

    def __init__(
        self,
        *,
        include_keywords: Iterable[str] | None = None,
        ignore_keywords: Iterable[str] | None = None,
        sample_keys: int = 15,
        max_body_chars: int = 400_000,
        store_sample: bool = False,
        shape_depth: int = 3,
    ) -> None:
        self._include = tuple(k.lower() for k in (include_keywords or ()))
        self._ignore = tuple(
            k.lower() for k in (ignore_keywords or ("analytics", "track", "beacon", "sentry", "promotion/ad"))
        )
        self._sample_keys = sample_keys
        self._max_body_chars = max_body_chars
        self._store_sample = store_sample
        self._shape_depth = shape_depth
        self._endpoints: dict[tuple, ApiEndpoint] = {}
        self._raw_requests: list[dict] = []

    # ------------------------------------------------------------------ #
    # 挂载 / 卸载
    # ------------------------------------------------------------------ #
    def attach(self, page) -> None:
        try:
            page.on("response", self._on_response)
            logger.debug("[network] 已挂载接口发现监听器")
        except Exception as e:
            logger.warning(f"[network] 挂载监听器失败: {e!r}")

    def detach(self, page) -> None:
        try:
            page.remove_listener("response", self._on_response)
        except Exception:  # 部分版本签名差异，忽略即可
            pass

    def attach_hook(self):
        """生成 Crawlee pre_navigation_hook：导航前挂监听器（浏览器由 Crawlee 管）。"""

        async def _hook(context) -> None:
            page = getattr(context, "page", None)
            if page is not None:
                self.attach(page)

        return _hook

    # ------------------------------------------------------------------ #
    # 捕获
    # ------------------------------------------------------------------ #
    async def _on_response(self, response) -> None:
        try:
            request = response.request
            url = response.url
            if url.lower().endswith(_STATIC_SUFFIXES):
                return
            content_type = self._header(response.headers, "content-type")
            resource_type = getattr(request, "resource_type", "")
            if not (resource_type in ("xhr", "fetch") or "json" in content_type.lower()):
                return

            low = url.lower()
            if self._ignore and any(k in low for k in self._ignore):
                return
            if self._include and not any(k in low for k in self._include):
                return

            body_json = None
            body_text = self._request_body_text(request)
            body_kind, body_template = self._parse_body(body_text, self._header(request.headers, "content-type"))
            graphql = self._detect_graphql(url, body_template)
            if graphql is not None:
                body_kind = "graphql"

            headers, redacted = self.sanitize_headers(getattr(request, "headers", {}) or {})
            url_template = self.to_template(url)
            path_params = self.path_params_of(url, url_template)
            query_params = {k: {"example": v} for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True)}

            response_json = None
            try:
                response_json = await response.json()
            except Exception:
                response_json = None
            body_json = response_json
            response_type = self._response_type(content_type, body_json)

            now = datetime.now().isoformat(timespec="seconds")
            endpoint = ApiEndpoint(
                method=request.method,
                url_template=url_template,
                name=self.template_name(request.method, url_template, graphql),
                sample_url=url,
                status=response.status,
                content_type=content_type,
                first_seen=now,
                last_seen=now,
                headers=headers,
                redacted_headers=redacted,
                query_params=query_params,
                path_params=path_params,
                body_kind=body_kind,
                body_template=body_template,
                graphql=graphql,
                response_type=response_type,
                response_keys=self._extract_keys(body_json),
                response_shape=json_shape(body_json, max_depth=self._shape_depth) if body_json is not None else None,
                response_graphql=bool(graphql),
                sample=self._sample_of(body_text),
            )
            self._merge(endpoint)
            self._raw_requests.append(
                {
                    "name": endpoint.name,
                    "method": request.method,
                    "url": url,
                    "status": response.status,
                    "resource_type": resource_type,
                    "content_type": content_type,
                    "body_kind": body_kind,
                    "headers": headers,
                    "redacted_headers": redacted,
                }
            )
        except Exception as e:  # 捕获失败绝不能影响主流程
            logger.debug(f"[network] 响应捕获异常(忽略): {e!r}")

    # ------------------------------------------------------------------ #
    # 解析辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _header(headers: Mapping | None, key: str) -> str:
        if headers is None:
            return ""
        try:
            return str(headers.get(key, "") or "")
        except Exception:
            return ""

    @staticmethod
    def _request_body_text(request) -> str:
        for attr in ("post_data", "post_data_json"):
            value = getattr(request, attr, None)
            if isinstance(value, str) and value:
                return value
        value = getattr(request, "post_data", None)
        return value if isinstance(value, str) else ""

    @staticmethod
    def _parse_body(body_text: str, content_type: str) -> tuple[str, Any]:
        """解析请求体：json / form / 原样字符串。"""
        if not body_text:
            return "none", None
        lowered = content_type.lower()
        stripped = body_text.lstrip()
        if "json" in lowered or stripped[:1] in ("{", "["):
            parsed = as_json(body_text)
            if parsed is not None:
                return "json", parsed
        if "form" in lowered or "=" in body_text:
            pairs = parse_qsl(body_text, keep_blank_values=True)
            if pairs:
                return "form", dict(pairs)
        return "raw", body_text

    @staticmethod
    def _detect_graphql(url: str, body_template: Any) -> Optional[dict]:
        """graphql 判定：URL 含 graphql，或请求体含 query/operationName/variables。"""
        url_is_gql = "graphql" in (url or "").lower()
        if isinstance(body_template, Mapping) and ({"query", "operationName", "variables"} & set(body_template)):
            query = str(body_template.get("query") or "")
            variables = body_template.get("variables")
            extensions = body_template.get("extensions") or {}
            persisted = ""
            if isinstance(extensions, Mapping):
                persisted = str((extensions.get("persistedQuery") or {}).get("sha256Hash") or "")
            operation = str(body_template.get("operationName") or "")
            if not operation and query:
                match = _GQL_OP_RE.search(query)
                operation = match.group(1) if match else ""
            return {
                "operation_name": operation,
                "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest()[:12] if query else "",
                "variables_keys": sorted(variables.keys()) if isinstance(variables, Mapping) else [],
                "persisted_query": persisted,
            }
        if url_is_gql:
            return {"operation_name": "", "query_hash": "", "variables_keys": [], "persisted_query": ""}
        return None

    @staticmethod
    def sanitize_headers(headers: Mapping | None) -> tuple[dict, list]:
        """请求头脱敏：丢弃 HTTP/2 伪头，敏感头值置为 <redacted>。"""
        clean: dict = {}
        redacted: list = []
        for key, value in (headers or {}).items():
            name = str(key)
            lowered = name.lower()
            if lowered.startswith(":"):
                continue
            if any(hint in lowered for hint in SENSITIVE_HEADER_HINTS):
                clean[name] = REDACTED
                redacted.append(name)
            else:
                clean[name] = str(value)
        return clean, sorted(redacted)

    @staticmethod
    def _response_type(content_type: str, body_json: Any) -> str:
        lowered = (content_type or "").lower()
        if "json" in lowered:
            return "json"
        if "html" in lowered:
            return "html"
        if "text" in lowered or "xml" in lowered:
            return "text"
        if body_json is not None:
            return "json"
        return "binary" if lowered else "unknown"

    def _sample_of(self, body_text: str) -> str:
        if not self._store_sample or not body_text:
            return ""
        return body_text[: self._max_body_chars][:400]

    def _merge(self, endpoint: ApiEndpoint) -> None:
        key = (endpoint.method, endpoint.url_template, (endpoint.graphql or {}).get("operation_name", ""))
        existing = self._endpoints.get(key)
        if existing is None:
            self._endpoints[key] = endpoint
            gql = ""
            if endpoint.graphql:
                gql = f" (graphql: {endpoint.graphql.get('operation_name') or endpoint.graphql.get('query_hash')})"
            logger.info(f"🔍 [network] 发现接口 [{endpoint.method}] {endpoint.url_template}{gql}")
            return
        existing.count += 1
        existing.last_seen = endpoint.last_seen
        if not existing.response_keys and endpoint.response_keys:
            existing.response_keys = endpoint.response_keys
            existing.response_shape = endpoint.response_shape
        if not existing.response_type and endpoint.response_type:
            existing.response_type = endpoint.response_type

    def _extract_keys(self, body: Any) -> tuple:
        def keys_of(obj: Any) -> tuple:
            if isinstance(obj, dict):
                return tuple(str(k) for k in list(obj.keys())[: self._sample_keys])
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                return tuple(str(k) for k in list(obj[0].keys())[: self._sample_keys])
            return ()

        keys = keys_of(body)
        if not keys and isinstance(body, dict):
            for value in body.values():
                keys = keys_of(value)
                if keys:
                    break
        return keys

    # ------------------------------------------------------------------ #
    # 模板归一
    # ------------------------------------------------------------------ #
    @staticmethod
    def to_template(url: str) -> str:
        """把具体 URL 归一成模板：数字/UUID/长 hex 段 -> {id}，查询值 -> {value}。

        注意：查询串要手工拼接而不是用 urlencode —— 后者会把占位符的 `{}` 百分号编码成
        `%7Bvalue%7D`，导致模板不可读、也难以再做字符串匹配。
        """
        parsed = urlparse(url)
        segments = []
        for seg in parsed.path.split("/"):
            if not seg:
                segments.append(seg)
            elif _NUMERIC_SEGMENT.match(seg) or _UUID_SEGMENT.match(seg) or _HEX_SEGMENT.match(seg):
                segments.append("{id}")
            else:
                segments.append(seg)
        query = "&".join(
            f"{quote_plus(k)}={{value}}" for k, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
        return urlunparse((parsed.scheme, parsed.netloc, "/".join(segments), "", query, ""))

    @staticmethod
    def path_params_of(url: str, url_template: str) -> dict:
        """从模板占位与真实 URL 的差异中推出 path 参数示例。"""
        real_segments = urlparse(url).path.split("/")
        tpl_segments = urlparse(url_template).path.split("/")
        if len(real_segments) != len(tpl_segments):
            return {}
        examples: dict = {}
        for real, tpl in zip(real_segments, tpl_segments):
            if tpl == "{id}" and real:
                current = examples.get("id")
                if current is None:
                    examples["id"] = {"example": real}
                elif isinstance(current, Mapping) and current.get("example") != real:
                    examples["id"] = [current["example"], real]
        return examples

    @staticmethod
    def template_name(method: str, url_template: str, graphql: Mapping | None = None) -> str:
        parsed = urlparse(url_template)
        base = f"{parsed.netloc}{parsed.path}"
        if parsed.query:
            base += "?" + ",".join(sorted(k for k, _ in parse_qsl(parsed.query, keep_blank_values=True)))
        if graphql:
            suffix = graphql.get("operation_name") or graphql.get("query_hash") or "gql"
            base += f"#{suffix}"
        return f"{method.lower()}.{base}"

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #
    @property
    def endpoints(self) -> list[ApiEndpoint]:
        return sorted(self._endpoints.values(), key=lambda e: e.count, reverse=True)

    def report(self) -> str:
        if not self._endpoints:
            return "（未发现 JSON 接口）"
        return "\n".join(f"    {e.line()}" for e in self.endpoints)

    def save(self, output_path: str | Path) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "store_sample": self._store_sample,
            "endpoints": [e.to_dict() for e in self.endpoints],
            "requests": self._raw_requests,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"💾 [network] 接口清单已保存: {path}")
        return path

    def templates(self) -> list:
        """把发现结果转成可重放的 ApiTemplate 列表。"""
        from crawler_engine.templates import build_template_from_capture

        result = []
        for endpoint in self.endpoints:
            response_info = {
                "type": endpoint.response_type,
                "keys": list(endpoint.response_keys),
                "shape": endpoint.response_shape,
                "content_type": endpoint.content_type,
                "graphql": endpoint.response_graphql,
            }
            result.append(
                build_template_from_capture(
                    {
                        "name": endpoint.name or endpoint.url_template,
                        "method": endpoint.method,
                        "url_template": endpoint.url_template,
                        "query_params": endpoint.query_params,
                        "path_params": endpoint.path_params,
                        "headers": endpoint.headers,
                        "redacted_headers": endpoint.redacted_headers,
                        "body_kind": endpoint.body_kind,
                        "body_template": endpoint.body_template,
                        "graphql": endpoint.graphql,
                        "hits": endpoint.count,
                        "first_seen": endpoint.first_seen,
                        "last_seen": endpoint.last_seen,
                        "sample_request": {
                            "method": endpoint.method,
                            "url": endpoint.sample_url,
                            "status": endpoint.status,
                        },
                    },
                    response_info=response_info,
                )
            )
        return result

    def save_templates(self, path: str | Path, *, source: Mapping[str, Any] | None = None) -> Path:
        from crawler_engine.templates import save_templates

        return save_templates(self.templates(), path, source=source)

    # ------------------------------------------------------------------ #
    # 主动发现（浏览器由 Crawlee 统一接管）
    # ------------------------------------------------------------------ #
    async def discover(
        self,
        url: str,
        *,
        cdp_url: Optional[str] = None,
        headless: Optional[bool] = None,
        wait_ms: int = 2500,
        scroll: bool = True,
        output_path: Optional[str] = None,
        template_path: Optional[str] = None,
        mode: Optional[str] = None,
        config=None,
    ) -> list[ApiEndpoint]:
        """打开目标页并被动监听接口流量；浏览器生命周期由 Crawlee 负责。

        有 cdp_url 时走 browser-cdp（复用已登录的 Chrome），否则用托管浏览器 + 无头。
        """
        from crawler_engine.config import EngineConfig
        from crawler_engine.runner import CrawlerRunner

        base_config = config or EngineConfig.from_env()
        resolved_mode = mode or ("browser-cdp" if (cdp_url or base_config.cdp_url) else "browser")
        overrides: dict[str, Any] = {"mode": resolved_mode}
        if cdp_url:
            overrides["cdp_url"] = cdp_url
        if headless is not None:
            overrides["headless"] = headless
        elif resolved_mode == "browser":
            overrides["headless"] = True  # 采集接口不需要人看
        run_config = base_config.with_(**overrides)

        async def scroll_and_wait(context) -> None:
            page = getattr(context, "page", None)
            if page is None:
                return
            if scroll:
                for offset in (600, 1200, 1800):
                    try:
                        await page.mouse.wheel(0, offset)
                        await page.wait_for_timeout(400)
                    except Exception:
                        break
            if wait_ms:
                await page.wait_for_timeout(wait_ms)

        logger.info(f"🌐 [network] 采样页面: {url} | 模式={resolved_mode}")
        runner = CrawlerRunner(run_config)
        await runner.run(
            scroll_and_wait,
            [url],
            mode=resolved_mode,
            pre_navigation_hooks=[self.attach_hook()],
        )

        logger.info(f"🧭 [network] 接口发现完成，共 {len(self.endpoints)} 个候选接口：")
        for line in self.report().splitlines():
            logger.info(line)
        if output_path:
            self.save(output_path)
        if template_path:
            self.save_templates(template_path, source={"sample_url": url, "mode": resolved_mode})
        return self.endpoints


# 便捷 re-export：模板的持久化与重放同属"接口发现"链路，从这里也能直接取到
from crawler_engine.templates import (  # noqa: E402
    ApiTemplate,
    ApiTemplateRunner,
    RenderedRequest,
    ReplayReport,
    load_templates,
    save_templates,
)

__all__ = [
    "ApiDiscovery",
    "ApiEndpoint",
    "ApiTemplate",
    "ApiTemplateRunner",
    "RenderedRequest",
    "ReplayReport",
    "load_templates",
    "save_templates",
]
