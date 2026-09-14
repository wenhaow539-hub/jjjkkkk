"""职责边界：把架构约定写成机器可校验的规则。

四层职责（渐进式迁移后的目标形态）：

    crawler_engine   怎么抓      浏览器生命周期 / 请求队列 / 并发 / Retry / Session / Cookie / Proxy / HTTP / Playwright
    adapters         抓哪个网站   + 怎么解析（只提供 URL、解析页面、返回结构化数据）
    pipeline         数据处理流程 编排采集与增强、落盘（本轮不改动它）
    enrichers        企业补全     独立站探测 / 天眼查 / AI 质检（本引擎不依赖它）

为什么要有这个模块：
    迁移过程中最容易出的问题不是"代码写错"，而是**越界**——适配器里偷偷建了 httpx client、
    引擎里顺手调了业务模块。这类问题靠 code review 容易漏，写成 AST 检查才能长期守住。

检查规则：
    R1  adapters/** 不得出现任何 HTTP / 浏览器客户端依赖与调用（抓取一律交给引擎）
    R2  adapters/** 不得依赖 crawler_engine / enrichers / pipeline / exporters
    R3  crawler_engine 核心模块（模块级导入）不得依赖 enrichers / pipeline / exporters / crawlers / adapters
    R4  声明为"有意例外"的依赖必须在 DOCUMENTED_EXCEPTIONS 中登记理由

用法：
    python -m crawler_engine --selfcheck        # 命令行自检
    from crawler_engine.architecture import run_checks, report
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# 约定
# --------------------------------------------------------------------------- #
LAYER_RESPONSIBILITIES = {
    "crawler_engine": "怎么抓：浏览器生命周期 / 请求队列 / 并发 / Retry / Session / Cookie / Proxy / HTTP / Playwright",
    "adapters": "抓哪个网站 + 怎么解析：只提供 URL、解析页面、返回结构化数据",
    "pipeline": "数据处理流程：编排采集与增强、落盘（本轮不改动）",
    "enrichers": "企业补全：独立站探测 / 天眼查 / AI 质检（引擎不依赖）",
}

ADAPTER_FORBIDDEN_IMPORTS = frozenset({
    "httpx", "requests", "aiohttp", "urllib3", "curl_cffi", "selenium", "playwright", "crawlee",
})
ADAPTER_FORBIDDEN_CALLS = frozenset({
    "connect_over_cdp", "new_page", "goto", "launch", "AsyncClient", "Client", "Session",
})

ADAPTER_ALLOWED_DEPS = frozenset({"adapters", "utils", "models"})
"""适配器只允许依赖：自身、纯工具（utils）、数据契约（models）。"""

ENGINE_CORE_FORBIDDEN_IMPORTS = frozenset({
    "enrichers", "pipeline", "exporters", "crawlers", "adapters",
})
"""引擎核心不依赖业务编排/补全/落盘，也不依赖具体平台实现。"""

DOCUMENTED_EXCEPTIONS = {
    "adapters/globalsources.py": {
        "note": "仅依赖 adapters/utils/models，无客户端依赖（入队只传纯 URL 字符串，不导入 crawlee）",
    },
    "crawler_engine/runner.py": {
        "exporters": "CLI 交付便利：仅在 --excel 分支内懒加载，引擎核心链路不依赖",
        "core": "legacy 桥接：run_platform 需从 CrawlerFactory 解析已注册的旧爬虫",
    },
    "crawler_engine/browser.py": {
        "core": "复用 legacy 的 Chrome 启动器（仅在 CDP 模式拉起调试端口时调用，引擎是唯一调用方）",
    },
}
"""有意且已说明理由的依赖例外：会被报告为 note 而不判为失败。

