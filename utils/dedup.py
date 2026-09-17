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


def commit_lead_fingerprints(leads) -> int:
    """把**已成功入库**的线索写进指纹库，返回真正新增的条数。

    ⚠️ 只能在**落盘成功之后**调用。

    指纹库的语义应当等价于「这家已经处理完了」，而"处理完"的终点是**进报表**，
    不是"抓过详情页"。写在采集阶段会形成一个不可逆的漏洞：

        采到 → 因缺中文名 / 工商库查不到被剔除 → 指纹却已经写下了 → **永久消失**，
        以后每次搜索都会被 `is_seen()` 跳过，再也没机会补救。

    这个坑实测过：`seen_hashes.txt` 累积到 3218 条时，报表只有 286 行 ——
    差额全是「采过却从未入库」的公司。改成落盘后写，被剔除的公司下轮还能重新采到。

    代价（有意的取舍）：被剔除的公司每轮都会被重新抓一次详情，多花一点请求。
    换来的是「不漏采」，通常比「省请求」更值。

    每家记**两个**写法：英文公司名 + 中文工商名 —— 两者哈希不同（实测
    `...CO., LIMITED` 与 `...CO.,LTD` 也会算出不同哈希），都记上才挡得住。
    """
    n = 0
    for lead in leads:
        for attr in ("company", "registered_company"):
            name = getattr(lead, attr, "") or ""
            name = str(name).strip()
            if name and dedup.add(name):
                n += 1
    return n