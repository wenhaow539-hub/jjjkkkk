import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Iterable, Optional, Union


def is_url_string(val: str) -> bool:
    """检测字符串是否为网址/链接"""
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
    """仅针对公司名称清洗归一并生成唯一的 MD5 指纹（与历史库 100% 算法兼容）"""
    if not name or not isinstance(name, str) or is_url_string(name):
        return ""
    # 过滤掉所有空格与标点符号，全小写，确保公司名归一
    clean_str = re.sub(r'[\s\W_]+', '', name.lower())
    if len(clean_str) < 3:
        return ""
    return hashlib.md5(clean_str.encode('utf-8')).hexdigest()


class HashDeduplicator:
    """企业名称哈希去重器（支持线程安全、多别名过滤与批量增量落盘）"""

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
        """检查单个企业名称（店铺名/工商全称）是否已采集过"""
        h = get_company_hash(company_name)
        if not h:
            return False
        with self._lock:
            return h in self.seen_hashes

    def is_any_seen(self, *company_names: Optional[str]) -> bool:
        """检查多个别名（如店铺名与工商注册名）中是否有任意一个已在库中"""
        with self._lock:
            for name in company_names:
                if not name:
                    continue
                h = get_company_hash(name)
                if h and h in self.seen_hashes:
                    return True
        return False

    def add(self, company_name: str) -> bool:
        """仅将企业名称哈希存入内存与文本文件，自动忽略任何 URL 与重复项"""
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

    def add_many(self, company_names: Iterable[str]) -> int:
        """批量写入多个企业名称哈希，降低频繁写文件的 I/O 开销"""
        new_hashes = []
        with self._lock:
            for name in company_names:
                if not name:
                    continue
                h = get_company_hash(name)
                if h and h not in self.seen_hashes:
                    self.seen_hashes.add(h)
                    new_hashes.append(h)

            if new_hashes:
                try:
                    self.record_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.record_path, "a", encoding="utf-8") as f:
                        f.writelines(f"{h}\n" for h in new_hashes)
                except Exception as e:
                    print(f"⚠️ [指纹库] 批量写入哈希异常: {e}")

        return len(new_hashes)


dedup = HashDeduplicator()