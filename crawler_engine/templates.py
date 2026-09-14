"""API 模板：可持久化、可重放的接口描述 + HTTP 批量调用。

链路（对应需求 8）：Browser 发现 API → 保存 API 模板 → 以后 HTTP 批量调用。

分工：
    network.py    负责"发现"（监听浏览器流量，产出 ApiTemplate）
    templates.py  负责"保存与重放"（本模块）

重放两条通路（用户已确认）：
    via="crawlee"（默认）—— 模板转成 crawlee Request 入队，由 HttpCrawler 执行，
                            队列 / 重试 / 会话 / 并发 / 限速 / robots 全部由 Crawlee 接管；
    via="fetcher"        —— 用引擎自带 AsyncFetcher 并发调用，轻量、直接拿 JSON。

安全约定：
    - 模板里的敏感头（cookie/authorization/token/...）一律为 "<redacted>"，重放时需调用方显式补全；
    - 只在 store_response_sample 打开时才落响应样本，否则只保存结构（utils.http_body.json_shape）。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from crawler_engine.config import EngineConfig
from utils.http_body import as_json, decode_response_body, json_shape
from utils.logger import get_logger

logger = get_logger("engine.templates")

TEMPLATE_SCHEMA_VERSION = 1
REDACTED = "<redacted>"

BODY_KIND_NONE = "none"
BODY_KIND_JSON = "json"
BODY_KIND_FORM = "form"
BODY_KIND_GRAPHQL = "graphql"

_CONTENT_TYPES = {
    BODY_KIND_JSON: "application/json",
    BODY_KIND_GRAPHQL: "application/json",
    BODY_KIND_FORM: "application/x-www-form-urlencoded",
}


@dataclass
class RenderedRequest:
    """一次可直接发送的请求。"""

    method: str
    url: str
    headers: dict = field(default_factory=dict)
    payload: str | None = None
    body_kind: str = BODY_KIND_NONE
    graphql: dict | None = None
    template_name: str = ""

    def brief(self) -> str:
        body = f" payload={len(self.payload)}B" if self.payload else ""
        return f"[{self.method}] {self.url}{body}"


@dataclass
class ApiTemplate:
    """一个可重放的接口模板（含占位符与脱敏信息）。"""

    name: str
    method: str
    url_template: str
    query_params: dict = field(default_factory=dict)
    path_params: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    redacted_headers: list = field(default_factory=list)
    body_kind: str = BODY_KIND_NONE
    body_template: Any = None
    graphql: dict | None = None
    response: dict = field(default_factory=dict)
    hits: int = 1
    first_seen: str = ""
    last_seen: str = ""
    sample_request: dict = field(default_factory=dict)
    """原始请求留档（同样经过脱敏），用于"模板是否忠实"的验证。"""

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def render(
        self,
        *,
        query_params: Mapping[str, Any] | None = None,
        path_params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> RenderedRequest:
        """把模板渲染成具体请求。

        无参调用 = 用模板里的 example 复现原始请求，可直接用于"模板是否可用"的自检。
        """
        url = self._render_url(query_params=query_params, path_params=path_params)
        merged_headers = self._render_headers(headers)
        payload = None

        if self.body_kind != BODY_KIND_NONE:
            rendered_body = self._render_body(body)
            merged_headers.setdefault("content-type", _CONTENT_TYPES.get(self.body_kind, "application/json"))
            if self.body_kind == BODY_KIND_FORM and isinstance(rendered_body, dict):
                payload = urlencode([(k, "" if v is None else v) for k, v in rendered_body.items()])
            else:
                payload = json.dumps(rendered_body, ensure_ascii=False)

        return RenderedRequest(
            method=self.method.upper(),
            url=url,
            headers=merged_headers,
            payload=payload,
            body_kind=self.body_kind,
            graphql=self.graphql,
            template_name=self.name,
        )

    def _render_url(self, *, query_params: Mapping[str, Any] | None, path_params: Mapping[str, Any] | None) -> str:
        parsed = urlparse(self.url_template)

        # 路径占位：按出现顺序用 path_params 的 example/覆盖值替换
        replacements: list[str] = []
        for key, meta in self.path_params.items():
            value = None
            if path_params and key in path_params:
                value = str(path_params[key])
            elif isinstance(meta, Mapping) and meta.get("example") is not None:
                value = str(meta["example"])
            elif isinstance(meta, list) and meta:
                value = str(meta[0])
            if value is not None:
                replacements.append(value)

        path = parsed.path
        for value in replacements:
            path = path.replace("{id}", value, 1)
        path = path.replace("{id}", "")
        path = path.replace("{value}", "")

        # 查询占位：按 key 覆盖，未覆盖用 example
        query_items = []
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            meta = self.query_params.get(key)
            value = None
            if query_params and key in query_params:
                value = query_params[key]
            elif isinstance(meta, Mapping) and meta.get("example") is not None:
                value = meta["example"]
            elif meta is not None:
                value = meta
            query_items.append((key, "" if value is None else str(value)))

        return urlunparse((parsed.scheme, parsed.netloc, path, "", urlencode(query_items), ""))

    def _render_headers(self, override: Mapping[str, str] | None) -> dict:
        headers = {}
        for key, value in (self.headers or {}).items():
            if value == REDACTED:
                continue  # 敏感头不落盘、也不默认带上
            headers[key] = value
        if override:
            headers.update({k: str(v) for k, v in override.items()})
        return headers

    def _render_body(self, override: Any) -> Any:
        template_body = self.body_template
        if self.body_kind == BODY_KIND_GRAPHQL and isinstance(template_body, Mapping):
            merged = dict(template_body)
            if isinstance(override, Mapping):
                if "variables" in override and isinstance(template_body.get("variables"), Mapping):
                    merged["variables"] = {**template_body["variables"], **override["variables"]}
                merged.update({k: v for k, v in override.items() if k != "variables"})
            return merged
        if isinstance(template_body, Mapping):
            merged = dict(template_body)
            if isinstance(override, Mapping):
                merged.update(override)
            return merged
        return override if override is not None else template_body

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApiTemplate":
        allowed = {f for f in cls.__dataclass_fields__}  # noqa: SLF001
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def line(self) -> str:
        extra = ""
        if self.graphql:
            extra = f" | gql={self.graphql.get('operation_name') or self.graphql.get('query_hash')}"
        return (f"[{self.method}] {self.url_template} (命中 {self.hits}, type={self.response.get('type')}{extra})"
                f"\n      参数: query={list(self.query_params)} path={list(self.path_params)}"
                f" | 脱敏头={self.redacted_headers or '无'}")


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #
def save_templates(templates: Iterable[ApiTemplate], path: str | Path, *, source: Mapping[str, Any] | None = None) -> Path:
    """把模板写入版本化 JSON（默认已脱敏、默认不含响应样本）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": TEMPLATE_SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": dict(source or {}),
        "templates": [t.to_dict() for t in templates],
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"💾 [templates] 已保存 {len(payload['templates'])} 个接口模板: {target}")
    return target


