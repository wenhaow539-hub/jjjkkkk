import re

def sanitize_phone_string(raw_p: str) -> str:
    clean_val = raw_p.strip()
    digits = re.sub(r'\D', '', clean_val)
    if digits.startswith("86"):
        digits = digits[2:]

    mobile_match = re.search(r'(1[3-9]\d{9})$', digits)
    if mobile_match:
        return f"+86 {mobile_match.group(1)}"

    landline_match = re.search(r'([1-9]\d{1,2})(\d{7,8})$', digits)
    if landline_match:
        return f"+86 {landline_match.group(1)}-{landline_match.group(2)}"

    return clean_val

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