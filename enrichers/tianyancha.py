import asyncio
import random
import re
from urllib.parse import quote_plus, urljoin
from utils.phone import deduplicate_phone_list


class TianyanchaEnricher:
    def __init__(self):
        self.search_count = 0  # 累计检索总数
        self.batch_count = 0  # 当前轮次检索计数
        self.next_big_rest_target = random.randint(17, 20)  # 动态随机大休眠阈值 (17-20)

    async def _human_rest(self, min_sec: float = 3.23, max_sec: float = 6.29, desc: str = ""):
        rest_time = round(random.uniform(min_sec, max_sec), 2)
        if desc:
            print(f"      ⏱️ [天眼查安全冷却] {desc}，等待 {rest_time} 秒...")
        await asyncio.sleep(rest_time)

    async def _big_rest(self, min_sec: float = 45.0, max_sec: float = 60.0):
        rest_time = round(random.uniform(min_sec, max_sec), 2)
        print("\n" + "=" * 65)
        print(f"☕ [天眼查防风控大休眠] 本轮已连续检索 {self.batch_count} 家商户，触发深度冷却！")
        print(f"⏳ 正在安全静默等待 {rest_time} 秒，降低触发滑块验证码的概率...")
        print("=" * 65 + "\n")
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
                    await self._human_rest(3.23, 6.29, desc="验证通过缓和停顿")
                    return

            print("⚠️ 人工验证等待超时，跳过该商户天眼查查询。")

    def _clean_capital(self, val: str) -> str:
        """金额提纯：提取规范金额及币种"""
        if not val:
            return "未公开"
        v = val.strip()
        if v in ["-", "--", "—", "无"]:
            return "-"
        if any(b in v for b in ["未公开", "未公布", "暂无"]):
            return "未公开"

        m = re.search(r'([\d\.]+\s*(?:万人民币|万美[元金]|万港币|万|人民币|美元|元))', v)
        if m:
            return m.group(1).strip()

        m2 = re.search(r'([\d\.]+\s*万)', v)
        if m2:
            return m2.group(1).strip()

        return v.split('\n')[0].strip()

    def _clean_insured(self, raw_val: str) -> str:
        """
        参保人数提纯：
        以空白与换行安全切词，彻底解决把 2024/2025/2026 年报年份粘连在人数后的问题
        """
        if not raw_val:
            return "未公开"

        v = raw_val.strip()
        if any(b in v for b in ["未公开", "未公布", "暂无", "-", "--", "—", "无"]):
            return "未公开"

        # 1. 逆向精准解耦：如果已经发生粘连 (如 152025, 1742024, 32025人)
        m_stuck = re.match(r'^(\d+?)(201\d|202\d)(?:\s*年报|\s*人)?$', v)
        if m_stuck:
            real_count = m_stuck.group(1)
            return f"{real_count}人"

        # 2. 空白切词：如果文本中间带有空格或换行 (如 "15 2025年报" 或 "15\n2025年报")
        tokens = [t.strip() for t in re.split(r'[\s\n\r\t]+', v) if t.strip()]
        if tokens:
            first = tokens[0]
            first_num = re.sub(r'人$', '', first)
            if first_num.isdigit():
                num = int(first_num)
                # 排除只有一个年份误入的情况
                if not (len(tokens) == 1 and 2018 <= num <= 2030):
                    return f"{num}人"

        # 3. 剥离末尾年份与年报后缀
        cleaned = re.sub(r'[\s\?？]*\b(?:201\d|202\d)\s*年报.*$', '', v)
        cleaned = re.sub(r'\s*年报.*$', '', cleaned).strip()

        if not cleaned or cleaned in ["-", "--"]:
            return "未公开"

        # 4. 提取主体纯数字
        num_match = re.search(r'^(\d+)', cleaned)
        if num_match:
            num = int(num_match.group(1))
            if 2018 <= num <= 2030:
                return "未公开"
            return f"{num}人"

        return "未公开"

    async def search_and_enrich(self, page, company_name: str) -> dict:
        info = {
            "phone": "",
            "email": "",
            "contact_person": "",
            "contact_title": "",
            "registered_company": "",
            "registered_address": "",
            "registered_capital": "未公开",
            "paid_in_capital": "-",
            "insured_count": "未公开"
        }
        if not company_name or len(company_name.strip()) < 3:
            return info

        self.search_count += 1
        self.batch_count += 1

        # 触发 17-20 家大休眠机制 (45-60 秒)
        if self.batch_count >= self.next_big_rest_target:
            await self._big_rest(min_sec=45.0, max_sec=60.0)
            self.batch_count = 0
            self.next_big_rest_target = random.randint(17, 20)

        search_kw = company_name.strip()
        print(
            f"      🔎 [天眼查检索 #{self.search_count} | 本轮: {self.batch_count}/{self.next_big_rest_target}] 正在检索: {search_kw}")

        try:
            await page.goto(
                f"https://www.tianyancha.com/search?key={quote_plus(search_kw)}",
                wait_until="domcontentloaded",
                timeout=25000
            )
            await self._handle_captcha_if_needed(page)
            await page.mouse.wheel(0, random.randint(200, 450))
            await self._human_rest(2.0, 3.5, desc="卡片初筛")

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

            cap_m = re.search(r'注册资本\s*[:：]?\s*([^\s\n\r]+)', card_text)
            if cap_m:
                info["registered_capital"] = self._clean_capital(cap_m.group(1))

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

            link_el = await card_el.query_selector('a[href*="/company/"]')
            if link_el:
                href = await link_el.get_attribute("href")
                if href:
                    detail_url = urljoin("https://www.tianyancha.com", href)
                    await self._human_rest(2.0, 3.5, desc="进详情页读取工商表格")
                    await page.goto(detail_url, wait_until="domcontentloaded", timeout=25000)
                    await self._handle_captcha_if_needed(page)

                    try:
                        await page.wait_for_selector(
                            'table.index_tableBox__ZadJW, table[class*="tableBox"], tr:has-text("注册资本")',
                            state="attached",
                            timeout=8000
                        )
                    except Exception:
                        await asyncio.sleep(1.5)

                    # 精确提取纯文本节点，不把 <a>/<div>/<span> 徽标文本带入
                    table_data = await page.evaluate("""
                        () => {
                            const result = {};
                            const tables = Array.from(document.querySelectorAll('table.index_tableBox__ZadJW, table[class*="tableBox"], table'));
                            for (const table of tables) {
                                const rows = Array.from(table.querySelectorAll('tr'));
                                for (const tr of rows) {
                                    const cells = Array.from(tr.children);
                                    for (let i = 0; i < cells.length; i++) {
                                        const cell = cells[i];
                                        const label = (cell.innerText || '').replace(/[\\s\\?？]/g, '');
                                        if (i + 1 < cells.length) {
                                            const valCell = cells[i + 1];

                                            if (label.includes('注册资本') && !result.reg_cap) {
                                                result.reg_cap = valCell.innerText ? valCell.innerText.trim() : '';
                                            } else if (label.includes('实缴资本') && !result.paid_cap) {
                                                result.paid_cap = valCell.innerText ? valCell.innerText.trim() : '';
                                            } else if (label.includes('参保人数') && !result.insured) {
                                                let directText = '';
                                                for (const node of valCell.childNodes) {
                                                    if (node.nodeType === Node.TEXT_NODE) {
                                                        directText += node.textContent;
                                                    }
                                                }
                                                directText = directText.trim();

                                                if (/^\\d+/.test(directText)) {
                                                    result.insured = directText;
                                                } else {
                                                    const clone = valCell.cloneNode(true);
                                                    for (const el of clone.querySelectorAll('*')) {
                                                        if ((el.innerText || '').includes('年报')) {
                                                            el.remove();
                                                        }
                                                    }
                                                    result.insured = clone.innerText ? clone.innerText.trim() : '';
                                                }
                                                result.insured_raw = valCell.innerText ? valCell.innerText.trim() : '';
                                            } else if ((label.includes('企业地址') || label.includes('注册地址') || label === '地址') && !result.addr) {
                                                result.addr = valCell.innerText ? valCell.innerText.trim() : '';
                                            }
                                        }
                                    }
                                }
                            }
                            return result;
                        }
                    """)

                    if table_data.get("reg_cap"):
                        info["registered_capital"] = self._clean_capital(table_data["reg_cap"])

                    if table_data.get("paid_cap"):
                        info["paid_in_capital"] = self._clean_capital(table_data["paid_cap"])

                    target_insured = table_data.get("insured") or table_data.get("insured_raw") or ""
                    if target_insured:
                        info["insured_count"] = self._clean_insured(target_insured)

                    if not info["registered_address"] and table_data.get("addr"):
                        info["registered_address"] = table_data["addr"].split('\n')[0].strip()

                    if not collected_phones:
                        detail_text = await page.evaluate("document.body.innerText")
                        for dp in re.findall(r'(?:电话|联系方式)\s*[:：]?\s*([+\d\s\-]+)', detail_text):
                            if "暂无" not in dp and "登录" not in dp and len(re.sub(r'\D', '', dp)) >= 7:
                                collected_phones.append(dp.strip())

            deduped = deduplicate_phone_list(collected_phones)
            if deduped:
                info["phone"] = " / ".join(deduped[:2])

            print(
                f"      ✅ [天眼查成功] 公司: {info['registered_company'] or '未查到'} | "
                f"注册资本: {info['registered_capital']} | "
                f"实缴资本: {info['paid_in_capital']} | "
                f"参保人数: {info['insured_count']}"
            )

        except Exception as e:
            print(f"      ⚠️ [天眼查检索异常] {search_kw}: {e}")

        # 单次检索冷却严格控制在 3.23 ~ 6.29 秒
        await self._human_rest(3.23, 6.29, desc="单次检索冷却")
        return info