def load_templates(path: str | Path) -> list[ApiTemplate]:
    """读取模板文件；版本不匹配时明确报错，不静默降级。"""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"模板文件不存在: {src}")
    data = json.loads(src.read_text(encoding="utf-8"))
    version = data.get("version")
    if version != TEMPLATE_SCHEMA_VERSION:
        raise ValueError(
            f"模板 schema 版本不匹配: 文件={version} 期望={TEMPLATE_SCHEMA_VERSION}。"
            f"请用当前版本重新执行 --discover 生成模板。"
        )
    templates = [ApiTemplate.from_dict(item) for item in data.get("templates", [])]
    logger.info(f"📥 [templates] 已加载 {len(templates)} 个接口模板: {src}")
    return templates


def build_template_from_capture(capture: Mapping[str, Any], *, response_info: Mapping[str, Any] | None = None) -> ApiTemplate:
    """把 network.py 的捕获结果转成可重放模板（供 ApiDiscovery 使用）。"""
    return ApiTemplate(
        name=str(capture.get("name") or capture.get("url_template") or "unnamed"),
        method=str(capture.get("method") or "GET"),
        url_template=str(capture.get("url_template") or ""),
        query_params=dict(capture.get("query_params") or {}),
        path_params=dict(capture.get("path_params") or {}),
        headers=dict(capture.get("headers") or {}),
        redacted_headers=list(capture.get("redacted_headers") or []),
        body_kind=str(capture.get("body_kind") or BODY_KIND_NONE),
        body_template=capture.get("body_template"),
        graphql=capture.get("graphql"),
        response=dict(response_info or {}),
        hits=int(capture.get("hits") or 1),
        first_seen=str(capture.get("first_seen") or ""),
        last_seen=str(capture.get("last_seen") or ""),
        sample_request=dict(capture.get("sample_request") or {}),
    )


