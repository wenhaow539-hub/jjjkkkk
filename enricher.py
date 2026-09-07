import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def search_company_info(company_name: str) -> str:
    """通过搜索引擎反查公司独立站及公开联系方式"""
    query = f"{company_name} official site contact email"
    search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return ""

        soup = BeautifulSoup(resp.text, 'html.parser')
        snippets = []
        # 提取前 5 条搜索结果的标题与摘要
        for result in soup.find_all('div', class_='result__body')[:5]:
            title = result.find('a', class_='result__snippet')
            snippet = result.find('a', class_='result__snippet')
            if snippet:
                snippets.append(snippet.get_text(strip=True))

        return "\n".join(snippets)
    except Exception as e:
        print(f"反查搜索异常: {e}")
        return ""