import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from utils.logger import get_logger

logger = get_logger("browser")

# ⚠️ 必须在任何 urllib 请求（query_devtools 探 CDP）之前设好。
# 双浏览器方案里「代理浏览器」是用 Chrome 的 --proxy-server 启动的（只影响浏览器自身流量），
# 但本机若设了全局 HTTP_PROXY/HTTPS_PROXY 环境变量，`query_devtools` 的 urllib 请求
# 也会被导去代理 → 连不上 127.0.0.1 的 DevTools → 表现为「端口始终等不到调试浏览器」。
# 实测踩过：显式设了 NO_PROXY 才能连上本地 CDP。
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

# —— CDP 端口的归属登记 ——
# 目的只有一个：**禁止两条流悄悄共用同一台浏览器**。
# 实测（2026-09-18）：浏览器 A 中途退出后，A 这条流回退时抓走了 B 的浏览器，
# 两条流从此共用一台（代理/账号/出口 IP 全部失效），而日志里只有一行不显眼的 ♻️。
_PORT_OWNERS: dict[int, str] = {}
_PORT_OWNERS_LOCK = threading.Lock()


def _claim_port(port: int, owner: str) -> str | None:
    """给端口登记归属。已被**其它** owner 占用时返回那个 owner，否则返回 None。

    同一个 owner 重复登记是允许的（`ensure_chrome_running` 会被反复调用）。
    """
    with _PORT_OWNERS_LOCK:
        cur = _PORT_OWNERS.get(port)
        if cur is None:
            _PORT_OWNERS[port] = owner
            return None
        return None if cur == owner else cur


def reset_port_owners() -> None:
    """清空归属登记（测试用；正常流程不需要）。"""
    with _PORT_OWNERS_LOCK:
        _PORT_OWNERS.clear()


@dataclass
class BrowserProfile:
    """一台调试浏览器 = 一个 profile 目录 + 一个 CDP 端口（可选一个代理）。

    「账号」就存在 profile 目录里（Chrome 的登录态是跟 user-data-dir 走的），
    所以**不需要在配置里写账号密码** —— 每个 profile 手动登录一次即可。

    双浏览器并行时两条流各持一份：
      · port/profile_dir 必须不同（Chrome 对同一 user-data-dir 只允许一个实例）；
      · lane_offset/lane_stride 让两条流扫**不相交**的列表页（A 扫 1/3/5、B 扫 2/4/6），
        否则两个浏览器会把同样的页各扫一遍。
    """

    name: str                       # "A" / "B"，同时也用作日志前缀与爬虫 stream_tag
    port: int = 9222
    profile_dir: str = "./chrome_debug_profile"
    proxy: str | None = None        # "http://user:pass@host:port"；None/空 = 直连
    enabled: bool = True
    # 主浏览器（列表里的第一个）。用现有 chrome_debug_profile —— 那是**本工具引入
    # 「登录标记」机制之前就存在、并已人工登录过**的历史目录，所以启动前**不做登录检查**
    # （要求它在 .wb_logged_in 标记会误判成「未登录」并整条流跳过）。
    # 其余 profile（B 等）必须由 `--login-browser <名字>` 建立过登录态。
    is_primary: bool = False
    # ⚠️ 只有**单浏览器**模式才该为 True。
    # ensure_chrome_running 在目标端口不是 DevTools 时会扫 port+1..port+10 找已在跑的
    # 调试浏览器并复用 —— 单浏览器时这是便利（能接管你自己拉的调试 Chrome），
    # 但**多流模式下等于"抢另一条流的浏览器"**：
    # 实测 2026-09-18：A 的浏览器中途退出后，A 回退时抓走了 B 的浏览器，
    # 两条流从此共用一台（代理/账号/IP 全废），而日志里只有一行不显眼的 ♻️。
    # 所以多流模式必须全 False：自己的浏览器没了就重新拉起自己的。
    reuse_nearby: bool = True
    start_delay: float = 0.0        # 流启动错峰秒数（错开两台首次翻页/验证码的时间点）
    lane_offset: int = 1            # 翻页车道起始页
    lane_stride: int = 1            # 翻页车道步长（双流 = 2）
    # 工商补全里**先跑哪一家**（hybrid 模式下的 pass 顺序）。
    # 两条流用不同的顺序可以让它们**错开站点**：A 先天眼查后爱企查、B 先爱企查后天眼查，
    # 于是任一时刻只有一条流在访问爱企查 —— 实测两条流同时进入爱企查会**同时弹验证码**，
    # 两个浏览器窗口互相抢焦点、人工过码时手忙脚乱。
    enrich_priority: str = "tianyancha"