# --------------------------------------------------------------------------- #
# 批量重放
# --------------------------------------------------------------------------- #
@dataclass
class TemplateReplayStat:
    name: str
    method: str
    url_template: str
    requests: int = 0
    ok: int = 0
    failed: int = 0
    statuses: dict = field(default_factory=dict)
    total_ms: float = 0.0
    record_counts: list = field(default_factory=list)
    sample_records: list = field(default_factory=list)
    note: str = ""

    def line(self) -> str:
        avg = (self.total_ms / self.requests) if self.requests else 0.0
        note = f" | {self.note}" if self.note else ""
        return (f"[{self.method}] {self.url_template}\n"
                f"        请求 {self.requests} 成功 {self.ok} 失败 {self.failed} "
                f"平均 {avg:.0f}ms 状态 {self.statuses}{note}")


@dataclass
class ReplayReport:
    via: str
    templates: int = 0
    requests: int = 0
    ok: int = 0
    failed: int = 0
    duration_s: float = 0.0
    stats: list = field(default_factory=list)
    output_path: str | None = None

    def summary(self) -> str:
        return (f"via={self.via} | 模板 {self.templates} | 请求 {self.requests} "
                f"(成功 {self.ok} / 失败 {self.failed}) | 耗时 {self.duration_s:.1f}s")

    def report(self) -> str:
        lines = [self.summary()]
        for stat in self.stats:
            lines.append("    " + stat.line())
        return "\n".join(lines)


