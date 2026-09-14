import hashlib
from pathlib import Path
import re
import threading
from typing import Iterable, Optional

def is_url_string(val: str) -> bool:
    if not val or not isinstance(val, str):
        return False
    v = val.strip().lower()
    return (
        v.startswith("http://")
        or v.startswith("https://")
        or v.startswith("//")
        or "globalsources.com" in v
        or "www." in v
    )

def get_company_hash(name: str) -> str:
    if not name or not isinstance(name, str) or is_url_string(name):
        return ""
    clean_str = re.sub(r'[\s\W_]+', '', name.lower())
    if len(clean_str) < 3:
        return ""
    return hashlib.md5(clean_str.encode('utf-8')).hexdigest()

class HashDeduplicator:
    def __init__(self, record_file: str = "seen_hashes.txt"):
        self.record_path = Path(record_file)
        self.seen_hashes: set[str] = set()
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        with self._lock:
            if self.record_path.exists():
                try:
                    with open(self.record_path, "r", encoding="utf-8") as f:
                        self.seen_hashes = {line.strip() for line in f if line.strip()}
                    print(f"🔑 [指纹库] 成功加载 {len(self.seen_hashes)} 条企业名称历史哈希。")
                except Exception as e:
                    print(f"⚠️ [指纹库] 读取历史哈希文件异常: {e}")
                    self.seen_hashes = set()
            else:
                self.record_path.parent.mkdir(parents=True, exist_ok=True)
                print(f"🔑 [指纹库] 首次运行，已自动初始化 {self.record_path.name}。")

    def is_seen(self, company_name: str) -> bool:
        h = get_company_hash(company_name)
        if not h:
            return False
        with self._lock:
            return h in self.seen_hashes

    def add(self, company_name: str) -> bool:
        h = get_company_hash(company_name)
        if not h:
            return False
        with self._lock:
            if h in self.seen_hashes:
                return False
            self.seen_hashes.add(h)
            try:
                self.record_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.record_path, "a", encoding="utf-8") as f:
                    f.write(f"{h}\n")
                return True
            except Exception as e:
                print(f"⚠️ [指纹库] 写入哈希失败: {e}")
                return False

dedup = HashDeduplicator()