注意：适配器默认**不得**导入 crawlee（保持与框架解耦、可离线单测）。
若确需使用 crawlee 的类型契约（例如构造 Router），请在此登记理由，否则自检判为越界。
"""

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SKIP_FILES = {"__init__.py", "__main__.py"}


@dataclass
class Finding:
    level: str  # "error" | "note"
    rule: str
    path: str
    detail: str

    def line(self) -> str:
        mark = "❌" if self.level == "error" else "ℹ️"
        return f"{mark} [{self.rule}] {self.path}: {self.detail}"


# --------------------------------------------------------------------------- #
# AST 工具
# --------------------------------------------------------------------------- #
def _rel(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _imports(tree: ast.AST, *, module_level_only: bool) -> set[str]:
    """收集 import 的顶层包名；module_level_only=True 时只统计模块级导入。"""
    found: set[str] = set()
    nodes = tree.body if module_level_only else list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入属于包内
                continue
            if node.module:
                found.add(node.module.split(".")[0])
    return found


def _calls(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name:
                names.add(name)
    return names


def _iter_modules(package: str):
    root = PROJECT_ROOT / package
    if not root.exists():
        return
    for path in sorted(root.rglob("*.py")):
        if path.name in _SKIP_FILES or "__pycache__" in path.parts:
            continue
        yield path


def _exception_for(rel_path: str, package: str) -> str | None:
    exceptions = DOCUMENTED_EXCEPTIONS.get(rel_path)
    if not exceptions:
        return None
    return exceptions.get(package)


# --------------------------------------------------------------------------- #
# R1 / R2：适配器边界
# --------------------------------------------------------------------------- #
def check_adapters() -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_modules("adapters"):
        rel = _rel(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        for pkg in sorted(_imports(tree, module_level_only=False) & ADAPTER_FORBIDDEN_IMPORTS):
            reason = _exception_for(rel, pkg)
            findings.append(Finding(
                "note" if reason else "error", "R1", rel,
                f"适配器不得依赖客户端/浏览器库 '{pkg}'（抓取一律交给 crawler_engine）"
                + (f"；已登记例外：{reason}" if reason else ""),
            ))

        deps = _imports(tree, module_level_only=False)
        for pkg in sorted(deps - ADAPTER_ALLOWED_DEPS - ADAPTER_FORBIDDEN_IMPORTS):
            if pkg in {"__future__", "typing", "dataclasses", "collections", "re", "json", "urllib",
                       "hashlib", "time", "asyncio", "pathlib", "inspect", "ast"}:
                continue
            findings.append(Finding(
                "note", "R2", rel,
                f"适配器出现非预期依赖 '{pkg}'（允许：{sorted(ADAPTER_ALLOWED_DEPS)} + 标准库）",
            ))

        for name in sorted(_calls(tree) & ADAPTER_FORBIDDEN_CALLS):
            findings.append(Finding(
                "error", "R2", rel,
                f"适配器不得调用 '{name}()'（页面/请求由引擎创建）",
            ))
    return findings


# --------------------------------------------------------------------------- #
# R3 / R4：引擎核心边界
# --------------------------------------------------------------------------- #
def check_engine() -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_modules("crawler_engine"):
        rel = _rel(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        module_level = _imports(tree, module_level_only=True)
        for pkg in sorted(module_level & ENGINE_CORE_FORBIDDEN_IMPORTS):
            reason = _exception_for(rel, pkg)
            findings.append(Finding(
                "note" if reason else "error", "R3", rel,
                f"模块级导入了业务模块 '{pkg}'"
                + (f"（已登记例外：{reason}）" if reason else "（引擎核心不得依赖业务层）"),
            ))

        nested_only = _imports(tree, module_level_only=False) - module_level
        for pkg in sorted(nested_only & ENGINE_CORE_FORBIDDEN_IMPORTS):
            reason = _exception_for(rel, pkg)
            findings.append(Finding(
                "note" if reason else "error", "R4", rel,
                f"函数内懒加载了业务模块 '{pkg}'"
                + (f"（已登记例外：{reason}）" if reason else "（未登记理由，请在 DOCUMENTED_EXCEPTIONS 说明）"),
            ))
    return findings


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def run_checks() -> tuple[list[Finding], list[Finding]]:
    """返回 (errors, notes)。"""
    findings = check_adapters() + check_engine()
    errors = [f for f in findings if f.level == "error"]
    notes = [f for f in findings if f.level == "note"]
    return errors, notes


def report() -> str:
    errors, notes = run_checks()
    lines = ["职责边界："]
    for layer, desc in LAYER_RESPONSIBILITIES.items():
        lines.append(f"    {layer:<16} {desc}")
    lines.append("")
    lines.append("检查结果：")
    if not errors and not notes:
        lines.append("    ✅ 无越界：adapters 无客户端依赖，引擎核心未依赖业务层")
    for finding in notes:
        lines.append("    " + finding.line())
    for finding in errors:
        lines.append("    " + finding.line())
    lines.append("")
    lines.append(f"    error={len(errors)} note={len(notes)}")
    return "\n".join(lines)
