import os
import shutil
import socket
import subprocess
import time

def is_port_open(host: str = "127.0.0.1", port: int = 9222) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0

def ensure_chrome_running(port: int = 9222, profile_dir: str = "./chrome_debug_profile", platform_name: str = "Crawler"):
    if is_port_open(port=port):
        return

    print(f"🚀 [{platform_name}] 未检测到 CDP 调试浏览器，拉起 Chrome (端口: {port})...")
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

    for _ in range(15):
        if is_port_open(port=port):
            time.sleep(1)
            return
        time.sleep(1)
    raise TimeoutError(f"等待 Chrome 启动超时 (端口: {port})。")