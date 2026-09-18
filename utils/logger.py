import contextvars
import logging
import sys
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("logs")
_root_configured = False

# 当前协程所属的「流」标识（双浏览器并行时为 "A"/"B"，单流为空串）。
#
# 为什么用 contextvars 而不是给 logger 加参数：双流并行时两条流写在同一个
# logs/pipeline_YYYYMMDD.log 里，交错输出无法区分是哪台浏览器。contextvars 随
# asyncio 任务隔离，天然是「这条协程」的记号，不需要把 tag 一路透传下去
# （pipeline.py 里有一百多处 logger 调用，改造量太大且容易漏）。
#
# 为什么用 Filter 而不是 LoggerAdapter：Filter 挂在 root handler 上，
# **一个调用点都不用改** —— 拿到 record 时按当前上下文给 msg 加前缀即可。
# 单流时 tag 为空串 → 输出与改造前**逐字节一致**。
STREAM_TAG: contextvars.ContextVar = contextvars.ContextVar("stream_tag", default="")


class _StreamTagFilter(logging.Filter):
    """给日志消息加上 `[A] ` / `[B] ` 前缀（仅当当前上下文设置了 tag）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        tag = STREAM_TAG.get()
        # `_tagged` 幂等标记：一条 record 会被 root 的多个 handler 各过滤一次，
        # 不挡的话前缀会被叠加成 "[A] [A] [A] "。
        if tag and not getattr(record, "_stream_tagged", False):
            record.msg = f"[{tag}] {record.msg}"
            record._stream_tagged = True
        return True


def stream_context(tag: str):
    """设置当前协程的流标识，返回可用于还原的 token。

    用法：`token = stream_context("B")` … `STREAM_TAG.reset(token)`。
    放在 try/finally 里，避免协程结束后把标记泄漏给别的任务。
    """
    return STREAM_TAG.set(str(tag or ""))


def get_logger(name: str = "pipeline") -> logging.Logger:
    """获取统一日志器：控制台输出 INFO（保持原有简洁样式），文件落盘 DEBUG。

    文件位于 logs/pipeline_YYYYMMDD.log，用于事后追溯（替代不可追溯的 print）。
    """
    global _root_configured
    root = logging.getLogger("jjkk")
    if not _root_configured:
        root.setLevel(logging.DEBUG)
        root.propagate = False

        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(message)s"))
        console.addFilter(_StreamTagFilter())
        root.addHandler(console)

        try:
            LOG_DIR.mkdir(exist_ok=True)
            file_handler = logging.FileHandler(
                LOG_DIR / f"pipeline_{datetime.now():%Y%m%d}.log", encoding="utf-8"
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
            )
            file_handler.addFilter(_StreamTagFilter())
            root.addHandler(file_handler)
        except OSError:
            root.warning("⚠️ [日志] 无法创建 logs/ 目录，本次仅输出到控制台。")

        _root_configured = True
    return logging.getLogger(f"jjkk.{name}")
