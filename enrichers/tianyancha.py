import asyncio
import random
import re
from urllib.parse import quote_plus, urljoin
from utils.phone import deduplicate_phone_list

class TianyanchaEnricher:
    async def _human_rest(self, min_sec: float = 3.0, max_sec: float = 5.0, desc: str = ""):
        rest_time = round(random.uniform(min_sec, max_sec), 2)
        if desc:
            print(f"      ⏱️ [天眼查安全冷却] {desc}，等待 {rest_time} 秒...")
        await asyncio.sleep(rest_time)

    async def _handle_captcha_if_needed(self, page):
        is_blocked = False
        current_url = page.url.lower()

        if "sec.tianyancha.com" in current_url or "verify" in current_url:
            is_blocked = True

        captcha_el = await page.query_selector('.sec-captcha, .geetest_holder, div[class*="captcha"], #nc_1_wrapper')
        if captcha_el:
            is_blocked = True

        if is_blocked:
            print("\n" + "!" * 60)
            print("🚨 [天眼查风控触发] 检测到滑块验证码 / 安全防护拦截！")
            print("👉 请切回已打开的 Chrome 浏览器窗口，【手动滑动完成验证】。")
            print("⏳ 流水线已自动暂停等待，验证通过后会自动恢复运行...")
            print("!" * 60 + "\n")

            for _ in range(120):
                await asyncio.sleep(2)
                cur_url = page.url.lower()
                captcha_now = await page.query_selector('.sec-captcha, .geetest_holder, div[class*="captcha"]')
                if "sec.tianyancha.com" not in cur_url and not captcha_now:
                    print("✅ [人工验证成功] 恢复自动化抓取！\n")
                    await self._human_rest(2.0, 3.5, desc="缓和停顿")
                    return

            print("⚠️ 人工验证等待超时，跳过该商户天眼查查询。")

    async def search_and_enrich(self, page, company_name: str) -> dict:
        info = {
            "phone": "",
            "email": "",
            "contact_person": "",
            "contact_title": "",
            "registered_company": "",
            "registered_address": ""
        }
        if not company_name or len(company_name.strip()) < 3:
            return info

        search_kw = company_name.strip()
        print(f"      🔎 [天眼查检索] 正在检索: {search_kw}")

        try:
            await page.goto(
                f"https://www.tianyancha.com/search?key={quote_plus(search_kw)}",
                wait_until="domcontentloaded",
                timeout=25000
            )
            await self._handle_captcha_if_needed(page)
            await page.mouse.wheel(0, random.randint(200, 450))
            await self._human_rest(1.5, 2.5, desc="观察页面")

            card_el = await page.query_selector('div[class*="search-item"], .search-block, div[class*="result-item"]')
            if not card_el:
                print(f"      ℹ️ [天眼查] 未找到匹配的企业卡片: {search_kw}")
                return info

            card_text = (await card_el.inner_text()).strip()
            title_el = await card_el.query_selector('a.name, .title, h2, a[href*="/company/"]')
            if title_el:
                c_name = (await title_el.inner_text()).strip()
                c_clean = re.sub(r'[\s_]+', '', c_name)
                if len(c_clean) >= 4:
                    info["registered_company"] = c_clean

            addr_m = re.search(r'(?:注册地址|地址)\s*[:：]?\s*([^\n\r]+)', card_text)
            if addr_m:
                a_clean = addr_m.group(1).strip()
                if "暂无" not in a_clean and "登录" not in a_clean and len(a_clean) >= 5:
                    info["registered_address"] = a_clean

            collected_phones = []
            for pm in re.findall(r'电话\s*[:：]?\s*([+\d\s\-]+)', card_text):
                if "暂无" not in pm and "登录" not in pm and len(re.sub(r'\D', '', pm)) >= 7:
                    collected_phones.append(pm.strip())

            em = re.search(r'邮箱\s*[:：]?\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', card_text)
            if em:
                info["email"] = em.group(1).strip()

            lm = re.search(r'法定代表人\s*[:：]?\s*([^\s\n\r]+)', card_text)
            if lm:
                p = lm.group(1).strip()
                if len(p) <= 8 and p not in ["-", "暂无", "登录"]:
                    info["contact_person"] = p
                    info["contact_title"] = "法定代表人"

            if not collected_phones or not info["registered_address"]:
                link_el = await card_el.query_selector('a[href*="/company/"]')
                if link_el:
                    href = await link_el.get_attribute("href")
                    if href:
                        await self._human_rest(1.0, 1.5, desc="进详情页")
                        await page.goto(urljoin("https://www.tianyancha.com", href), wait_until="domcontentloaded", timeout=20000)
                        await self._handle_captcha_if_needed(page)

                        detail_text = await page.evaluate("document.body.innerText")
                        if not collected_phones:
                            for dp in re.findall(r'(?:电话|联系方式)\s*[:：]?\s*([+\d\s\-]+)', detail_text):
                                if "暂无" not in dp and "登录" not in dp and len(re.sub(r'\D', '', dp)) >= 7:
                                    collected_phones.append(dp.strip())

                        if not info["registered_address"]:
                            d_addr = re.search(r'(?:注册地址|企业地址|地址)\s*[:：]?\s*([^\n\r<]+)', detail_text)
                            if d_addr:
                                a_clean = d_addr.group(1).strip()
                                if "暂无" not in a_clean and "登录" not in a_clean and len(a_clean) >= 5:
                                    info["registered_address"] = a_clean

            deduped = deduplicate_phone_list(collected_phones)
            if deduped:
                info["phone"] = " / ".join(deduped[:2])

            print(f"      ✅ [天眼查成功] 中文名: {info['registered_company'] or '未查到'} | 地址: {info['registered_address'] or '未公开'} | 电话: {info['phone'] or '未公开'}")

        except Exception as e:
            print(f"      ⚠️ [天眼查检索异常] {search_kw}: {e}")

        await self._human_rest(2.5, 4.0, desc="检索冷却")
        return info