def is_port_open(host: str = "127.0.0.1", port: int = 9222) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def query_devtools(host: str = "127.0.0.1", port: int = 9222, timeout: float = 1.5) -> dict | None:
    """探测端口上是否有**可用的 DevTools 服务**，返回 /json/version 内容，否则 None。

    为什么需要它：仅凭 "端口能连上"（is_port_open）无法区分
        「Chrome 的 DevTools」与「别的程序恰好占用了这个端口」。
    后者会让 connect_over_cdp 报 404 "This does not look like a DevTools server"。
    """
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            info = json.loads(resp.read().decode("utf-8", "ignore") or "{}")
            return info if info.get("Browser") else None
    except Exception:
        # 连接被拒 / 404 / 返回的不是 JSON —— 都视为"不是 DevTools"
        return None


def is_devtools_ready(host: str = "127.0.0.1", port: int = 9222) -> bool:
    """端口上是否已经有一个可用的 DevTools 调试浏览器。"""
    return query_devtools(host=host, port=port) is not None


def find_available_port(start: int = 9223, tries: int = 30, host: str = "127.0.0.1") -> int:
    """从 start 起找一个完全没被占用的端口。"""
    for port in range(start, start + tries):
        if not is_port_open(host=host, port=port):
            return port
    raise RuntimeError(f"从 {start} 起连续 {tries} 个端口都被占用，请手动释放一个端口。")


