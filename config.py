"""项目配置：凭据一律来自环境变量 / .env，代码里不出现任何明文密钥。

优先级：真实环境变量 > 项目根目录 .env > 代码内默认值

用法：
    1. 复制 .env.example 为 .env，填入自己的密钥；
    2. 直接 `import config` 即可 —— 本模块会自行加载 .env，无需再调 load_dotenv()；
    3. 未配置密钥也不会崩：pipeline 会把空 key 传给 enrichers.evaluator，
       后者走内置降级（返回"未执行大模型质检"），采集与 Excel 导出照常进行。

安全约定：
    - 本文件**不得**出现明文凭据（历史上曾写死 OPENAI_API_KEY，已迁出到 .env）；
    - .env 已在 .gitignore 中；如需排查密钥问题，请用 api_key_status()，它只暴露长度与尾 4 位。
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

try:  # python-dotenv 是项目依赖；缺失时退化为纯环境变量，不阻断启动
    from dotenv import load_dotenv

    # override=False：真实环境变量优先于 .env 文件
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


def _first_env(*names: str, default: str = "") -> str:
    """按顺序取第一个非空环境变量，都没有则返回默认值。"""
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


# —— 大模型（DeepSeek，OpenAI 兼容协议）——
OPENAI_API_KEY = _first_env("OPENAI_API_KEY", "LLM_API_KEY", "DEEPSEEK_API_KEY")
OPENAI_BASE_URL = _first_env(
    "OPENAI_BASE_URL", "LLM_BASE_URL", "DEEPSEEK_BASE_URL", default="https://api.deepseek.com"
)
MODEL_NAME = _first_env("MODEL_NAME", "LLM_MODEL", default="deepseek-chat")

# 兼容旧名：pipeline.py 用 OPENAI_*，其它脚本可能用 LLM_*
LLM_API_KEY = OPENAI_API_KEY
LLM_BASE_URL = OPENAI_BASE_URL
LLM_MODEL = MODEL_NAME

# —— 路径与输出（均可用环境变量覆盖）——
USER_DATA_DIR = os.path.abspath(os.getenv("USER_DATA_DIR") or str(PROJECT_ROOT / "gs_user_data"))
OUTPUT_FILE = os.getenv("OUTPUT_FILE") or "target_leads.xlsx"


def api_key_status() -> str:
    """只暴露"是否配置"与尾 4 位，用于日志排查，不泄露密钥本体。"""
    if not OPENAI_API_KEY:
        return "未配置（大模型质检将走内置降级）"
    return f"已配置（...{OPENAI_API_KEY[-4:]}，长度 {len(OPENAI_API_KEY)}）"


if not OPENAI_API_KEY:  # 一次性告警：避免"静默降级"被误当成质检正常
    try:
        from utils.logger import get_logger

        get_logger("config").warning(
            "⚠️ 未检测到大模型密钥：请在项目根目录 .env 中配置 OPENAI_API_KEY（可参考 .env.example）。"
            "当前大模型质检将走内置降级，采集与导出不受影响。"
        )
    except Exception:  # 日志系统不可用时保持静默，绝不因告警影响启动
        pass
