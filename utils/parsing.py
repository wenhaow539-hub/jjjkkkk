import re
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from utils.logger import get_logger

logger = get_logger("parsing")

_LABEL_TAGS = ["dt", "span", "div", "label", "p", "li", "strong"]


def soup_from_html(html: str) -> Optional[BeautifulSoup]:
    if not html:
        return None
    try:
        return BeautifulSoup(html, "html.parser")
    except Exception:
        return None


def _is_valid_value(value: str, stop_words: Iterable[str] = ()) -> bool:
    if not value or len(value) > 200:
        return False
    low = value.lower()
    if any(sw.lower() in low for sw in stop_words):
        return False
    if re.search(r'[:：]\s*$', value):
        return False
    return True


def find_labeled_value(soup: Optional[BeautifulSoup], label_patterns: list, stop_words: Iterable[str] = ()) -> str:
    """DOM 结构化兜底提取：在 dt/dd、表格相邻单元格、相邻兄弟节点中寻找 标签->值。

    与 utils.text 的纯文本行匹配互补：页面结构改版导致行式匹配失效时，
    该方法基于 DOM 层级关系仍有机会命中，降低解析零容错风险。
    """
    if soup is None:
        return ""
    stops = tuple(stop_words)
    for pat_str in label_patterns:
        try:
            pat = re.compile(rf"^\s*{pat_str}\s*[:：]?\s*$", re.I)
        except re.error:
            continue

        for dt in soup.find_all("dt"):
            if pat.match(dt.get_text(strip=True)):
                dd = dt.find_next_sibling("dd")
                if dd:
                    value = dd.get_text(" ", strip=True)
                    if _is_valid_value(value, stops):
                        return value

        for cell in soup.find_all(["th", "td"]):
            if pat.match(cell.get_text(strip=True)):
                nxt = cell.find_next_sibling(["td", "th"])
                if nxt:
                    value = nxt.get_text(" ", strip=True)
                    if _is_valid_value(value, stops):
                        return value

        for el in soup.find_all(_LABEL_TAGS):
            if pat.match(el.get_text(strip=True)):
                sib = el.find_next_sibling()
                if sib is not None and sib.name not in ("script", "style"):
                    value = sib.get_text(" ", strip=True)
                    if _is_valid_value(value, stops):
                        return value
    return ""