def _chrome_path() -> str:
    possible_paths = [
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    chrome_path = next((p for p in possible_paths if p and os.path.exists(p)), None)
    if not chrome_path:
        raise FileNotFoundError("未在系统路径找到 Chrome 可执行文件，请确认是否已安装。")
    return chrome_path


def ensure_chrome_running(
    port: int = 9222,
    profile_dir: str = "./chrome_debug_profile",
    platform_name: str = "Crawler",
    proxy: str | None = None,
    reuse_nearby: bool = True,
    owner: str | None = None,
) -> int:
    """确保存在一个**可用的 CDP 调试浏览器**，返回实际使用的端口。

    返回端口很重要：当请求的端口被别的程序占用时会自动改用其他端口，
    调用方必须用返回值（而不是自己原来那个数字）去 connect_over_cdp。

    proxy：给这台 Chrome 挂 HTTP 代理（`--proxy-server`）。None/空 = 直连。
    reuse_nearby：是否允许"复用邻近端口上已在跑的调试浏览器"。
        **只在单浏览器模式下开启**（用于接管用户自己拉起的调试 Chrome）。
        双浏览器时必须全部关闭 —— 见下面的详细说明。
    owner：这条流/这个调用方的名字（如 "A"/"B"）。传了就会给端口登记归属：
        若解析出的端口已属于**别的** owner，直接抛错而不是静默共用。
    """
    resolved = _resolve_chrome_port(
        port=port, profile_dir=profile_dir, platform_name=platform_name,
        proxy=proxy, reuse_nearby=reuse_nearby,
    )
    if owner:
        thief = _claim_port(resolved, owner)
        if thief:
            raise RuntimeError(
                f"❌ 端口 {resolved} 已归属另一条流（{thief}），当前流（{owner}）不能使用它。\n"
                f"   这几乎总是意味着：**该流自己的浏览器已经退出**，于是回退时抓走了另一条流的浏览器。\n"
                f"   后果是两条流共用同一台浏览器 —— 代理、登录账号、出口 IP 全部失效，而日志看起来一切正常。\n"
                f"   所以这里**直接中止该流**，而不是让它悄悄污染数据。\n"
                f"   处置：检查那台浏览器为什么退出（窗口被关 / 崩溃 / 内存不足），"
                f"关掉所有调试浏览器后重跑。"
            )
    return resolved


def _resolve_chrome_port(
    port: int = 9222,
    profile_dir: str = "./chrome_debug_profile",
    platform_name: str = "Crawler",
    proxy: str | None = None,
    reuse_nearby: bool = True,
) -> int:
    """真正去探测/拉起浏览器，返回端口（不做归属校验）。"""
    if is_devtools_ready(port=port):
        return port

    # 目标端口不是可用的 DevTools。先在附近找有没有**已经在跑的**调试浏览器可以复用：
    # Chrome 对同一个 user-data-dir 只允许一个实例，盲目再起一个会因为 profile 被占用
    # 而"启动成功但立刻退出"，表现为端口始终等不到 DevTools（实测踩过这个坑）。
    #
    # ⚠️⚠️ 但这个便利对「双浏览器」是**有害的**，实测踩到过（2026-09-18）：
    #    A 的浏览器中途退出 → A 这条流发现 9222 不是 DevTools → 这里一路扫到 9223、
    #    把 **B 的浏览器**返回给了 A → 从此两条流共用同一台浏览器，
    #    代理/账号/出口 IP 全部形同虚设，而日志里只有一行不显眼的 ♻️，数据照常入库。
    #    所以双浏览器模式下**所有** profile 都必须 reuse_nearby=False：
    #    自己的浏览器没了就重新拉起自己的，而不是去抢别人的。
    if reuse_nearby:
        for candidate in range(port + 1, port + 11):
            info = query_devtools(port=candidate)
            if info:
                logger.warning(
                    f"♻️ [{platform_name}] 端口 {port} 不是可用的 DevTools，"
                    f"复用已在端口 {candidate} 运行的调试浏览器 ({info.get('Browser')})。"
                    f"⚠️ 多流模式下这会让两条流共用一台浏览器，请确认这不是意外。"
                )
                return candidate

    if is_port_open(port=port):
        # 端口能连上，但不是 DevTools —— 直接用它必然 connect_over_cdp 404。
        # 实测场景：本机已有 Chrome/其他服务占着 9222，返回 404 空响应。
        occupied_port = port
        port = find_available_port(start=port + 1)
        logger.warning(
            f"⚠️ [{platform_name}] 端口 {occupied_port} 已被占用，但它不是可用的 DevTools 服务 "
            f"(实测 /json/version 无响应) —— 已自动改用端口 {port}。"
        )

    _proxy_tag = f" | 代理: {proxy}" if proxy else " | 直连（已显式禁用系统代理）"
    logger.warning(f"🚀 [{platform_name}] 未检测到 CDP 调试浏览器，拉起 Chrome (端口: {port}{_proxy_tag})...")
    chrome_path = _chrome_path()

    os.makedirs(profile_dir, exist_ok=True)
    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={os.path.abspath(profile_dir)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu-shader-disk-cache",
    ]
    if proxy:
        cmd.append(f"--proxy-server={proxy}")
        # `<-loopback>` 是 Chrome 的隐式 bypass 规则：让 127.0.0.1/localhost 不走代理。
        # 不加的话，某些代理会尝试转发本机回环地址，导致 DevTools 和本地页面异常。
        cmd.append("--proxy-bypass-list=<-loopback>")
    else:
        # ⚠️ 显式直连，**不能省略这个参数**。
        # Chrome 默认会继承 Windows 的「系统代理」（注册表 ProxyEnable/ProxyServer）。
        # 用户开着 Clash/V2Ray 之类的系统代理时，本意"直连"的那台浏览器其实**也在走代理** ——
        # 于是双浏览器变成"两台同一个出口 IP"，分摊风控完全落空，而且**没有任何提示**：
        # 日志照样打印、采集照样成功，只是风控风险一点没降。
        # 实测（2026-09-18）：本机 ProxyEnable=1 / ProxyServer=127.0.0.1:7897，正是这个情况。
        #
        # 想让它用系统代理时，请**显式**配 `BROWSER_*_PROXY`（而不是依赖继承）——
        # 显式配置可读、可查、不会随系统的代理开关悄悄改变行为。
        cmd.append("--no-proxy-server")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 就绪判据是"DevTools 真的应答"，而不是"端口开了"（后者会被别的进程误导）
    for _ in range(20):
        if is_devtools_ready(port=port):
            time.sleep(1)
            return port
        time.sleep(1)
    raise TimeoutError(
        f"等待 Chrome 调试端口就绪超时 (端口: {port})。"
        f"若该端口被其他程序占用，请关闭占用者或改用其它端口。"
    )
