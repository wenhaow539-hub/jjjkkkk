import hashlib
import os
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

    # ------------------------------------------------------------------ #
    # 撤销（2026-09-17 新增）
    # ------------------------------------------------------------------ #
    # 口径改成「候选即写指纹 + 剔除时回滚」后必须能删：
    # 候选阶段写是为了不再重复采到同一家（省掉白跑的详情 + 两家补全），
    # 但没入库的必须撤掉，否则重演"采过却从未入库"的永久黑洞。
    def remove(self, company_name: str) -> bool:
        """撤销一个名字的哈希（会重写落盘文件）。返回是否真的删掉了。"""
        return self.remove_many([company_name]) > 0

    def remove_many(self, names: Iterable[str]) -> int:
        """批量撤销。**只重写一次文件** —— 逐条 remove 会变成 O(n²) 次落盘。"""
        hashes = {get_company_hash(n) for n in names}
        hashes.discard("")
        if not hashes:
            return 0
        with self._lock:
            hit = hashes & self.seen_hashes
            if not hit:
                return 0
            self.seen_hashes -= hit
            self._rewrite()
            return len(hit)

    def _rewrite(self) -> None:
        """全量重写文件。**必须在持有 self._lock 时调用。**

        为什么不像 `add` 那样追加：删除没法用追加表达。
        文件是纯哈希清单（千级），重写成本可忽略。
        用「写临时文件 + os.replace」原子替换 —— 中途崩只会保留旧库，
        不会留下写了一半的残文件（这个库已经是用户数据，不能写坏）。
        """
        try:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.record_path.with_name(self.record_path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("".join(f"{h}\n" for h in sorted(self.seen_hashes)))
            os.replace(tmp, self.record_path)
        except Exception as e:
            print(f"⚠️ [指纹库] 重写（撤销）失败: {e}")

    def count(self) -> int:
        with self._lock:
            return len(self.seen_hashes)

dedup = HashDeduplicator()


def mark_fingerprints(names: Iterable[str]) -> int:
    """**候选阶段**立即登记指纹，返回真正新增的条数。

    2026-09-17 用户口径：候选时就直接进指纹库 —— 这样同一轮/后续批次不会再把
    同一家采一遍（省掉白跑的详情页 + 独立站 + 两家工商补全）。
    代价是必须配套回滚：没入库的要用 `rollback_fingerprints()` 撤掉。
    """
    n = 0
    for name in names:
        s = str(name or "").strip()
        if s and dedup.add(s):
            n += 1
    return n


def rollback_fingerprints(names: Iterable[str]) -> int:
    """撤除这些名字的指纹，返回真正删掉的条数。

    用于「候选时已登记、但最终没入库」的回滚（无中文名 / 工商库查空被剔除）。
    ⚠️ 不撤的话会重演"采过却从未入库"的永久黑洞：
       实测 `seen_hashes.txt` 累积到 3218 条时，报表只有 286 行，差额全是这种。
    """
    cleaned = [str(n).strip() for n in names]
    return dedup.remove_many([c for c in cleaned if c])


def commit_lead_fingerprints(leads) -> int:
    """兜底确认：把**已成功入库**的线索写进指纹库，返回真正新增的条数。

    ⚠️ 现在是**兜底**而不是主路径 —— 主路径是采集端的 `mark_fingerprints()`
    （候选即写）+ 剔除端的 `rollback_fingerprints()`。

    保留它的理由：无论哪条采集路径、无论中间被谁改了逻辑，
    "进了报表的公司一定在指纹库里" 这件事都要成立。已写过的会因哈希重复而返回 0，
    所以重复调用是安全的。

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