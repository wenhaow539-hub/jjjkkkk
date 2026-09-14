import json
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger("checkpoint")

CHECKPOINT_DIR = Path("checkpoints")


class LeadCheckpoint:
    """基于 JSONL 的商户级断点存储：一行一个商户，加载时后写覆盖先写。

    每个商户记录各阶段（爬取/独立站探测/LLM 质检/天眼查）的完成标记与结果，
    流水线任意时刻中断后重跑，均可从上次完成的阶段继续，不再全量重来。
    """

    def __init__(self, platform: str, keyword: str, resume: bool = True):
        safe_kw = "".join(c if (c.isascii() and c.isalnum()) else "_" for c in keyword.strip().lower())
        CHECKPOINT_DIR.mkdir(exist_ok=True)
        self.path = CHECKPOINT_DIR / f"{platform}_{safe_kw or 'default'}.jsonl"
        self.records: dict = {}
        if resume:
            self._load()
        else:
            self._truncate()

    @staticmethod
    def _key(store_url: str, company: str = "") -> str:
        return (store_url or company or "").strip().rstrip("/")

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = self._key(rec.get("store_url", ""), rec.get("company", ""))
                    if key:
                        self.records[key] = rec
            if self.records:
                logger.info(f"🔑 [断点库] 恢复 {len(self.records)} 条商户进度记录: {self.path.name}")
        except Exception as e:
            logger.warning(f"⚠️ [断点库] 读取失败({e!r})，将忽略历史进度重新开始。")

    def _truncate(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("")
        except Exception as e:
            logger.warning(f"⚠️ [断点库] 重置文件失败({e!r})。")

    def get(self, store_url: str, company: str = "") -> Optional[dict]:
        return self.records.get(self._key(store_url, company))

    def upsert(self, record: dict):
        key = self._key(record.get("store_url", ""), record.get("company", ""))
        if not key:
            return
        self.records[key] = record
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"⚠️ [断点库] 写入失败({e!r})，进度仅保留在内存中。")

    def all_records(self) -> list:
        return list(self.records.values())
