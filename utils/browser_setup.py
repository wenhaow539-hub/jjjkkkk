"""浏览器登录态的一次性设置与启动前检查（双浏览器 / 多账号专用）。

## 为什么需要它

「账号」在这套系统里就是 **Chrome 的 profile 目录**（登录态跟随 `--user-data-dir`）。
浏览器 A 沿用既有的 `chrome_debug_profile`，里面天眼查/爱企查早就登录好了；
但双浏览器新增的浏览器 B 用的是**全新目录** —— 里面一个登录态都没有。

如果不管这件事直接跑双流，B 这条流的工商补全会**全线失败**（天眼查/爱企查都要求登录），
而且失败形态是"查无此企业/拿不到字段"，很容易被误判成数据问题。

## 怎么判断"登录好了"

不用「页面上有没有『登录』字样」这类判据 —— 实测那个词在正文里到处出现
（enrichers 里就有专门把"登录"当脏值过滤掉的代码），判错方向会让整条流静默失效。

改成**显式标记文件**：跑一次 `--login-browser B`，在窗口里把两个站都登好，回终端按回车，
我们就在该 profile 目录里写一个 `.wb_logged_in` 标记。启动检查只看这个标记 ——
确定、可解释、不依赖站点改版。

代价：标记不会因 cookie 过期自动失效。所以启动横幅会打印标记的时间戳，
session 过期时重跑一次 `--login-browser B` 即可。
"""

import asyncio
import datetime
import os
from pathlib import Path

from core.browser import ensure_chrome_running
from utils.logger import get_logger

logger = get_logger("browser_setup")

MARKER_NAME = ".wb_logged_in"

# 需要登录的两个站点（工商补全用）
LOGIN_URLS = (
    ("天眼查", "https://www.tianyancha.com/login"),
    ("爱企查", "https://aiqicha.baidu.com/user/login"),
)


def login_marker(profile) -> Path:
    """标记文件路径（放在该 profile 目录里，跟着浏览器走）。"""
    return Path(profile.profile_dir) / MARKER_NAME


def is_logged_in(profile) -> bool:
    """该 profile 是否已具备登录态。

    ⚠️ 主浏览器（`is_primary`）直接返回 True，**不看标记文件**。
    它用的是 `chrome_debug_profile` —— 本工具引入标记机制之前就存在、并已人工登录过的
    历史目录。若也要求标记，会被判成"未登录"→ 整条流被跳过 → 整个流程废掉。
    """
    if getattr(profile, "is_primary", False):
        return True
    return login_marker(profile).exists()


def needs_login_check(profile) -> bool:
    """是否需要做登录检查（主浏览器不需要）。"""
    return not getattr(profile, "is_primary", False)


def mark_logged_in(profile) -> Path:
    path = login_marker(profile)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"logged_in_at={datetime.datetime.now().isoformat(timespec='seconds')}\n"
            f"note=由 `python main.py --login-browser {profile.name}` 写入；"
            f"session 过期时删掉本文件并重跑该命令。\n",
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(f"⚠️ [登录设置] 无法写入标记文件 {path}: {e}")
    return path


def marker_summary(profile) -> str:
    """给启动横幅用的一行说明。"""
    if getattr(profile, "is_primary", False):
        return "主浏览器（沿用历史目录，假定已登录）"
    path = login_marker(profile)
    if not path.exists():
        return "⚠️ 未登录（缺标记）"
    try:
        first = path.read_text(encoding="utf-8").splitlines()[0]
        return first.replace("logged_in_at=", "登录于 ")
    except Exception:
        return "已登录（标记存在）"


