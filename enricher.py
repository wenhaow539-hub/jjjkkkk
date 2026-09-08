import re
import requests
import urllib3

# 忽略 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
}

PROVINCES = "京津沪渝冀晋蒙辽吉黑苏浙皖闽赣鲁豫鄂湘粤琼川贵云陕甘青宁新"

# 1. 工信部 ICP 备案正则 (如：粤ICP备19082517号、京ICP备12345678号-1)
ICP_REGEX = rf'([{PROVINCES}]\s*ICP\s*备\s*\d+\s*号(?:-\d+)?)'

# 2. 全国公安机关网安备案正则 (如：粤公网安备 44030502008518号)
POLICE_REGEX = rf'([{PROVINCES}]?\s*公网安备\s*[\d\s]+号?)'

def check_website_filing(website: str) -> str:
    """
    穿透企业官网首页及页脚，检测并提取：
    1. 工信部 ICP 备案号 (如 粤ICP备19082517号)
    2. 公安网安备案号 (如 粤公网安备 44030502008518号)
    """
    if not website or not website.startswith("http"):
        return "无独立官网"

    print(f"      🛡️ 正在探测官网合规备案: {website}")
    try:
        resp = requests.get(website, headers=HEADERS, timeout=8, verify=False)
        html = resp.content.decode(resp.encoding or 'utf-8', errors='ignore')

        filings = []

        # 匹配 ICP 备案
        icp_match = re.search(ICP_REGEX, html, re.I)
        if icp_match:
            clean_icp = re.sub(r'\s+', '', icp_match.group(1)).strip()
            filings.append(clean_icp)

        # 匹配公安网安备案
        police_match = re.search(POLICE_REGEX, html)
        if police_match:
            clean_police = re.sub(r'\s+', ' ', police_match.group(0)).strip()
            filings.append(clean_police)

        # 备选：从公安备案官方跳转外链中提取 recordcode
        if not police_match:
            record_code_match = re.search(r'beian\.(?:gov|mps)\.cn/.*?recordcode=(\d+)', html, re.I)
            if record_code_match:
                filings.append(f"公网安备:{record_code_match.group(1)}")

        if filings:
            result_str = " | ".join(filings)
            print(f"      📌 [备案抓取成功] {result_str}")
            return result_str

        return "无备案"

    except Exception as e:
        print(f"      ⚠️ 官网无法连通或检测超时: {e}")
        return "官网无法访问/无"