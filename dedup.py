import hashlib
import os
import re


def get_name_hash(name: str) -> str:
    """清洗归一并生成唯一的 MD5 指纹"""
    if not name:
        return ""
    clean_str = re.sub(r'[\s\W_]+', '', name.lower())
    return hashlib.md5(clean_str.encode('utf-8')).hexdigest()


class HashDeduplicator:
    """本地轻量哈希指纹去重器"""

    def __init__(self, record_file: str = "seen_hashes.txt"):
        self.record_file = record_file
        self.seen_hashes = set()
        self._load()

    def _load(self):
        if os.path.exists(self.record_file):
            with open(self.record_file, "r", encoding="utf-8") as f:
                self.seen_hashes = {line.strip() for line in f if line.strip()}
            print(f"🔑 [指纹库] 成功加载 {len(self.seen_hashes)} 个企业历史指纹，秒级去重启动。")
        else:
            print("🔑 [指纹库] 首次运行，已自动创建全新指纹库文件 seen_hashes.txt。")

    def is_seen(self, identifier: str) -> bool:
        """判断公司名或主页链接是否已抓取过"""
        h = get_name_hash(identifier)
        return bool(h and h in self.seen_hashes)

    def add(self, identifier: str):
        """实时存入内存并追加到本地文本文件"""
        h = get_name_hash(identifier)
        if h and h not in self.seen_hashes:
            self.seen_hashes.add(h)
            with open(self.record_file, "a", encoding="utf-8") as f:
                f.write(f"{h}\n")


# 全局单例对象，方便直接导入使用
dedup = HashDeduplicator()