async def open_for_login(profile, *, auto_confirm_seconds: float = 0.0) -> bool:
    """拉起指定浏览器并打开两个登录页，等人工登录完成后写标记。

    `auto_confirm_seconds > 0` 时不等回车，改为等待固定秒数后自动确认
    （适合无人值守/脚本化场景；默认 0 = 等你在终端按回车）。
    """
    print()
    print("=" * 66)
    print(f"🔑 [登录设置] 浏览器 {profile.name}｜CDP 端口 {profile.port}")
    print(f"   profile 目录: {os.path.abspath(profile.profile_dir)}")
    print(f"   出口: {'代理 ' + profile.proxy if profile.proxy else '本机直连'}")
    print("=" * 66)
    if profile.proxy:
        print("   ⚠️ 这台走代理：登录页能否打开就是代理是否可用的第一道检验。")
        print("      若登录页打不开/一直转圈，先确认 BROWSER_%s_PROXY 填对了。" % profile.name)

    # 复用统一的启动逻辑：端口/profile/代理/复用策略全部一致，
    # 避免"设置登录态时用的浏览器"和"跑流水线时用的浏览器"其实不是同一台。
    ensure_chrome_running(
        port=profile.port,
        profile_dir=profile.profile_dir,
        platform_name=f"登录设置({profile.name})",
        proxy=profile.proxy,
        reuse_nearby=profile.reuse_nearby,
    )

    from playwright.async_api import async_playwright

    opened = 0
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(
                f"http://127.0.0.1:{profile.port}", timeout=30000
            )
        except Exception as e:
            print(f"\n❌ [登录设置] 无法附着 CDP 浏览器（端口 {profile.port}）: {type(e).__name__}: {e}")
            print("   最常见原因：该浏览器里有卡死/无响应的标签页 —— 关掉它们再重试。")
            return False

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        for site, url in LOGIN_URLS:
            try:
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                print(f"   ✅ 已打开{site}登录页: {url}")
                opened += 1
            except Exception as e:
                print(f"   ⚠️ 打开{site}登录页失败（{type(e).__name__}）: {url}")
                print(f"      可手动在窗口里访问该站点登录。")

    if opened == 0:
        print("\n⚠️ [登录设置] 两个登录页都没打开成功，请手动在浏览器里访问站点登录。")

    print()
    print("👉 请在刚打开的 Chrome 窗口里，分别登录【天眼查】和【爱企查】。")
    print("   登录完成后回到这里继续 —— 我会在 profile 目录里写一个标记文件，")
    print("   之后每次跑双流都会检查它。")
    print(f"   标记位置: {os.path.abspath(login_marker(profile))}")
    print()

    if auto_confirm_seconds > 0:
        logger.info(f"   ⏳ {auto_confirm_seconds:.0f}s 后自动确认（--login-auto-confirm）...")
        await asyncio.sleep(auto_confirm_seconds)
    else:
        try:
            # input() 是阻塞调用，扔进线程避免卡住事件循环
            await asyncio.to_thread(input, "   两个站都登录好了吗？按【回车】写入标记（Ctrl-C 放弃）: ")
        except (EOFError, KeyboardInterrupt):
            print("\n   已取消，未写入标记。")
            return False

    path = mark_logged_in(profile)
    print(f"\n✅ [登录设置] 已写入标记: {path}")
    print(f"   之后跑 `python main.py --pipeline ...` 就会带上浏览器 {profile.name}。")
    print("   （session 过期时删掉该文件并重跑本命令即可）")
    return True


async def wait_for_login(profile, timeout: float) -> bool:
    """启动前检查：没登录就提示，并按 timeout 等待人工补救。返回是否可用。

    超时**不抛异常** —— 调用方应当只跳过这条流，让另一条继续跑。
    """
    if is_logged_in(profile):
        return True

    # 区分两种「没标记」：
    #   ① profile 目录根本不存在 → 从没设置过。这时**不空等**，直接跳过并告诉他跑什么
    #      （否则首次跑双流会白等 3 分钟，人还容易被卡在那里干等）
    #   ② 目录在、但没标记 → 用户可能正在设置，等一会儿有意义
    from pathlib import Path
    if not Path(profile.profile_dir).exists():
        print()
        print("!" * 66)
        print(f"⏭️ [双浏览器] 浏览器 {profile.name} 从未设置过（profile 目录不存在），本次跳过：")
        print(f"   {os.path.abspath(profile.profile_dir)}")
        print(f"   要启用它，先跑一次：")
        print(f"       python main.py --login-browser {profile.name}")
        print(f"   在弹出的窗口里登录天眼查 + 爱企查，之后它就会自动加入。")
        print("!" * 66)
        return False

    print()
    print("!" * 66)
    print(f"🚨 [双浏览器] 浏览器 {profile.name} 尚未设置登录态！")
    print(f"   标记文件不存在: {os.path.abspath(login_marker(profile))}")
    print(f"   profile 目录: {os.path.abspath(profile.profile_dir)}")
    print()
    print(f"   该流的工商补全（天眼查/爱企查）都会失败 —— 因为这两个站要求登录。")
    print(f"   正确做法：先跑一次 `python main.py --login-browser {profile.name}`")
    print(f"             在弹出的窗口里登录两个站点，然后回来跑流水线。")
    print("!" * 66)

    if timeout <= 0:
        print(f"   ⚠️ LOGIN_WAIT_SECONDS=0 → 不等待，本次**跳过浏览器 {profile.name}**。")
        return False

    print(f"   ⏳ 现在给你 {timeout:.0f} 秒：切到浏览器 {profile.name} 的窗口把两个站登录上。")
    print(f"      （期间我每秒检查一次标记文件；你也可以另开终端跑 --login-browser {profile.name}）")
    print()

    waited = 0.0
    while waited < timeout:
        await asyncio.sleep(1.0)
        waited += 1.0
        if is_logged_in(profile):
            logger.info(f"   ✅ 检测到浏览器 {profile.name} 的登录标记，继续。")
            return True
        if int(waited) % 20 == 0:
            print(f"   ⏳ 仍在等待浏览器 {profile.name} 的登录标记...（已等 {waited:.0f}/{timeout:.0f}s）")

    print(f"   ⚠️ 等待超时（{timeout:.0f}s）→ 本次**跳过浏览器 {profile.name}**，"
          f"另一条流继续跑。")
    return False
