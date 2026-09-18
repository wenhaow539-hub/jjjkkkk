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
# 点选类验证码（天眼查的「请在下图依次点击」）走云码的另一个 type：
# 30009 = 通用任意点选 1~4 个坐标，**人工识别接口**，返回按顺序的坐标。
# 注意：它比旋转类型贵（约 0.025 元/次），且官方注明「不支持报错退费」。
YUNMA_POINT_TYPE = _first_env("YUNMA_POINT_TYPE", default="30009")

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
# **点选类**验证码落点的二维正态偏移标准差（px），默认 3.0。
# 注意这里与上面的旋转类型**故意不同**，不是自相矛盾：
#   · 旋转：服务端不知道"理想落点"，且识别误差本来就让落点变化 → 加偏移纯属吃容差余量，默认 0。
#   · 点选：服务端**知道每个图形的确切位置**，原样点击会"每次都钉在几何中心"，
#     这是可统计检测的自动化特征 → 必须带随机偏移。
# 3px 的依据：蝴蝶类图形约 35~45px 宽（半径 ~20px），3σ 截断到 2σ=6px 仍稳在命中区内。
CAPTCHA_POINT_JITTER_PX = float(_first_env("CAPTCHA_POINT_JITTER_PX", default="3.0") or 0)
# 图片预处理：把非正方形的验证码图居中裁成正方形（旋转模型要求方图）。
# auto = 长宽比偏差超过 5% 才裁；on = 总是裁；off = 不裁。
CAPTCHA_SQUARE_CROP = _first_env("CAPTCHA_SQUARE_CROP", default="auto").strip().lower() or "auto"


# —— 详情页请求节奏（GS 等站的 HTTPX 抓取）——
# ⚠️ 这是**全局限速器**：所有并发任务共享同一个实例，相邻请求的启动间隔 >= 本值 × (1±jitter)。
#    所以它**直接决定**详情阶段的墙钟时间 ≈ 请求数 × 本值。
#    实测（2026-09-17）：12 个请求、并发上限 4 → 耗时 12.6s（均值 1.14s/请求），**完全串行**。
#    ⇒ 调大并发度没有意义（`Semaphore(4)` 只是让 4 个协程一起排队），只有这个值能改变速度。
#    粗算：30 家 × 3~4 个请求 ≈ 90~120 次 → 0.9s 时约 1.4~1.8 分钟（1.2s 时是 1.8~2.4 分钟）。
#    ⚠️ 不宜再往下压：一次 403/429 重试要花 3 个限速槽 + 4.5s 退避 ≈ 8s，被拒多了反而更慢。
DETAIL_RATE_MIN_INTERVAL = float(_first_env("DETAIL_RATE_MIN_INTERVAL", default="0.9") or 0.9)
DETAIL_RATE_JITTER = float(_first_env("DETAIL_RATE_JITTER", default="0.35") or 0.35)


# —— GS 搜索列表的翻页深度 ——
# ⚠️ 这个值曾经写死 25，是**真实踩到的坑**（2026-09-18）：
#    实测 `phone + China-Guangdong + 5年内` 在 GS 上有 **62 页**（每页 20 家 ≈ 1240 家），
#    而代码只让扫到第 25 页 —— 前 25 页被采完之后，26 页往后**永远扫不到**。
#    表现不是报错，而是"候选入库 +0 家"反复出现 → `run_pipeline` 判为
#    「连续 2 批零新增、候选池枯竭」并提前退出，看起来像"这个关键词没数据了"。
#    实际是**代码自己把池子砍掉了六成**。
# 放心调大：翻页循环里有"空页检测"（`query_selector_all` 返回空即 break），
#    页数上限设得比实际大不会白跑 —— 扫到底自然停，只多花一次请求。
# 成本：每页约 8~10s（加载 ~2s + 就绪停顿 2~3s + 滚动 3 次 + 翻页冷却 1.5~2.5s）。
GS_MAX_SEARCH_PAGES = int(_first_env("GS_MAX_SEARCH_PAGES", default="100") or 100)


