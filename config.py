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


# —— 打码平台（可选，用于自动过爱企查的旋转验证码）——
# 不配置也能跑：验证码会自动退化为「人工等待」。
# 支持两家，协议差异封装在 enrichers/captcha_solver.py：
#   ttshitu 图鉴：TTSHITU_USERNAME / TTSHITU_PASSWORD（typeid 默认 29=旋转类型）
#   yunma   云码：YUNMA_TOKEN（type 默认 900011=通用旋转验证码）
CAPTCHA_PROVIDER = _first_env("CAPTCHA_PROVIDER", default="").strip().lower()
TTSHITU_USERNAME = _first_env("TTSHITU_USERNAME")
TTSHITU_PASSWORD = _first_env("TTSHITU_PASSWORD")
TTSHITU_TYPEID = _first_env("TTSHITU_TYPEID", default="29")
YUNMA_TOKEN = _first_env("YUNMA_TOKEN")
YUNMA_TYPE = _first_env("YUNMA_TYPE", default="900011")

# 打码 HTTP 超时与「同一张图」的重试次数（页面级的失败重来另算）
# 图鉴官方文档明确要求超时 ≥60s（人工/人机型接口有时需排队），故默认 60 而非 30。
CAPTCHA_HTTP_TIMEOUT = float(_first_env("CAPTCHA_HTTP_TIMEOUT", default="60") or 60)
# 命中验证码后**人工等待**的上限（秒）。默认 240，对齐天眼查的口径
# （天眼查 `_handle_captcha_if_needed` 是 `for _ in range(120): sleep(2)` ≈ 240s）。
# 原来是 60s —— 人还没来得及切窗口滑一下就超时了，超时还会被计成"环境异常"喂给熔断。
# 无人值守场景用 `--captcha-mode off` 直接跳过，别靠把这里调小。
CAPTCHA_WAIT_SECONDS = float(_first_env("CAPTCHA_WAIT_SECONDS", default="240") or 240)
CAPTCHA_MAX_ATTEMPTS = int(_first_env("CAPTCHA_MAX_ATTEMPTS", default="2") or 2)
# 角度 → 拖动像素的换算基准（度/圈对应的轨道行程）。
# 0 = 运行时从 DOM 量轨道宽度；量不到才回退到 212（百度页内 ac_c 公式的基准值）。
CAPTCHA_TRACK_PX = float(_first_env("CAPTCHA_TRACK_PX", default="0") or 0)
# 拖动方向：+1 = 向右拖为顺时针（默认，与平台"顺时针角度"语义一致）；-1 = 反向
CAPTCHA_ANGLE_SIGN = -1.0 if _first_env("CAPTCHA_ANGLE_SIGN", default="1") == "-1" else 1.0
# 落点的随机偏移（像素）。默认 0 = 不加偏移 —— 这是有意的。
# 理由：平台识别本身就有约 ±4~9° 的误差，而服务端的角度容差余量有限；
# 人为加偏移只会**吃掉余量、降低通过率**。而且落点本来就随图片与识别结果天然变化
# （每张验证码图不同、识别结果也有几度波动），同一落点不会重复出现。
# 真正该"加随机"的地方是**轨迹**（时序/抖动/过冲/中途停顿），那已在 _drag_to_angle 里随机化了。
# 若仍想试验，建议 ≤1.5px（≈2°）。
CAPTCHA_LANDING_JITTER_PX = float(_first_env("CAPTCHA_LANDING_JITTER_PX", default="0") or 0)
# 图片预处理：把非正方形的验证码图居中裁成正方形（旋转模型要求方图）。
# auto = 长宽比偏差超过 5% 才裁；on = 总是裁；off = 不裁。
CAPTCHA_SQUARE_CROP = _first_env("CAPTCHA_SQUARE_CROP", default="auto").strip().lower() or "auto"


def auto_captcha_provider() -> str:
    """决定实际生效的打码平台。

    显式配置 CAPTCHA_PROVIDER 时以它为准（none/off 表示明确禁用）；
    **`auto` 与未配置等价 —— 按"哪家凭据齐全"自动推断**（这点踩过坑：
    若把 "auto" 当成平台名往下传，会被判成"未知平台"而静默失效）。
    都没有则返回空串（退化为人工等待）。
    """
    explicit = (CAPTCHA_PROVIDER or "").strip().lower()
    if explicit in ("none", "off", "disabled"):
        return ""
    if explicit and explicit != "auto":
        return explicit
    if YUNMA_TOKEN:
        return "yunma"
    if TTSHITU_USERNAME and TTSHITU_PASSWORD:
        return "ttshitu"
    return ""


def captcha_status() -> str:
    """只暴露"配置是否就绪"与尾号，不泄露密钥本体（与 api_key_status 同规约）。"""
    provider = auto_captcha_provider()
    if provider == "yunma":
        if YUNMA_TOKEN:
            return f"平台=云码，token 已配置（...{YUNMA_TOKEN[-4:]}，type={YUNMA_TYPE}）"
        return "平台=云码（显式指定），但 YUNMA_TOKEN 为空 → 退化为人工等待"
    if provider == "ttshitu":
        if TTSHITU_USERNAME and TTSHITU_PASSWORD:
            return (f"平台=图鉴，账号已配置（...{TTSHITU_USERNAME[-3:]}，"
                    f"typeid={TTSHITU_TYPEID}）")
        return "平台=图鉴（显式指定），但账号或密码为空 → 退化为人工等待"
    explicit = (CAPTCHA_PROVIDER or "").strip().lower()
    if explicit and explicit not in ("none", "off", "disabled", "auto"):
        return f"平台={explicit}，但凭据缺失或平台名未知 → 退化为人工等待"
    return "未配置（验证码走人工等待：遇到验证码会提示并等待你手动滑过）"


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
