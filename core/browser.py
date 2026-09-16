import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request


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
) -> int:
    """确保存在一个**可用的 CDP 调试浏览器**，返回实际使用的端口。

    返回端口很重要：当请求的端口被别的程序占用时会自动改用其他端口，
    调用方必须用返回值（而不是自己原来那个数字）去 connect_over_cdp。
    """
    if is_devtools_ready(port=port):
        return port

    # 目标端口不是可用的 DevTools。先在附近找有没有**已经在跑的**调试浏览器可以复用：
    # Chrome 对同一个 user-data-dir 只允许一个实例，盲目再起一个会因为 profile 被占用
    # 而"启动成功但立刻退出"，表现为端口始终等不到 DevTools（实测踩过这个坑）。
    for candidate in range(port + 1, port + 11):
        info = query_devtools(port=candidate)
        if info:
            print(
                f"♻️ [{platform_name}] 端口 {port} 不是可用的 DevTools，"
                f"复用已在端口 {candidate} 运行的调试浏览器 ({info.get('Browser')})。"
            )
            return candidate

    if is_port_open(port=port):
        # 端口能连上，但不是 DevTools —— 直接用它必然 connect_over_cdp 404。
        # 实测场景：本机已有 Chrome/其他服务占着 9222，返回 404 空响应。
        occupied_port = port
        port = find_available_port(start=port + 1)
        print(
            f"⚠️ [{platform_name}] 端口 {occupied_port} 已被占用，但它不是可用的 DevTools 服务 "
            f"(实测 /json/version 无响应) —— 已自动改用端口 {port}。"
        )

    print(f"🚀 [{platform_name}] 未检测到 CDP 调试浏览器，拉起 Chrome (端口: {port})...")
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