class ApiTemplateRunner:
    """按模板批量调用接口（两条通路：crawlee / fetcher）。"""

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig.from_env()

    def render_all(
        self,
        templates: Sequence[ApiTemplate],
        *,
        batches: Sequence[Mapping[str, Any]] | None = None,
        limit: int | None = None,
    ) -> list[RenderedRequest]:
        """把模板 × 批次渲染成请求列表（batches 为空时每个模板各渲染一次原始请求）。"""
        rendered: list[RenderedRequest] = []
        for template in templates:
            for batch in (batches or [{}]):
                rendered.append(template.render(**dict(batch)))
                if limit and len(rendered) >= limit:
                    return rendered
        return rendered

    async def replay(
        self,
        templates: Sequence[ApiTemplate],
        *,
        via: str = "crawlee",
        batches: Sequence[Mapping[str, Any]] | None = None,
        limit: int | None = None,
        output_path: str | None = None,
    ) -> ReplayReport:
        templates = list(templates)
        rendered = self.render_all(templates, batches=batches, limit=limit)
        if not rendered:
            return ReplayReport(via=via, templates=len(templates))

        stats = {t.name: TemplateReplayStat(name=t.name, method=t.method.upper(), url_template=t.url_template)
                 for t in templates}
        for template in templates:
            if template.graphql and template.graphql.get("persisted_query"):
                stats[template.name].note = "GraphQL persisted query：重放需提供相同 hash 与 variables（部分支持）"

        started = time.perf_counter()
        # 重放同样服从 robots.txt 声明的 Crawl-delay（两条通路都走这里的 config）
        if rendered:
            from crawler_engine.runner import CrawlerRunner

            self.config = await CrawlerRunner(self.config).resolve_pacing(
                self.config, [rendered[0].url]
            )
        if via == "crawlee":
            results = await self._replay_via_crawlee(rendered)
        elif via == "fetcher":
            results = await self._replay_via_fetcher(rendered)
        else:
            raise ValueError(f"未知重放通路: {via}（可选 crawlee / fetcher）")
        duration = time.perf_counter() - started

        lines = []
        for req, status_code, ok, records, elapsed_ms, error in results:
            stat = stats.setdefault(
                req.template_name,
                TemplateReplayStat(name=req.template_name, method=req.method, url_template=req.url),
            )
            stat.requests += 1
            stat.total_ms += elapsed_ms
            stat.statuses[str(status_code)] = stat.statuses.get(str(status_code), 0) + 1
            if ok:
                stat.ok += 1
            else:
                stat.failed += 1
                if error:
                    stat.note = stat.note or error
            if records is not None:
                count = len(records) if isinstance(records, (list, tuple)) else 1
                stat.record_counts.append(count)
                if len(stat.sample_records) < 3:
                    stat.sample_records.append(records if count <= 3 else list(records)[:3])
            lines.append({
                "template": req.template_name, "method": req.method, "url": req.url,
                "status": status_code, "ok": ok, "records": stat.record_counts[-1] if records is not None else 0,
                "elapsed_ms": round(elapsed_ms, 1), "error": error,
            })

        report = ReplayReport(
            via=via,
            templates=len(templates),
            requests=len(results),
            ok=sum(1 for r in results if r[2]),
            failed=sum(1 for r in results if not r[2]),
            duration_s=duration,
            stats=list(stats.values()),
            output_path=output_path,
        )
        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("w", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            logger.info(f"💾 [templates] 重放结果已写入: {out}")

        logger.info(f"🔁 [templates] 批量重放完成: {report.summary()}")
        return report

    # ------------------------------------------------------------------ #
    # 通路实现
    # ------------------------------------------------------------------ #
    async def _replay_via_crawlee(self, rendered: Sequence[RenderedRequest]):
        """走 Crawlee http 模式：队列 / 重试 / 会话 / 并发 / 限速 / robots 全由 Crawlee 接管。"""
        from crawlee import Request

        from crawler_engine.runner import CrawlerRunner

        requests = []
        for index, req in enumerate(rendered):
            requests.append(
                Request.from_url(
                    req.url,
                    method=req.method,
                    headers=req.headers or None,
                    payload=req.payload,
                    user_data={"tpl_index": index, "tpl_name": req.template_name},
                )
            )

        collected: dict[int, tuple] = {}

        async def collector(context) -> None:
            user_data = getattr(context.request, "user_data", None) or {}
            meta = dict(user_data) if isinstance(user_data, Mapping) else {}
            index = int(meta.get("tpl_index", -1))
            http_response = getattr(context, "http_response", None)
            status = int(getattr(http_response, "status_code", 0) or 0)
            body = await decode_response_body(http_response)
            payload_json = as_json(body)
            records = self._count_records(payload_json)
            collected[index] = (status, 200 <= status < 400, payload_json, records, "")

        runner = CrawlerRunner(self.config)
        await runner.run(collector, requests=requests, mode="http")

        results = []
        for index, req in enumerate(rendered):
            status, ok, payload_json, records, error = collected.get(index, (0, False, None, 0, "未收到响应"))
            results.append((req, status, ok, payload_json, 0.0, error))
        return results

    async def _replay_via_fetcher(self, rendered: Sequence[RenderedRequest]):
        """走引擎自带 AsyncFetcher：轻量并发调用，直接拿 JSON。"""
        import asyncio

        from crawler_engine.fetcher import AsyncFetcher

        semaphore = asyncio.Semaphore(max(int(self.config.concurrency), 1))
        results: list = [None] * len(rendered)

        async with AsyncFetcher(self.config) as fetcher:
            async def one(index: int, req: RenderedRequest) -> None:
                async with semaphore:
                    started = time.perf_counter()
                    parse_as_json = req.body_kind in (BODY_KIND_JSON, BODY_KIND_GRAPHQL)
                    response = await fetcher.request(
                        req.method,
                        req.url,
                        headers=req.headers or None,
                        json_body=json.loads(req.payload) if (parse_as_json and req.payload) else None,
                        data=None if parse_as_json else req.payload,
                    )
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    payload_json = None
                    if response.text:
                        payload_json = as_json(response.text)
                    records = self._count_records(payload_json)
                    results[index] = (
                        req, response.status, response.ok, payload_json,
                        elapsed_ms, response.error,
                    )

            await asyncio.gather(*[one(i, req) for i, req in enumerate(rendered)])
        return results

    @staticmethod
    def _count_records(payload_json: Any) -> int:
        """粗略统计返回记录数：list -> len；dict 取首个 list；其它 -> 1。"""
        if payload_json is None:
            return 0
        if isinstance(payload_json, list):
            return len(payload_json)
        if isinstance(payload_json, dict):
            for value in payload_json.values():
                if isinstance(value, list):
                    return len(value)
            for value in payload_json.values():
                if isinstance(value, dict):
                    for inner in value.values():
                        if isinstance(inner, list):
                            return len(inner)
            return 1
        return 1

    @staticmethod
    def shape_of(payload_json: Any) -> Any:
        """响应结构（不含具体值），用于落盘时避免持久化个人信息。"""
        return json_shape(payload_json)
