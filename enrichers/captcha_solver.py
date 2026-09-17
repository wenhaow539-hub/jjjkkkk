"""付费打码平台接入：识别旋转验证码的角度。

═══ 先读这段，它解释了整个方案的可行性边界 ═══════════════════════════════

我们只向平台要**一件事**：把图片转正所需的顺时针角度（度）。

百度旋转验证码提交时要带一堆加密参数（`fs` = `rzData` 经 AES-ECB 加密、
`spin-0` 动态 p 值、`fuid` 环境指纹），这些**不需要我们管** ——
因为拖动是在**真实浏览器里用真实鼠标事件**（CDP `Input.dispatchMouseEvent`）完成的，
页面自己的 JS 会采集轨迹、自己算出这些加密参数。
**这是本方案能成立的前提**：我们要做的只是"把滑块拖到正确的像素位置"。

页面内的换算关系（多处逆向资料交叉验证）：
    ac_c = round(角度 × 212 / 360, 2)        ← 212 是滑块的基准行程
所以拖动距离 ≈ 角度 / 360 × 轨道可用宽度。轨道宽度运行时从 DOM 量，
量不到才回退到 212（配置可用 CAPTCHA_TRACK_PX 覆盖）。

═══ 已核实的平台协议（2026-09 查证，勿凭记忆改）════════════════════════════

  图鉴 ttshitu : POST http://api.ttshitu.com/predict
                 JSON {"username","password","typeid","image"}
                 typeid=29 → 旋转类型，返回角度；约 0.002 元/次
  云码 yunma   : POST http://api.jfbym.com/api/YmServer/customApi
                 JSON {"token","type","image"}
                 type=900011 → 通用旋转验证码，返回转正所需顺时针度数(0~360)

两家返回的都是「转正所需顺时针角度」，语义一致，所以可以同构封装。

═══ 诚实的局限 ═══════════════════════════════════════════════════════════

* **角度误差是现实存在的**。平台侧多为人工/人机接口，通常可用；
  自建模型（rotate-captcha-crack 的 RotNetR）跨域平均误差约 7°，
  站点容差未必够。所以调用方**必须支持"失败就重取新图重来"的多轮尝试**，
  不能指望一次命中 —— 见 `AiQiChaEnricher._auto_solve_captcha`。
* 平台一改协议、或验证码改版，这里就会失效。失效时**不是静默**的：
  会打印明确告警并自动退化为人工等待（见 `AiQiChaEnricher.captcha_mode`）。
* 凭据一律从环境变量 / `.env` 读（见 `config.py`），代码里不出现明文。
  未配置任何凭据时 `CaptchaSolver.from_config()` 返回 None，属于**正常状态**。

═══ 怎么拿凭据（用户常问）═════════════════════════════════════════════════

  图鉴 ttshitu  https://www.ttshitu.com/register.html   （登录页 /login.html）
               凭据 = **注册账号本身（用户名 + 密码）**，没有单独 token。
               充值：登录后进用户中心购买流量包。官方文档 /docs/python.html
  云码 yunma    https://console.jfbym.com/register/       （用户中心 index/index）
               凭据 = 用户中心里的 **Token/密钥**（可重置）。
               充值：用户中心「在线充值」；价格见 www.jfbym.com/price.html

  ⚠️ 两家的注册/登录都带**点选式图形验证码**，必须人工操作一次。

═══ 自检用法 ════════════════════════════════════════════════════════════

    python -m enrichers.captcha_solver --status           # 看配置是否就绪（不联网）
    python -m enrichers.captcha_solver --balance          # 查余额/账号（不消耗识别次数）
    python -m enrichers.captcha_solver --image cap.png    # 用本地图片试一次真实识别
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys

import httpx

import config
from utils.logger import get_logger

logger = get_logger("captcha_solver")


class CaptchaProviderError(RuntimeError):
    """打码平台返回的错误。

    fatal=True 表示"重试也没用"（余额不足 / 账号密码错 / 无此识别类型 / 无权限），
    调用方应当**停用**该 provider 并退化为人工，而不是反复重试烧掉配额。
    """

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


def _parse_angle(value) -> float:
    """把平台返回的角度解析成 [0, 360) 的浮点数。"""
    if value is None:
        raise CaptchaProviderError("平台未返回角度（result 为空）")
    if isinstance(value, (list, tuple)):
        if not value:
            raise CaptchaProviderError("平台返回空列表")
        value = value[0]
    text = str(value)
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        raise CaptchaProviderError(f"无法从平台返回值解析角度: {text[:60]!r}")
    angle = float(m.group(0)) % 360.0
    return angle


def _parse_points(value) -> list[tuple[float, float]]:
    """把平台返回的点选结果解析成 `[(x, y), ...]`，**严格保持平台给出的顺序**。

    点选类验证码的全部意义就在顺序上（「请在下图依次点击」），
    所以这里绝不做排序、去重或按坐标重排 —— 那会把正确答案打乱成错的。

    返回格式不统一，已被"code 是字符串"坑过一次，这里同样兜多种形态：
        "100,200|300,400"        "100,200;300,400"
        "[[100,200],[300,400]]"  ["100,200", "300,400"]     （JSON 字符串）
        "1.(100,200) 2.(300,400)"（带序号）
    """
    if value is None:
        raise CaptchaProviderError("平台未返回坐标（result 为空）")

    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, (list, tuple)):
                parts.append(",".join(str(x) for x in item))
            else:
                parts.append(str(item))
        text = "|".join(parts)
    else:
        text = str(value)

    # ① JSON 数组形态：[[x,y],[x,y]]
    try:
        loaded = json.loads(text)
        if (isinstance(loaded, list) and loaded
                and all(isinstance(i, (list, tuple)) and len(i) >= 2 for i in loaded)):
            return [(float(i[0]), float(i[1])) for i in loaded]
    except Exception:
        pass

    # ② 显式成对写法：`x,y`（分隔符可为 , ， ; : 空格）
    pairs = re.findall(r"(-?\d+(?:\.\d+)?)\s*[,，;:\s]\s*(-?\d+(?:\.\d+)?)", text)
    if pairs:
        return [(float(a), float(b)) for a, b in pairs]

    # ③ 兜底：把所有数字两两配对
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text)]
    if len(nums) >= 4 and len(nums) % 2 == 0:
        return [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]

    raise CaptchaProviderError(f"无法从平台返回值解析点选坐标: {text[:80]!r}")


def _b64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("ascii")


def _as_int(value) -> int | None:
    """把平台返回的状态码归一成 int。

    实测两家都可能返回**字符串**码（图鉴 `"code":"-1"`、云码 `"code":"10003"`），
    所以任何与整数常量的比较之前都必须先归一 —— 否则成功也会被误判成失败。
    """
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _payload_field(payload, key):
    """从平台返回的 data 字段里取值。

    `data` 的形态**并不固定**（已被坑过两次，故三种都兜）：
      * 成功时多为 dict（图鉴 `{"result": ...}` / 云码 `{"data": ...}`）
      * 失败时云码返回**空 list**
      * 还可能直接把结果作为标量放在 data 里（`"137.2"`）
    """
    if isinstance(payload, dict):
        return payload.get(key)
    if isinstance(payload, (list, tuple)):
        if payload and isinstance(payload[0], dict):
            return payload[0].get(key)
        return None
    if isinstance(payload, (str, int, float)):
        return payload
    return None


# --------------------------------------------------------------------------- #
# Provider：每个平台的请求/响应差异只在这两个类里
# --------------------------------------------------------------------------- #
class _Provider:
    name = ""
    # 是否支持点选类验证码。目前只有云码有对口的 type（30009 = 通用任意点选 1~4 坐标）。
    supports_points = False

    def available(self) -> bool:
        return False

    def describe(self) -> str:
        return self.name

    async def solve_rotate(self, client: httpx.AsyncClient, image_bytes: bytes) -> float:
        raise NotImplementedError

    async def solve_points(self, client: httpx.AsyncClient,
                           image_bytes: bytes) -> list[tuple[float, float]]:
        """识别点选验证码，返回**按点击顺序**的坐标（相对传入图片的像素坐标）。

        注意：图片里必须自带规则说明（提示文字或箭头图标）——
        人工识别接口拿不到额外参数，打码员只能看图办事。
        """
        raise NotImplementedError

    async def query_balance(self) -> str:
        """查询账户余额 / 账号信息（可选能力，**不消耗识别次数**）。

        用途：让用户在真正跑采集前就确认"凭据填对了没有"，而不是等到弹验证码才发现。
        平台没提供查询接口时返回提示文本，不当成错误。
        """
        return "该平台未提供余额查询接口，请登录其用户中心查看"


class TtshituProvider(_Provider):
    """图鉴（ttshitu.com）：通用图片识别接口，typeid=29 为旋转类型。

    凭据就是**注册账号本身**（用户名 + 密码），没有单独的 token。
    另有几个可选的旋转类 typeid：1029 / 2029 = 背景匹配旋转（需两张图 image+imageback），
    29 = 单图旋转类型 —— 默认用 29，若识别效果不佳可用 TTSHITU_TYPEID 换。
    """

    name = "ttshitu"
    API = "http://api.ttshitu.com/predict"
    BALANCE_API = "http://api.ttshitu.com/queryAccountInfo.json"
    # 这些词出现在 message 里说明是账号/配额/类型问题，重试无意义
    _FATAL_HINTS = ("余额", "密码", "用户名", "账号", "权限", "类型", "不存在", "失效", "欠费")
    # code=-1 是**实测确认**的"用户名或密码错误"（返回的是字符串 "-1"）。
    # 其余 code 官方语义不一，故主要靠 message 关键词判断，不用猜测的码表。
    _FATAL_CODES = {-1}

    def __init__(self, username: str, password: str, typeid: str = "29") -> None:
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.typeid = str(typeid or "29").strip() or "29"

    def available(self) -> bool:
        return bool(self.username and self.password)

    def describe(self) -> str:
        return f"图鉴(typeid={self.typeid}, 账号=...{self.username[-3:] if self.username else '?'})"

    async def query_balance(self) -> str:
        """图鉴官方的账户信息查询（GET /queryAccountInfo.json），不消耗识别次数。"""
        try:
            async with httpx.AsyncClient(timeout=config.CAPTCHA_HTTP_TIMEOUT) as client:
                resp = await client.get(
                    self.BALANCE_API,
                    params={"username": self.username, "password": self.password},
                )
                data = resp.json()
        except Exception as e:
            return f"余额查询失败（{type(e).__name__}: {e}）"
        if not data.get("success"):
            return f"余额查询失败：{data.get('message')}（code={data.get('code')}）"
        return f"账户信息 {json.dumps(data.get('data'), ensure_ascii=False)}"

    async def solve_rotate(self, client: httpx.AsyncClient, image_bytes: bytes) -> float:
        try:
            typeid: object = int(self.typeid)
        except ValueError:
            typeid = self.typeid
        payload = {
            "username": self.username,
            "password": self.password,
            "typeid": typeid,
            "image": _b64(image_bytes),
        }
        resp = await client.post(self.API, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            code = data.get("code")
            msg = str(data.get("message") or "未知错误")
            fatal = (_as_int(code) in self._FATAL_CODES) or any(h in msg for h in self._FATAL_HINTS)
            raise CaptchaProviderError(f"图鉴: {msg}（code={code}）", fatal=fatal)
        return _parse_angle(_payload_field(data.get("data"), "result"))


class YunmaProvider(_Provider):
    """云码（jfbym.com）：跨平台通用接口，type=900011 为通用旋转验证码。"""

    name = "yunma"
    API = "http://api.jfbym.com/api/YmServer/customApi"
    supports_points = True
    OK_CODE = 10000
    # 10002 余额不足 / 10003 无权限(未取得有效 token) / 10004 无此验证类型
    FATAL_CODES = {10002, 10003, 10004}

    def __init__(self, token: str, type_code: str = "900011",
                 point_type: str = "30009") -> None:
        self.token = (token or "").strip()
        self.type_code = str(type_code or "900011").strip() or "900011"
        # 点选类型与旋转类型是**两个不同的 type**，必须分开保存：
        # 900011=旋转（便宜） / 30009=通用任意点选（人工接口，约 0.025 元/次）
        self.point_type = str(point_type or "30009").strip() or "30009"

    def available(self) -> bool:
        return bool(self.token)

    def describe(self) -> str:
        return (f"云码(旋转={self.type_code}, 点选={self.point_type}, "
                f"token=...{self.token[-4:] if self.token else '?'})")

    async def _post(self, client: httpx.AsyncClient, type_code: str, image_bytes: bytes):
        payload = {"token": self.token, "type": type_code, "image": _b64(image_bytes)}
        resp = await client.post(self.API, json=payload)
        resp.raise_for_status()
        data = resp.json()
        # ⚠️ 关键：云码返回的 code 是**字符串**（实测 {"code":"10003",...}）。
        # 直接与整数 10000 比较会让**成功也被判成失败**（"10000" != 10000），
        # 也会让 10003 这类致命错误漏掉 fatal 判定而徒劳重试。必须先归一到 int。
        code = _as_int(data.get("code"))
        if code != self.OK_CODE:
            msg = str(data.get("msg") or "未知错误")
            raise CaptchaProviderError(
                f"云码: {msg}（code={data.get('code')}）",
                fatal=(code in self.FATAL_CODES) if code is not None else False,
            )
        return data

    async def solve_points(self, client: httpx.AsyncClient,
                           image_bytes: bytes) -> list[tuple[float, float]]:
        data = await self._post(client, self.point_type, image_bytes)
        return _parse_points(_payload_field(data.get("data"), "data"))

    async def solve_rotate(self, client: httpx.AsyncClient, image_bytes: bytes) -> float:
        data = await self._post(client, self.type_code, image_bytes)
        return _parse_angle(_payload_field(data.get("data"), "data"))


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
class CaptchaSolver:
    """打码平台的统一入口：`await solver.solve_rotate(png_bytes) -> 角度`。"""

    def __init__(self, provider: _Provider, *, timeout: float = 30.0,
                 retries: int = 2) -> None:
        self.provider = provider
        self.timeout = float(timeout)
        self.retries = max(1, int(retries))
        self.calls = 0
        self.ok = 0
        self.disabled_reason = ""

    # ---- 构造 ---------------------------------------------------------- #
    @classmethod
    def from_config(cls, provider: str | None = None) -> "CaptchaSolver | None":
        """按配置构造；未配置凭据时返回 None（正常状态，调用方退化为人工）。"""
        name = (provider or "").strip().lower()
        if name in ("", "auto"):
            name = config.auto_captcha_provider()
        if name in ("", "none", "off"):
            return None

        chosen: _Provider | None = None
        if name == "ttshitu":
            chosen = TtshituProvider(
                config.TTSHITU_USERNAME, config.TTSHITU_PASSWORD, config.TTSHITU_TYPEID
            )
        elif name == "yunma":
            chosen = YunmaProvider(config.YUNMA_TOKEN, config.YUNMA_TYPE,
                                   config.YUNMA_POINT_TYPE)
        else:
            logger.warning(
                f"⚠️ [打码] 未知平台 {name!r}（可选: ttshitu / yunma），本次退化为人工等待"
            )
            return None

        if not chosen.available():
            logger.warning(
                f"⚠️ [打码] 平台 {name!r} 的凭据未配置完整，本次退化为人工等待。"
                f"请在 .env 中补齐（见 .env.example 的「打码平台」一节）"
            )
            return None

        logger.info(f"🤖 [打码] 已启用 {chosen.describe()}，验证码将尝试自动识别")
        return cls(chosen, timeout=config.CAPTCHA_HTTP_TIMEOUT,
                   retries=config.CAPTCHA_MAX_ATTEMPTS)

    # ---- 识别 ---------------------------------------------------------- #
    async def solve_rotate(self, image_bytes: bytes) -> float | None:
        """识别旋转角度。成功返回 [0,360) 的度数；失败返回 None（调用方转人工）。

        可重试的错误（网络抖动 / 平台繁忙）会自动重试；
        fatal 错误（余额、账号、类型）立即放弃并记住原因，不浪费配额。
        """
        if not image_bytes or len(image_bytes) < 64:
            logger.warning("⚠️ [打码] 验证码图片为空或过小，跳过识别")
            return None

        last_err = ""
        for attempt in range(1, self.retries + 1):
            self.calls += 1
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    angle = await self.provider.solve_rotate(client, image_bytes)
                self.ok += 1
                return angle
            except CaptchaProviderError as e:
                last_err = str(e)
                if e.fatal:
                    self.disabled_reason = last_err
                    logger.error(f"❌ [打码] {self.provider.name} 不可用（重试无意义）: {e}")
                    return None
                logger.warning(f"⚠️ [打码] 第 {attempt}/{self.retries} 次失败: {e}")
            except (httpx.HTTPError, ValueError, KeyError) as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning(f"⚠️ [打码] 第 {attempt}/{self.retries} 次异常: {last_err}")
            if attempt < self.retries:
                await asyncio.sleep(1.0 * attempt)

        logger.error(f"❌ [打码] {self.provider.name} 识别失败，转人工等待（最后错误: {last_err}）")
        return None

    async def solve_points(self, image_bytes: bytes) -> list[tuple[float, float]] | None:
        """识别点选验证码，成功返回**按点击顺序**的坐标；失败返回 None（调用方转人工）。

        与 solve_rotate 分开实现（而不是抽公共循环）：旋转那条链路是**已在真实站点验证通过**的，
        不为复用去改动它。这里的失败代价更高（点选是人工接口、0.025 元/次且不报错退费），
        所以重试策略也更保守。
        """
        if not self.provider.supports_points:
            logger.warning(
                f"⚠️ [打码] 平台 {self.provider.name} 不支持点选类验证码"
                f"（目前只有云码的 type=30009 支持），该次转人工等待"
            )
            return None
        if not image_bytes or len(image_bytes) < 64:
            logger.warning("⚠️ [打码] 验证码截图为空或过小，跳过识别")
            return None

        last_err = ""
        for attempt in range(1, self.retries + 1):
            self.calls += 1
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    points = await self.provider.solve_points(client, image_bytes)
                self.ok += 1
                return points
            except CaptchaProviderError as e:
                last_err = str(e)
                if e.fatal:
                    self.disabled_reason = last_err
                    logger.error(f"❌ [打码] {self.provider.name} 不可用（重试无意义）: {e}")
                    return None
                logger.warning(f"⚠️ [打码] 点选 第 {attempt}/{self.retries} 次失败: {e}")
            except (httpx.HTTPError, ValueError, KeyError) as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning(f"⚠️ [打码] 点选 第 {attempt}/{self.retries} 次异常: {last_err}")
            if attempt < self.retries:
                await asyncio.sleep(1.0 * attempt)

        logger.error(f"❌ [打码] {self.provider.name} 点选识别失败，转人工等待（最后错误: {last_err}）")
        return None


# --------------------------------------------------------------------------- #
# 自检 CLI：让用户在不跑整条流水线的前提下验证自己的 key 是否可用
# --------------------------------------------------------------------------- #
def _status() -> int:
    provider = config.auto_captcha_provider()
    print("打码平台配置状态")
    print(f"  CAPTCHA_PROVIDER = {config.CAPTCHA_PROVIDER or '(未设置)'}")
    print(f"  实际生效平台     = {provider or '(无 —— 验证码将走人工等待)'}")
    print(f"  {config.captcha_status()}")
    if not provider:
        print("\n未配置时行为：命中验证码 → 提示并等待你手动滑过（不影响采集，只是需要人在电脑前）。")
        print("\n怎么拿凭据（都是先注册、再充值，注册要过一次点选图形验证码）：")
        print("  图鉴 ttshitu  https://www.ttshitu.com/register.html")
        print("               凭据 = 注册账号本身（用户名+密码），无单独 token；")
        print("               填 .env 的 TTSHITU_USERNAME / TTSHITU_PASSWORD")
        print("  云码 yunma    https://console.jfbym.com/register/")
        print("               凭据 = 用户中心里的 Token；填 .env 的 YUNMA_TOKEN")
        print("  价格参考      图鉴低至 0.2 厘/次；云码 www.jfbym.com/price.html")
    return 0 if provider else 1


async def _try_image(path: str) -> int:
    solver = CaptchaSolver.from_config()
    if solver is None:
        print("未配置打码平台凭据，无法测试。先运行 --status 查看需要填哪些键。")
        return 1
    with open(path, "rb") as f:
        data = f.read()
    print(f"图片: {path}（{len(data)} 字节）  平台: {solver.provider.describe()}")
    angle = await solver.solve_rotate(data)
    if angle is None:
        print("识别失败（详见上方日志）")
        return 1
    print(f"识别结果: 转正所需顺时针角度 = {angle:.1f}°")
    return 0


async def _try_points(path: str) -> int:
    """点选验证码自检：把一张**含提示**的验证码截图丢给平台，看返回的坐标序列。

    这是接天眼查点选前最有价值的一步 —— 不用开浏览器、不用跑流水线，
    就能先确认「这个平台的这个人可接口，到底认不认得这种箭头提示的点选图」。
    """
    solver = CaptchaSolver.from_config()
    if solver is None:
        print("未配置打码平台凭据，无法测试。先运行 --status 查看需要填哪些键。")
        return 1
    if not solver.provider.supports_points:
        print(f"当前平台 {solver.provider.name} 不支持点选类验证码"
              f"（需要云码 type=30009）。可在 .env 配 YUNMA_TOKEN 后重试。")
        return 1
    with open(path, "rb") as f:
        data = f.read()
    print(f"图片: {path}（{len(data)} 字节）  平台: {solver.provider.describe()}")
    print("⚠️ 提醒：截图必须包含图中的**提示**（文字或箭头图标），否则打码员无法判断顺序。")
    points = await solver.solve_points(data)
    if points is None:
        print("识别失败（详见上方日志）")
        return 1
    print(f"识别结果: 共 {len(points)} 个点，按点击顺序为")
    for i, (x, y) in enumerate(points, 1):
        print(f"   {i}. ({x:.0f}, {y:.0f})")
    print("\n提示：坐标相对**这张图片**的左上角。前 3~4 个点若顺序/位置明显不对，"
          "说明平台认不准这种图，建议先别接自动过码，仍走人工滑动。")
    return 0


async def _balance() -> int:
    solver = CaptchaSolver.from_config()
    if solver is None:
        print("未配置打码平台凭据，无法查询。先运行 --status 查看需要填哪些键。")
        return 1
    print(f"平台: {solver.provider.describe()}")
    print(f"  {await solver.provider.query_balance()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打码平台接入自检")
    parser.add_argument("--status", action="store_true", help="只检查配置是否就绪（不联网）")
    parser.add_argument("--balance", action="store_true",
                        help="查询账户余额/信息（不消耗识别次数，用来确认凭据是否有效）")
    parser.add_argument("--image", default=None,
                        help="用这张本地图片真实调用一次**旋转**识别（type=900011）")
    parser.add_argument("--points", default=None,
                        help="用这张本地图片真实调用一次**点选**识别（云码 type=30009）。"
                             "图片必须含提示文字/箭头，即验证码弹窗的完整截图")
    args = parser.parse_args(argv)
    if args.points:
        return asyncio.run(_try_points(args.points))
    if args.image:
        return asyncio.run(_try_image(args.image))
    if args.balance:
        return asyncio.run(_balance())
    return _status()


if __name__ == "__main__":
    sys.exit(main())
