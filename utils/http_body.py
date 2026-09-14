"""响应体读取与解码（引擎与适配器共用）。

为什么要独立成 utils：
    - 适配器（adapters/**）按职责边界不得依赖 crawler_engine；
    - 引擎的 HTTP 重放处理器又需要同一套解码逻辑；
    - 放在 utils/ 里，两边都能用，避免重复实现（架构规则：adapters 允许依赖 utils）。

坑（实测）：crawlee 1.10 的 http 上下文里 http_response 实际是 impit 的响应对象，
它的 read() 是**协程**；而 crawlee.http_clients.HttpResponse.read() 是同步方法。
只按其中一种写，会出现 "coroutine was never awaited" 并拿到空内容。
"""

from __future__ import annotations

import inspect
import json
from typing import Any

DEFAULT_ENCODINGS = ("utf-8", "gbk", "latin-1")


def charset_of(headers) -> str:
    """从 content-type 里取 charset。"""
    content_type = ""
    if headers is not None and hasattr(headers, "get"):
        content_type = headers.get("content-type", "") or ""
    for part in str(content_type).split(";"):
        if "charset=" in part:
            return part.split("charset=")[-1].strip().strip('"\'')
    return ""


def decode_bytes(body: bytes, headers=None) -> str:
    """按 charset → utf-8 → gbk → latin-1 逐级解码，永不抛异常。"""
    for encoding in (charset_of(headers), *DEFAULT_ENCODINGS):
        if not encoding:
            continue
        try:
            return body.decode(encoding, errors="ignore")
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="ignore")


async def decode_response_body(http_response) -> str:
    """把各种形态的响应对象读成 str。

    兼容：
        - impit 响应：read() 是协程
        - crawlee HttpResponse：read() 是同步方法
        - 直接暴露 content(bytes) / text(str) 的响应对象
    """
    if http_response is None:
        return ""

    body: Any = None
    read = getattr(http_response, "read", None)
    if callable(read):
        try:
            body = read()
            if inspect.isawaitable(body):
                body = await body
        except Exception:
            body = None

    if body is None:
        for attr in ("content", "text", "body"):
            value = getattr(http_response, attr, None)
            if value is not None and not callable(value):
                body = value
                break

    if body is None:
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, (bytes, bytearray)):
        return decode_bytes(bytes(body), getattr(http_response, "headers", None))
    return str(body)


def as_json(text: str) -> Any:
    """安全 JSON 解析：失败返回 None（不抛异常）。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def json_shape(value: Any, *, max_depth: int = 3, max_keys: int = 20) -> Any:
    """提取 JSON 的**结构**（key -> 类型名），不保留具体值。

    用于接口模板落盘：既能说明字段形态，又不会把响应里的个人信息写进文件。
    """
    if max_depth <= 0:
        return type(value).__name__

    if isinstance(value, dict):
        items = list(value.items())[:max_keys]
        return {str(k): json_shape(v, max_depth=max_depth - 1, max_keys=max_keys) for k, v in items}
    if isinstance(value, list):
        if not value:
            return ["<empty>"]
        return [json_shape(value[0], max_depth=max_depth - 1, max_keys=max_keys)]
    if value is None:
        return "null"
    return type(value).__name__