def detail_rate_status() -> str:
    """详情页限速的一行摘要（启动横幅 / GS 详情阶段打印用）。"""
    lo = DETAIL_RATE_MIN_INTERVAL * (1 - DETAIL_RATE_JITTER)
    hi = DETAIL_RATE_MIN_INTERVAL * (1 + DETAIL_RATE_JITTER)
    return (f"详情页限速 {lo:.2f}~{hi:.2f}s/请求（均值 {DETAIL_RATE_MIN_INTERVAL:.2f}s，"
            f"全局限速器串行 → 并发度不改变总耗时）")


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
            return (f"平台=云码，token 已配置（...{YUNMA_TOKEN[-4:]}，"
                    f"旋转 type={YUNMA_TYPE}，点选 type={YUNMA_POINT_TYPE}）")
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


# =========================================================================== #
# 双浏览器 / 多账号（单进程内并行）
# =========================================================================== #
# 目标：一个 Python 进程里同时驱动两台 Chrome，各跑一套完整流水线。
#   · 浏览器 A —— 本机直连，沿用现有 chrome_debug_profile（登录态已在，无需重登）
#   · 浏览器 B —— 走 HTTP 代理，独立 profile 目录（**需要先手动登录一次**）
# 一个 profile 目录 = 一组登录态（天眼查 + 爱企查），所以 2 台 = 4 个账号。
# 账号密码**不写在这里** —— Chrome 的登录态是跟 user-data-dir 走的，
# 用 `python main.py --login-browser B` 拉起窗口手动登一次即可。
#
# ⚠️ 退回单浏览器：把 NUM_STREAMS 设为 1（或 run_mvp.py 里改常量）。
NUM_STREAMS = max(1, int(_first_env("NUM_STREAMS", default="2") or 2))

# 人工等待登录的上限（秒）。浏览器 B 的 profile 是全新的、没有任何登录态，
# 首次跑双流时若直接进补全，天眼查/爱企查会全线失败。启动时会检查一次登录态，
# 未登录就提示并按这个秒数等待；超时**只跳过 B 流**（A 照常跑），不整轮失败。
# 设 0 = 不等待（适合无人值守；但仍会打印告警）。
LOGIN_WAIT_SECONDS = max(0.0, float(_first_env("LOGIN_WAIT_SECONDS", default="180") or 180))

# 默认端口/profile 约定：A 用 9222 + 现有目录（兼容历史），B 用 9223 + 新目录。
_BROWSER_DEFAULTS = {
    "A": {"port": 9222, "profile": "./chrome_debug_profile", "delay": 0.0},
    "B": {"port": 9223, "profile": "./chrome_debug_profile_b", "delay": 8.0},
}


