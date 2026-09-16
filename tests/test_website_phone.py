"""enrichers/website.py 电话号码提取的回归用例。

这里的每一条都对应一次实测踩坑，别当普通单测随便删：
  · `86-20-31477658` → 丢前导 0 会变成不存在的 `20-31477658`
  · `+44 7354893854` → 被座机规则切成半截 `44-73548938`（半截号比留空更糟）
  · `010-2019-1688` / `9139369130` → 1688 店铺 slug 与页面 UUID 曾经被当座机

跑法：项目根目录 `python -m pytest tests/ -q`，或直接 `python tests/test_website_phone.py`
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enrichers.website import WebsiteEnricher  # noqa: E402

_ENR = WebsiteEnricher()

# (原始写法, 期望归一化结果)；期望 "" 表示应当判为"不是电话"
CANON_CASES = [
    # --- 国内座机：区号前导 0 必须补回来 ---
    ("86-20-31477658", "020-31477658"),
    ("0086-769-81377158", "0769-81377158"),
    ("769-81377158", "0769-81377158"),          # 广东外贸站常见的裸区号写法
    ("0755-27967077", "0755-27967077"),
    ("020-31477658", "020-31477658"),
    ("010-12345678", "010-12345678"),
    # --- 手机号 ---
    ("15913797691", "15913797691"),
    ("0086 15913797691", "15913797691"),
    ("+86 132-0200-7108", "13202007108"),
    ("13202007108", "13202007108"),
    # --- 400/800 ---
    ("400-888-8888", "400-888-8888"),
    ("800-820-1234", "800-820-1234"),
    # --- 境外号码：整串保留，绝不切分 ---
    ("+65 90656597", "+6590656597"),            # 新加坡
    ("+44 7354893854", "+447354893854"),        # 英国，老逻辑会截成 44-73548938
    ("+1 385-372-3008", "+13853723008"),        # 美国
    ("+852 1234 5678", "+85212345678"),         # 中国香港
    # --- 脏数据：必须判为不是电话 ---
    ("010-2019-1688", ""),                      # 1688 店铺 slug
    ("9139369130", ""),                         # 页面内联 JSON 的模块 UUID
    ("11111111111", ""),
]

# (HTML 片段, 期望 phone)
HTML_CASES = [
    ('<a href="tel:447354893854">call</a>', "+447354893854"),
    ('<a href="tel:0086 15913797691">call</a>', "15913797691"),   # 属性值含空格
]


def test_canon_phone():
    for src, want in CANON_CASES:
        assert _ENR._canon_phone(src) == want, f"_canon_phone({src!r}) 应为 {want!r}"


def test_tel_href():
    for html, want in HTML_CASES:
        phone, _ = _ENR._extract_contact_info(html)
        assert phone == want, f"tel: 链接 {html!r} 应解析为 {want!r}"


def test_mixed_text_no_truncation():
    """境外号 + 国内号混排时，不能出现半截号，区号也要补全。"""
    text = "Tel: 86-20-31477658  Mob/WhatsApp: +44 7354893854  Hotline: 0086-769-81377158"
    phone, _ = _ENR._extract_contact_info(text)
    assert "44-73548938" not in phone, f"境外号被截断: {phone!r}"
    assert "020-31477658" in phone, f"区号前导 0 丢失: {phone!r}"
    assert "0769-81377158" in phone, f"座机未提取: {phone!r}"


if __name__ == "__main__":
    failed = 0
    for src, want in CANON_CASES:
        got = _ENR._canon_phone(src)
        ok = got == want
        failed += not ok
        print(f"  {'OK  ' if ok else 'FAIL'} _canon_phone({src!r:24s}) = {got!r:20s} want {want!r}")
    for html, want in HTML_CASES:
        got, _ = _ENR._extract_contact_info(html)
        ok = got == want
        failed += not ok
        print(f"  {'OK  ' if ok else 'FAIL'} tel-href {html!r:36s} -> {got!r:18s} want {want!r}")
    print(f"\n失败 {failed} 项")
    sys.exit(1 if failed else 0)
