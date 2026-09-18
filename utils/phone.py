import re

# —— 平台占位/客服号码黑名单 ——
# 这些都是**页面 UI 文本**，不是企业电话，实测已混进报表 12 行：
#   `+86 400-6080000` 出现 12 次（天眼查自家客服电话，页面页脚/悬浮条上到处都有）
# 正则 `(?:电话|联系方式)\s*[:：]?\s*([+\d\s\-]+)` 会把它们当号码抓走 ——
# 尤其 `更多电话 3` 这种，`电话` 后面正好跟一个数字，命中率 100%。
# 这里按「纯数字归一化后」比对，`+86 `/`-`/空格 等写法差异都能拦住。
PLACEHOLDER_PHONE_DIGITS = {
    "4006080000",       # 天眼查客服热线
    "4006080001",       # 邻近号段，一并拦（同源占位）
    "4008888888",       # 常见示例号
    "8008201234",
}

# 明显是 UI 文本而非号码的片段（出现在原文里就直接判废）
PLACEHOLDER_PHONE_HINTS = (
    "更多电话", "解锁企业电话", "快速定位目标企业", "登录后查看", "点击查看",
    "暂无", "未公开", "查看", "详情",
)


def sanitize_phone_string(raw_p: str) -> str:
    clean_val = raw_p.strip()

    # ① UI 文本先拦掉：这类根本不含合法号码结构，别指望下面的数字规则救回来
    if any(h in clean_val for h in PLACEHOLDER_PHONE_HINTS):
        return ""

    digits = re.sub(r'\D', '', clean_val)
    if digits.startswith("86"):
        digits = digits[2:]

    # ② 占位/客服号码黑名单
    if digits in PLACEHOLDER_PHONE_DIGITS:
        return ""

    # ③ 手机号
    mobile_match = re.search(r'(1[3-9]\d{9})$', digits)
    if mobile_match:
        return f"+86 {mobile_match.group(1)}"

    # ④ 座机：**按区号前缀表切分**，绝不按长度盲猜。
    # ⚠️ 这里踩过两次坑，都记下来：
    #    · `010-12345678` 若按 `\d{1,3}` 盲切 → 区号 `101` + `2345678`（不存在的号）；
    #    · `0731-05413961`（长沙，本地号带前导 0）若按 `0?(\d{1,3})` 切 → `7310`+`5413961`。
    #    根因：**区号长度不固定（2/3/4 位）且无规律**，只能查表。
    #    口径统一为「不带前导 0 的区号」（010 → 10，0755 → 755），与表里的键一致。
    reg = _split_area_code(digits)
    if reg:
        area, local = reg
        return f"+86 {area}-{local}"

    # ⑤ 400/800 服务号（10 位）
    if re.fullmatch(r'[48]00\d{7}', digits):
        return f"+86 {digits[:3]}-{digits[3:6]}-{digits[6:]}"

    return clean_val


# —— 国内固定电话区号表（不带前导 0）——
# 只用来看「该切几位」，不做归属地校验。覆盖直辖市/省会/主要外贸城市 + 广东全省。
# 没命中表的走「本地号 8 位」兜底（见 `_split_area_code`）。
AREA_CODES_3 = {           # 3 位区号：去掉前导 0 后是 3 位（如 0755 → 755）
    "755", "769", "760", "756", "757", "758", "752", "750", "751", "753", "754", "759", "766", "662", "663",
    "10", "20", "21", "22", "23", "24", "25", "27", "28", "29",
    "311", "371", "371", "512", "519", "531", "532", "551", "571", "574", "579",
    "591", "592", "595", "597", "731", "771", "791", "871", "891",
}
AREA_CODES_4 = {           # 4 位区号（如 0769 之外的 4 位乡镇号段）
    "7699",
}
# 2 位区号（01x/02x 去掉前导 0，如 010 → 10、021 → 21）
AREA_CODES_2 = {"10", "20", "21", "22", "23", "24", "25", "27", "28", "29"}


def _split_area_code(digits: str) -> tuple[str, str] | None:
    """把纯数字座机号切成 (区号, 本地号)，切不出来返回 None。

    优先按「表命中的最长区号」切；表里没有则用「8 位本地号」倒推区号。
    """
    # 去国际前缀
    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) > 11:
        digits = digits[2:]
    if not digits or not digits.isdigit():
        return None

    # 统一去掉区号前导 0 再查表（010 → 10、0755 → 755）
    body = digits[1:] if digits.startswith("0") else digits

    for n in (4, 3, 2):
        if len(body) > n + 6:
            area, local = body[:n], body[n:]
            if (n == 4 and area in AREA_CODES_4) or \
               (n == 3 and area in AREA_CODES_3) or \
               (n == 2 and area in AREA_CODES_2):
                if len(local) in (7, 8):
                    return area, local

    # 表里没有：本地号通常是 7~8 位，区号取剩下的高位
    if 10 <= len(body) <= 12:
        for local_len in (8, 7):
            area = body[:len(body) - local_len]
            if 2 <= len(area) <= 4 and body[len(body) - local_len] != "0":
                return area, body[len(body) - local_len:]

    return None

def get_canonical_phone(raw_phone: str) -> str:
    if not raw_phone:
        return ""
    main_part = re.split(r'(?:ext|分机|转)', raw_phone, flags=re.I)[0]
    digits = re.sub(r'\D', '', main_part)

    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) >= 11:
        digits = digits[2:]

    if digits.startswith("0") and len(digits) >= 10:
        digits = digits[1:]

    return digits

def score_phone_format(val: str) -> int:
    score = 0
    if "+" in val: score += 5
    if "-" in val: score += 3
    if " " in val: score += 2
    if re.search(r'(?:\+86|86)[\s\-]*0\d', val): score -= 4
    if re.match(r'^\+?\d+$', val.strip()): score -= 1
    return score

def deduplicate_phone_list(phones: list[str]) -> list[str]:
    seen_dict: dict[str, str] = {}
    for p in phones:
        clean_p = sanitize_phone_string(p)
        fingerprint = get_canonical_phone(clean_p)
        if not fingerprint or len(fingerprint) < 7:
            continue
        if fingerprint not in seen_dict:
            seen_dict[fingerprint] = clean_p
        else:
            if score_phone_format(clean_p) > score_phone_format(seen_dict[fingerprint]):
                seen_dict[fingerprint] = clean_p
    return list(seen_dict.values())