def _build_browser_profiles() -> list:
    """按 NUM_STREAMS 产出 BrowserProfile 列表（A 是主浏览器，B 是代理浏览器）。

    几个刻意为之的点：
      · **只有单浏览器模式才允许复用邻近端口**（`reuse_nearby = NUM_STREAMS == 1`）。
        ensure_chrome_running 在目标端口不是 DevTools 时会扫邻近端口找"已在跑的调试浏览器"
        并复用；单流时这是便利，**多流时等于抢另一条流的浏览器** ——
        实测 2026-09-18：A 的浏览器中途退出后，A 回退时抓走了 B 的浏览器，
        两条流从此共用一台（代理/账号/IP 全废），日志里只有一行不显眼的 ♻️。
      · **翻页车道错开**：A 扫 1/3/5…、B 扫 2/4/6…（同一关键词互不重复扫页）。
        单流时 stride=1、offset=1，行为与改造前完全一致。
      · **补全顺序错开**：A 先天眼查→后爱企查，B 先爱企查→后天眼查。
        两条流若同序，会几乎同时进入爱企查并**同时弹验证码**（实测），
        两个浏览器窗口抢焦点、人工过码很难受。错开后任一时刻只有一条流在爱企查。
      · 端口自动递推：不显式配置时 B 取 A 的端口 + 1，避免两台撞同一个 CDP 端口。
    """
    from core.browser import BrowserProfile   # 延迟导入：避免 config <-> core 循环依赖

    letters = ["A", "B", "C", "D"][:NUM_STREAMS]
    stride = NUM_STREAMS if NUM_STREAMS > 1 else 1
    # 多流才需要"抢"浏览器，所以复用便利只在单流保留
    allow_reuse = (NUM_STREAMS == 1)
    out: list = []
    for idx, letter in enumerate(letters):
        d = _BROWSER_DEFAULTS.get(letter, {"port": 9222 + idx,
                                           "profile": f"./chrome_debug_profile_{letter.lower()}",
                                           "delay": 8.0})
        try:
            port = int(_first_env(f"BROWSER_{letter}_PORT", default=str(d["port"])) or d["port"])
        except ValueError:
            port = d["port"]
        profile = _first_env(f"BROWSER_{letter}_PROFILE", default=d["profile"]) or d["profile"]
        proxy = _first_env(f"BROWSER_{letter}_PROXY", default="") or None
        try:
            delay = float(_first_env(f"BROWSER_{letter}_START_DELAY", default=str(d["delay"]))
                          or d["delay"])
        except ValueError:
            delay = d["delay"]
        # 补全顺序：偶数号（A/C）先天眼查，奇数号（B/D）先爱企查 —— 相邻两条流永远错开
        default_priority = "tianyancha" if idx % 2 == 0 else "aiqicha"
        priority = (_first_env(f"BROWSER_{letter}_ENRICH_PRIORITY", default=default_priority)
                    or default_priority).strip().lower()
        if priority not in ("tianyancha", "aiqicha"):
            priority = default_priority
        out.append(BrowserProfile(
            name=letter,
            port=port,
            profile_dir=profile,
            proxy=proxy,
            enabled=True,
            is_primary=(idx == 0),
            reuse_nearby=allow_reuse,
            start_delay=max(0.0, delay),
            lane_offset=idx + 1,
            lane_stride=stride,
            enrich_priority=priority,
        ))
    return out


BROWSER_PROFILES: list = _build_browser_profiles()


def browser_status() -> str:
    """启动横幅用的一行摘要（只暴露端口/profile/是否走代理，不含账号信息）。"""
    if len(BROWSER_PROFILES) <= 1:
        p = BROWSER_PROFILES[0]
        return (f"单浏览器（{p.name}）｜CDP 端口 {p.port}｜profile {p.profile_dir}｜"
                f"{'代理 ' + p.proxy if p.proxy else '本机直连'}")
    parts = []
    _PRIO = {"tianyancha": "天眼查先", "aiqicha": "爱企查先"}
    for p in BROWSER_PROFILES:
        parts.append(f"{p.name}: 端口{p.port}/{p.profile_dir}/"
                     f"{('代理 ' + p.proxy) if p.proxy else '本机直连'}"
                     f"/扫第{p.lane_offset},{p.lane_offset + p.lane_stride},…页"
                     f"/补全{_PRIO.get(p.enrich_priority, p.enrich_priority)}")
    return f"双浏览器并行（共 {len(BROWSER_PROFILES)} 台）｜" + "｜".join(parts)


if not OPENAI_API_KEY:  # 一次性告警：避免"静默降级"被误当成质检正常
    try:
        from utils.logger import get_logger

        get_logger("config").warning(
            "⚠️ 未检测到大模型密钥：请在项目根目录 .env 中配置 OPENAI_API_KEY（可参考 .env.example）。"
            "当前大模型质检将走内置降级，采集与导出不受影响。"
        )
    except Exception:  # 日志系统不可用时保持静默，绝不因告警影响启动
        pass
