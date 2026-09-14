import logging
import sys
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("logs")
_root_configured = False


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
            root.addHandler(file_handler)
        except OSError:
            root.warning("⚠️ [日志] 无法创建 logs/ 目录，本次仅输出到控制台。")

        _root_configured = True
    return logging.getLogger(f"jjkk.{name}")
