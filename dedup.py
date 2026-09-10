import hashlib
import os
import re


def is_url_string(val: str) -> bool:
    """检测字符串是否为网址/链接"""
    if not val:
        return False
    v = val.strip().lower()
    return v.startswith("http://") or v.startswith("https://") or v.startswith("//") or "globalsources.com" in v or "www." in v


def get_company_hash(name: str) -> str:
    """仅针对公司名称清洗归一并生成唯一的 MD5 指纹"""
    if not name or is_url_string(name):
        return ""
    # 过滤掉所有空格与标点符号，全小写，确保公司名归一
    clean_str = re.sub(r'[\s\W_]+', '', name.lower())
    if not clean_str:
        return ""
    return hashlib.md5(clean_str.encode('utf-8')).hexdigest()


class HashDeduplicator:
    """企业名称哈希去重器（仅接收 company 与 registered_company）"""

    def __init__(self, record_file: str = "seen_hashes.txt"):
        self.record_file = record_file
        self.seen_hashes = set()
        self._load()

    def _load(self):
        if os.path.exists(self.record_file):
            with open(self.record_file, "r", encoding="utf-8") as f:
                self.seen_hashes = {line.strip() for line in f if line.strip()}
            print(f"🔑 [指纹库] 成功加载 {len(self.seen_hashes)} 条企业名称历史哈希。")
        else:
            print("🔑 [指纹库] 首次运行，已自动初始化 seen_hashes.txt。")

    def is_seen(self, company_name: str) -> bool:
        """检查企业名称（店铺名/工商全称）是否已采集过"""
        h = get_company_hash(company_name)
        return bool(h and h in self.seen_hashes)

    def add(self, company_name: str):
        """仅将企业名称哈希存入内存与文本文件，自动忽略任何 URL"""
        h = get_company_hash(company_name)
        if h and h not in self.seen_hashes:
            self.seen_hashes.add(h)
            with open(self.record_file, "a", encoding="utf-8") as f:
                f.write(f"{h}\n")


dedup = HashDeduplicator()