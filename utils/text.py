import html as html_lib
import re

def is_valid_company_name(name: str) -> bool:
    """公司名白名单校验：必须含公司后缀词，且不命中产品/平台垃圾词。

    （P1 修复）原逻辑对无后缀名称走"不含产品词即通过"的黑名单兜底，
    会放行 "Verified Supplier" / "Hot Products" 等垃圾锚文本；现改为必须命中公司后缀。
    """
    if not name or not isinstance(name, str):
        return False
    clean_name = re.sub(r'^[,\.;:\s"\'\(]+|[,\.;:\s"\'\)]+$', '', name.strip())
    if len(clean_name) < 4 or len(clean_name) > 100:
        return False

    name_lower = clean_name.lower()
    company_pattern = (
        r'(\bco\.?,?\s*ltd(?:\.|\b)|'
        r'\bpte\.?\s*ltd(?:\.|\b)|'
        r'\bltd(?:\.|\b)|'
        r'\blimited\b|'
        r'\bllc(?:\.|\b)|'
        r'\binc(?:\.|\b)|'
        r'\bcorp(?:\.|\b)|'
        r'\bcorporation\b|'
        r'\bgmbh(?:\.|\b)|'
        r'\bs\.?r\.?l(?:\.|\b)|'
        r'\bs\.?a(?:\.|\b)|'
        r'\bsdn\.?\s*bhd(?:\.|\b)|'
        r'\bcompany\b|\bgroup\b|\bfactory\b|'
        r'\btechnology\b|\btechnologies\b|'
        r'\benterprise\b|\bindustrial\b)'
    )
    if not re.search(company_pattern, name_lower):
        return False

    junk_phrases = [
        "gaming monitor", "wholesale", "factory price", "moq", "pieces",
        "frameless", "hot sale", "hot products", "verified supplier",
        "gold member", "gold supplier", "trade assurance", "send inquiry",
        "inquiry now", "chat now", "contact supplier", "request quote", "view more",
    ]
    return not any(pk in name_lower for pk in junk_phrases)

def clean_token(val: str) -> str:
    if not val:
        return ""
    val = re.sub(r'[\r\n\t]+', ' ', val).strip()
    for b in ["send inquiry", "inquiry now", "chat now", "inquire", "contact supplier", "verified", "view more"]:
        if b in val.lower():
            return ""
    return val

def normalize_website(url: str, exclude_domain: str = "") -> str:
    if not url:
        return ""
    url = re.sub(r'[,;:\s<>"\'\)]+$', '', url.strip())
    if exclude_domain and exclude_domain.lower() in url.lower():
        return ""
    # 站内相对路径不可能是"独立站"：必须挡在补协议之前，
    # 否则 /favicon.ico 会被当成域名并补成 http://favicon.ico
    if url.startswith(("/", "./", "../")):
        return ""
    if any(url.lower().endswith(ext) for ext in [
        '.png', '.jpg', '.jpeg', '.gif', '.css', '.js', '.svg', '.webp',
        '.ico', '.woff', '.woff2', '.ttf', '.eot', '.map', '.json', '.xml',
    ]):
        return ""
    clean_domain = re.sub(r'^https?://', '', url).split('/')[0]
    if '.' not in clean_domain or len(clean_domain) < 4:
        return ""
    # 顶级域必须是纯字母（把 favicon.ico / xxx.js 这类"看起来像域名"的文件名挡住）；
    # 放行 punycode（中文域名如 xn--fiqs8s）
    tld = clean_domain.rsplit('.', 1)[-1]
    if not re.fullmatch(r'[a-zA-Z]{2,}', tld) and not tld.lower().startswith('xn--'):
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url}"
    return url

def html_to_clean_text(html_content: str) -> str:
    if not html_content:
        return ""
    clean = re.sub(r'<(script|style|head|noscript|svg)[^>]*>.*?</\1>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<(?:br|p|div|tr|li|h[1-6]|section|article)[^>]*>', '\n', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<[^>]+>', ' ', clean)
    clean = html_lib.unescape(clean)
    lines = [re.sub(r'[ \t\xa0\u3000]+', ' ', line).strip() for line in clean.splitlines()]
    return '\n'.join([line for line in lines if line])