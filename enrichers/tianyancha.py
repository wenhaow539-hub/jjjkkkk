import asyncio
import math
import random
import re
import time
from urllib.parse import quote_plus, urljoin

import config
from enrichers.captcha_solver import CaptchaProviderError, CaptchaSolver
from utils.logger import get_logger
from utils.phone import deduplicate_phone_list

logger = get_logger("enricher.tianyancha")


class TianyanchaEnricher:
    # 触发风控的判定线索。天眼查的拦截页是 `tianyancha.com/sorry/captcha?from=...&token=...`，
    # 注意它**不含** "verify" 字样 —— 原实现只查 "verify"/"sec.tianyancha.com"，会漏判。
    CAPTCHA_URL_HINTS = ("sec.tianyancha.com", "/sorry/captcha", "/captcha", "verify")
    CAPTCHA_SELECTORS = (
        '.sec-captcha', '.geetest_holder', 'div[class*="captcha"]', '#nc_1_wrapper',
    )
    # 点选弹窗的提示文字（两段式验证的第二段）。
    # 极验的措辞不止一种，所以取一个**较宽的集合**：宁可多认（多花 0.025 元试一次），
    # 也不要漏认 —— 漏认的表现是"按钮点了但第二段没出来"，排查成本远高于一次打码费。
    POINT_HINT_RE = re.compile(r'请在下图|依次点击|按顺序点击|请按顺序|点击下图|依次点选|按提示顺序')
    # 第一段那个「点击按钮开始验证」按钮的文案变体
    START_BTN_TEXTS = ("点击按钮开始验证", "点击开始验证", "开始验证", "点击验证", "开始校验")

    # 搜索结果卡片的选择器（多处复用，避免不一致）
    CARD_SELECTOR = 'div[class*="search-item"], .search-block, div[class*="result-item"]'

    # 极验控件的渲染等待上限（秒）。拦截页 URL 一出现 `_captcha_present` 就为真，
    # 但那时极验 JS 往往还没跑完 —— 必须给渲染留时间，否则按钮必然找不到。
    STAGE_WAIT_FIRST = 12.0    # 等第一段（按钮 or 直接点选）出现
    STAGE_WAIT_SECOND = 15.0   # 点完按钮后，等第二段点选弹窗
    START_BTN_WAIT = 12.0      # 等「开始验证」按钮本身

    # 打码轮数。口径与爱企查的 `captcha_max_attempts=3` 一致：
    # 验证失败后极验会**换一张新图 + 一组新提示**，所以要"取新图重来"，而不是同图重试。
    # 实测（2026-09-17 17:2x）：提示条 3 个图标、云码只返回 2 个坐标 → 必失败；
    # 而当时只要换图重来一次就能过（同一账号几分钟后弹的那次一次就过了）。
    CAPTCHA_MAX_ROUNDS = 3
    # 单轮点完之后等放行的秒数。**要短**：失败时极验 1~2s 就给结果，
    # 等太久会拖慢换新图的节奏（原先写死 15s，一轮白等）。
    ROUND_RELEASE_WAIT = 6.0

    def __init__(self, captcha_mode: str = "auto", captcha_provider: str | None = None,
                 captcha_wait: float | None = None):
        self.search_count = 0  # 累计检索总数
        self.batch_count = 0  # 当前轮次检索计数
        self.next_big_rest_target = random.randint(17, 20)  # 动态随机大休眠阈值 (17-20)

        # —— 验证码策略（与爱企查对齐的语义）——
        # auto   : 先试打码平台自动过（需 .env 配云码），失败再人工等待
        # manual : 直接人工等待
        # off    : 命中即放弃该家，不等待
        self.captcha_mode = (captcha_mode or "auto").strip().lower()
        if self.captcha_mode not in ("auto", "manual", "off"):
            self.captcha_mode = "auto"
        self.captcha_provider = captcha_provider
        self.captcha_wait = captcha_wait
        self._solver: CaptchaSolver | None = None
        self._solver_inited = False
        # 本次流程内是否遇到过"验证码没过去"，供 search_and_enrich 标记 blocked
        self.last_captcha_failed = False

        # 拟人鼠标：记录我们**自己**上次把鼠标移到哪，作为下一次轨迹的起点。
        # （只记自己的移动；用户手动挪过鼠标我们不知道，但起点的少量偏差不影响真实性）
        self._mouse_pos: tuple[float, float] | None = None
        # 点选落点的二维正态偏移标准差（px）。见 _jitter 的说明。
        self.point_jitter_px = float(
            getattr(config, "CAPTCHA_POINT_JITTER_PX", 3.0) or 0.0
        )

    # ------------------------------------------------------------------ #
    # 拟人鼠标
    # ------------------------------------------------------------------ #
    # 为什么三个动作都不能用 Playwright 的现成 API：
    #   ① `locator.click()` 把鼠标**瞬移**到元素几何中心，零中间点；
    #   ② `mouse.move(x, y, steps=N)` 是**直线匀速**插值，既无曲率也无加减速；
    #   ③ `mouse.down()` 紧接 `mouse.up()`，**按压时长为 0**。
    # 这三条合起来是教科书级的自动化指纹。极验（天眼查验证码用的就是它）
    # 会采集点击坐标与移动轨迹，所以这一步不能省。
    def _human_path(self, x0: float, y0: float, x1: float, y1: float
                    ) -> list[tuple[float, float]]:
        """生成一条带曲率、按 easeInOut 变速的鼠标路径（三次贝塞尔采点）。"""
        dx, dy = x1 - x0, y1 - y0
        dist = math.hypot(dx, dy)
        if dist < 1.0:
            return [(x1, y1)]

        nx, ny = -dy / dist, dx / dist          # 路径的单位法向量
        # 两个控制点：在路径 1/3、2/3 处沿法向随机外凸 —— 这就是"曲线"的来源。
        # 幅度取距离的 6%~20%，方向随机；太小等于直线，太大像在做抛物线。
        c1 = dist * random.uniform(0.06, 0.20) * random.choice((-1.0, 1.0))
        c2 = dist * random.uniform(0.06, 0.20) * random.choice((-1.0, 1.0))
        p1 = (x0 + dx * 0.30 + nx * c1, y0 + dy * 0.30 + ny * c1)
        p2 = (x0 + dx * 0.70 + nx * c2, y0 + dy * 0.70 + ny * c2)

        # 采样点数随距离变化（真人长距离会有更多中间采样）
        n = max(6, min(28, int(dist / 12) + random.randint(4, 10)))
        pts: list[tuple[float, float]] = []
        for i in range(1, n + 1):
            t = i / n
            e = t * t * (3 - 2 * t)             # easeInOut：起步慢、中途快、收尾慢
            mt = 1 - e
            x = (mt ** 3) * x0 + 3 * (mt ** 2) * e * p1[0] \
                + 3 * mt * (e ** 2) * p2[0] + (e ** 3) * x1
            y = (mt ** 3) * y0 + 3 * (mt ** 2) * e * p1[1] \
                + 3 * mt * (e ** 2) * p2[1] + (e ** 3) * y1
            pts.append((x, y))
        return pts

    async def _mouse_start(self, page) -> tuple[float, float]:
        """轨迹起点：懒初始化成视口内的随机点，而不是 (0,0) 这种不可能的位置。"""
        if self._mouse_pos is None:
            try:
                w = await page.evaluate("window.innerWidth") or 1200
                h = await page.evaluate("window.innerHeight") or 800
            except Exception:
                w, h = 1200, 800
            self._mouse_pos = (random.uniform(w * 0.25, w * 0.75),
                               random.uniform(h * 0.20, h * 0.70))
        return self._mouse_pos

    async def _human_move(self, page, x: float, y: float) -> None:
        x0, y0 = await self._mouse_start(page)
        for (px, py) in self._human_path(x0, y0, x, y):
            await page.mouse.move(px, py)
            # 每步之间不等长：真人的移动是变速的（easeInOut 已管整体节奏，
            # 这里再加一点抖动，避免"每一步间隔都一模一样"）
            await asyncio.sleep(random.uniform(0.004, 0.018))
        self._mouse_pos = (x, y)

    async def _human_click(self, page, x: float, y: float) -> None:
        """移动到 (x,y) 再点击：带曲线轨迹、落点停顿、**按压时长**。"""
        await self._human_move(page, x, y)
        await asyncio.sleep(random.uniform(0.04, 0.16))   # 到位后的小停顿再按下
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.13))   # 按压时长（原来是 0）
        await page.mouse.up()
        self._mouse_pos = (x, y)

    async def _human_click_locator(self, page, loc) -> bool:
        """拟人点击一个元素：**落点在元素内随机**，而不是几何中心。

        点按钮时"每次都正中中心"同样是不自然特征（真人手抖，不会次次精准命中中心）。

        ⚠️ **必须先 `scroll_into_view_if_needed()`**：`bounding_box()` 返回的是
        **视口相对坐标**，而 `page.mouse.click(x, y)` 是原始鼠标事件、**不会自动滚动**。
        元素在页面深处时（实测天眼查的「详情」按钮 y=5569、而视口高仅 791），
        坐标会落在视口外 —— 点击**静默失效**（不报错、也不生效）。
        旧的 `locator.click()` 之所以没这个问题，是因为它自带"滚动到可见"。
        换成手工造鼠标事件后，这个能力要自己补回来。
        """
        try:
            await loc.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            box = await loc.bounding_box()
        except Exception:
            box = None
        if not box or box["width"] < 4 or box["height"] < 4:
            try:
                await loc.click(timeout=4000)
                return True
            except Exception:
                return False
        # 尺寸合理性检查：命中一个"比半个视口还大"的元素，说明选择器选到了外层容器。
        # 实测踩到：`div:has-text('点击按钮开始验证')` 的 `.first` 是文档顺序最靠前的
        # 祖先容器 `DIV.container`（929×407），而真按钮只有 112×22。
        # 元素框过大时"随机落点"必然落在空白处 —— 一旦点不中，就别再猜了。
        try:
            vw = await page.evaluate("window.innerWidth") or 1200
            vh = await page.evaluate("window.innerHeight") or 800
        except Exception:
            vw, vh = 1200, 800
        # 阈值取 60%：验证码弹窗本身约 340×385，在小窗口下可能占到视口近一半，
        # 所以门槛要留够余量，只拦那些"明显是整页容器"的元素。
        if box["width"] >= vw * 0.6 or box["height"] >= vh * 0.6:
            logger.warning(
                f"⚠️ [拟人点击] 命中的元素过大（{box['width']:.0f}×{box['height']:.0f}，"
                f"视口 {vw:.0f}×{vh:.0f}），判定为选错元素（多为选择器匹配到祖先容器），放弃点击"
            )
            return False

        # 落点：框内随机，四周各留 15% 边距（别点到元素边缘外）
        mx, my = box["width"] * 0.15, box["height"] * 0.15
        x = box["x"] + random.uniform(mx, box["width"] - mx)
        y = box["y"] + random.uniform(my, box["height"] - my)
        await self._human_click(page, x, y)
        return True

    def _jitter(self) -> tuple[float, float]:
        """点选落点的二维正态偏移（截断到 2σ），默认 σ=3px。

        **为什么不加偏移是错的**：云码返回的是图形**中心**，若原样点击，就会
        每一次都精确落在几何中心。极验服务端**知道每个图形的确切位置**，
        所以"永远钉在中心"是可统计检测的自动化特征 —— 真实用户点击图形时，
        相对中心的偏移是二维正态分布（标准差通常 5~12px）。

        注意这与旋转验证码的结论**不冲突**：那里的落点是滑块位移，服务端
        并不知道"理想落点"是什么，且识别误差本身就让落点天然变化，所以加偏移
        纯属吃掉容差余量。点选恰恰相反 —— 服务端完全知道正确答案在哪。

        幅度取 3px：蝴蝶那类图形约 35~45px 宽（半径 ~20px），3px 的 σ
        （极端 6px）仍稳在命中区内，不会为拟人牺牲通过率。
        """
        s = self.point_jitter_px
        if s <= 0:
            return 0.0, 0.0
        limit = 2.0 * s
        for _ in range(8):                       # 拒绝采样，避免偶发大偏移脱靶
            dx, dy = random.gauss(0.0, s), random.gauss(0.0, s)
            if abs(dx) <= limit and abs(dy) <= limit:
                return dx, dy
        return 0.0, 0.0

    def _get_solver(self) -> CaptchaSolver | None:
        """懒加载打码平台（只在真的要过码时才构造，避免无谓的配置告警）。"""
        if not self._solver_inited:
            self._solver_inited = True
            self._solver = CaptchaSolver.from_config(self.captcha_provider)
            if self._solver is not None and not self._solver.provider.supports_points:
                logger.warning(
                    f"⚠️ [天眼查] 当前打码平台 {self._solver.provider.name} 不支持**点选**类验证码"
                    f"（天眼查用的就是点选，需要云码 type=30009），将直接转人工等待"
                )
        return self._solver

    def _resolve_wait(self) -> float:
        """人工等待秒数：None → 取配置（默认 240）；<=0 表示不等待。"""
        v = self.captcha_wait
        if v is None:
            v = config.CAPTCHA_WAIT_SECONDS
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = config.CAPTCHA_WAIT_SECONDS
        return max(0.0, v)

    async def _captcha_signal(self, page) -> str:
        """返回风控线索（'' = 没有）。区分「URL 拦截页」和「页面里有个 captcha 容器」。

        ⚠️ 选择器分支**必须要求元素可见**：天眼查普通页面里也会挂着隐藏的
        captcha 容器，只判"存在"会误报，然后白等人工 240 秒。
        """
        try:
            url = (page.url or "").lower()
        except Exception:
            return ""
        for hint in self.CAPTCHA_URL_HINTS:
            if hint in url:
                return f"url:{hint}"
        for sel in self.CAPTCHA_SELECTORS:
            try:
                el = await page.query_selector(sel)
            except Exception:
                continue
            if not el:
                continue
            try:
                r = await el.bounding_box()
            except Exception:
                r = None
            if r and r.get("width", 0) >= 8 and r.get("height", 0) >= 8:
                return f"selector:{sel}"
        return ""

    async def _captcha_present(self, page) -> bool:
        """当前是否处于风控拦截状态。"""
        return bool(await self._captcha_signal(page))

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

    async def _handle_captcha_if_needed(self, page) -> bool:
        """命中风控时处置。返回 True 表示"可以继续抓取"，False 表示"这家没戏了"。

        处理顺序：自动打码（需云码）→ 人工等待 → 放弃。
        """
        signal = await self._captcha_signal(page)
        if not signal:
            return True

        print("\n" + "!" * 60)
        print(f"🚨 [天眼查风控触发] 检测到安全验证拦截！（线索: {signal}）")
        print("!" * 60)

        if self.captcha_mode == "off":
            print("      ⏭️ [天眼查] captcha_mode=off：跳过该企业、不等待")
            self.last_captcha_failed = True
            return False

        if self.captcha_mode == "auto" and self._get_solver() is not None:
            if await self._auto_solve_captcha(page):
                print("🤖 [天眼查] 自动过码成功，恢复抓取！\n")
                await self._human_rest(3.23, 6.29, desc="验证通过缓和停顿")
                return True
            print("      ⚠️ [天眼查] 自动过码未成功，转人工等待")

        ok = await self._manual_wait_captcha(page)
        self.last_captcha_failed = not ok
        return ok

    # ---- 人工等待（保留原有行为，仅改为可配置时长 + 返回值）---- #
    async def _manual_wait_captcha(self, page) -> bool:
        wait = self._resolve_wait()
        if wait <= 0:
            print("      ⏭️ [天眼查] 人工等待已关闭（等待 0 秒），跳过该企业")
            return False

        print("\n" + "!" * 60)
        print("👉 请切回已打开的 Chrome 浏览器窗口，【手动完成验证】。")
        print(f"⏳ 流水线已自动暂停等待（最多 {wait:.0f} 秒），验证通过后会自动恢复运行...")
        print("   （无人值守场景可用 --captcha-mode off 直接跳过，不空等）")
        print("!" * 60 + "\n")

        waited = 0.0
        while waited < wait:
            await asyncio.sleep(2)
            waited += 2
            if not await self._captcha_present(page):
                print(f"✅ [人工验证成功] 等待 {waited:.0f}s，恢复自动化抓取！\n")
                await self._human_rest(3.23, 6.29, desc="验证通过缓和停顿")
                return True
            if int(waited) % 20 == 0:
                print(f"      ⏳ [天眼查] 仍在等待人工验证...（已等 {waited:.0f}/{wait:.0f}s）")

        print(f"⚠️ 人工验证等待超时（{wait:.0f}s），跳过该商户天眼查查询。")
        return False

    # ------------------------------------------------------------------ #
    # 海关信息（海关注册编码 / 注册日期）
    # ------------------------------------------------------------------ #
    # 2026-09-17 只读取证到的真实结构（东莞市荣锝康电子科技有限公司 id=1630018885）：
    #   位置  : /company/<id>/jingzhuang（经营信息）→「进出口信用」区块
    #   列表表: table-wrap，表头 `序号|注册日期|注册海关|行业种类|经营类别|信用等级|操作`
    #           数据行 `1|2018-06-06|埔长安关|其他电子元件制造|进出口货物收发货人|注册登记和备案企业|详情`
    #   「详情」: `<span class="link-click">详情</span>` —— **href 为空，只能点击**
    #   弹窗  : `div.tyc-modal-root` →「进出口信用详情」→ 键值表 `table.index_tableBox__ZadJW`
    #           `['海关注册编码','4419960UVL','注册海关','埔长安关']`（每行 4 格 = k,v,k,v）
    CUSTOMS_URL = "https://www.tianyancha.com/company/{cid}/jingzhuang"

    # ⚠️ 数据行是**异步填充**的：表头会先渲染出来，此时表格里只有一个
    # `<tr><td colspan="7">暂未找到相关数据</td></tr>` 的占位行（实测 0.5s 时是空状态、
    # 1.5s 才有真数据）。所以定位「详情」**必须逐行找 `.link-click`**，
    # 不能取"最后一行的最后一个单元格" —— 那样会命中占位行、拿到 null。
    _FIND_CUSTOMS_DETAIL_JS = r"""
        () => {
            document.querySelectorAll('[data-wb-customs]')
                .forEach(e => e.removeAttribute('data-wb-customs'));
            for (const tb of document.querySelectorAll('table')) {
                const t = tb.innerText || '';
                // 要的是「列表表」：有"序号"表头。弹窗里的详情表没有"序号"。
                if (!/序号/.test(t) || !/注册海关/.test(t)) continue;
                for (const tr of tb.querySelectorAll('tr')) {
                    const btn = tr.querySelector('.link-click');
                    if (!btn || !/详情/.test(btn.innerText || '')) continue;
                    btn.setAttribute('data-wb-customs', '1');
                    const r = btn.getBoundingClientRect();
                    return { x: r.x, y: r.y, width: r.width, height: r.height,
                             text: (btn.innerText || '').trim() };
                }
            }
            return null;
        }
    """

    # 等待条件必须等到**数据行填充**，不能只等表头 ——
    # 表头先到、数据后到，只等表头会拿到空状态占位行（实测踩到）。
    _CUSTOMS_ROWS_READY_JS = (
        "() => {"
        "  for (const tb of document.querySelectorAll('table')) {"
        "    const t = tb.innerText || '';"
        "    if (!/序号/.test(t) || !/注册海关/.test(t)) continue;"
        "    for (const tr of tb.querySelectorAll('tr')) {"
        "      const b = tr.querySelector('.link-click');"
        "      if (b && /详情/.test(b.innerText || '')) return true;"
        "    }"
        "  }"
        "  return false;"
        "}"
    )

    _READ_CUSTOMS_MODAL_JS = r"""
        () => {
            let scope = null;
            for (const el of document.querySelectorAll('div')) {
                const cls = String(el.className || '');
                if (!cls.includes('tyc-modal')) continue;
                if (!/海关注册编码/.test(el.innerText || '')) continue;
                scope = el;
                break;
            }
            if (!scope) return null;
            const kv = {};
            for (const tb of scope.querySelectorAll('table')) {
                for (const tr of tb.querySelectorAll('tr')) {
                    const cells = [...tr.children]
                        .map(c => (c.innerText || '').replace(/\s+/g, ' ').trim());
                    // 每行 4 格 = k,v,k,v（也可能是 2 格）
                    for (let i = 0; i + 1 < cells.length; i += 2) {
                        const k = cells[i].replace(/[\s:：]/g, '');
                        if (k && !kv[k]) kv[k] = cells[i + 1];
                    }
                }
            }
            return kv;
        }
    """

    async def _fetch_customs(self, page, company_id: str) -> dict:
        """抓「海关注册编码 / 注册日期」。拿不到就返回空串 —— **绝不阻断主流程**。

        为什么单独一趟：这两个字段只在「经营信息」页的「进出口信用」里，
        而工商基本信息在 `/company/<id>`（基本信息 tab），是两个页面。
        """
        out = {"customs_code": "", "customs_reg_date": ""}
        if not company_id:
            return out
        try:
            await page.goto(self.CUSTOMS_URL.format(cid=company_id),
                            wait_until="domcontentloaded", timeout=25000)
            if not await self._handle_captcha_if_needed(page):
                return out

            # 等「进出口信用」的**数据行**就绪（不是表头）；等不到 = 这家没有进出口信用
            try:
                await page.wait_for_function(self._CUSTOMS_ROWS_READY_JS,
                                             timeout=12000, polling=100)
            except Exception:
                return out

            await asyncio.sleep(random.uniform(0.4, 0.9))
            box = await page.evaluate(self._FIND_CUSTOMS_DETAIL_JS)
            if not box:
                return out
            # 拟人点击「详情」（与验证码那套点击走同一条路径）
            await self._human_click_locator(page, page.locator("[data-wb-customs]").first)

            # 等弹窗里的「海关注册编码」出现
            try:
                await page.wait_for_function(
                    "() => [...document.querySelectorAll('div')].some(e =>"
                    "  String(e.className||'').includes('tyc-modal')"
                    "  && /海关注册编码/.test(e.innerText || ''))",
                    timeout=8000,
                    # polling 用数字而非默认的 raf：后台标签页的 rAF 会被浏览器暂停，
                    # 条件可能永远不触发（防御性设置，非本次问题的根因）
                    polling=100,
                )
            except Exception:
                return out

            await asyncio.sleep(random.uniform(0.3, 0.7))
            kv = await page.evaluate(self._READ_CUSTOMS_MODAL_JS) or {}
            out["customs_code"] = (kv.get("海关注册编码") or "").strip()
            out["customs_reg_date"] = (kv.get("注册日期") or "").strip()
            if out["customs_code"] or out["customs_reg_date"]:
                print(f"      🛃 [天眼查] 海关信息: 注册编码={out['customs_code'] or '—'} "
                      f"注册日期={out['customs_reg_date'] or '—'}")
        except Exception as e:
            print(f"      ⚠️ [天眼查] 海关信息提取异常（不影响其它字段）: {type(e).__name__}: {e}")
        return out

    # ---- 自动过码：两段式（先点按钮，再点选图形）---- #
    # 找出"包含提示文字 + 图片"的**最小**容器 = 最贴近验证码本体。
    # 取最小而非最大：外层祖先会一路包含到整个页面。同时给元素打标记，
    # 让 Python 侧能拿它做 bounding_box() 与 element.screenshot()。
    _FIND_MODAL_JS = r"""
        () => {
            const hint = /请在下图|依次点击|按顺序点击|请按顺序|点击下图|依次点选|按提示顺序/;
            document.querySelectorAll('[data-wb-captcha]').forEach(e => e.removeAttribute('data-wb-captcha'));
            const cands = [];
            for (const el of document.querySelectorAll('div, section, form, article')) {
                const t = el.innerText || '';
                if (!hint.test(t)) continue;
                if (!el.querySelector('img, canvas')) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 120 || r.height < 120) continue;
                if (r.width > window.innerWidth * 0.95 || r.height > window.innerHeight * 0.95) continue;
                cands.push({ el, area: r.width * r.height });
            }
            if (!cands.length) return null;
            cands.sort((a, b) => a.area - b.area);
            const el = cands[0].el;
            el.setAttribute('data-wb-captcha', '1');
            const r = el.getBoundingClientRect();
            // 提示条里的图标个数 = **需要点击的次数**。
            // 极验的提示是图标（↖ ↗ →）而不是文字，所以只能靠数图；拿它和打码平台返回的
            // 点数对比，就能识别"漏点"（实测：提示 3 个、平台只返回 2 个 → 必失败）。
            // 图标来自 static.geetest.com/.../icon_material/，按 src 关键字数最稳；
            // 数不到就退回"提示条容器内的 img 数"，再不行就 0（0 = 不做校验，只记录）。
            const bySrc = [...el.querySelectorAll('img')]
                .filter(i => /icon_material/i.test(i.getAttribute('src') || '')).length;
            const tips = el.querySelector('[class*="geetest_tips"]');
            const hintIcons = bySrc || (tips ? tips.querySelectorAll('img').length : 0);
            return { x: r.x, y: r.y, width: r.width, height: r.height,
                     dpr: window.devicePixelRatio || 1, hint_icons: hintIcons };
        }
    """

    @staticmethod
    def _png_size(png: bytes) -> tuple[int, int] | None:
        """从 PNG 字节里读 IHDR 的宽高。

        为什么不引入 Pillow：venv 里没有 PIL（9-16 做方图裁剪时就是因此在浏览器 canvas 里做的）。
        截图坐标 → 页面坐标的换算**必须**用真实像素尺寸，不能假设 devicePixelRatio
        （Playwright 的截图按设备像素出图，而鼠标点击用的是 CSS 像素 —— 差一个 dpr 就会点偏）。
        """
        if not png or len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        try:
            w = int.from_bytes(png[16:20], "big")
            h = int.from_bytes(png[20:24], "big")
        except Exception:
            return None
        return (w, h) if w > 0 and h > 0 else None

    # 定位「点击按钮开始验证」本体。
    # **不能用 `div:has-text(...)` + `.first`** —— `:has-text` 匹配所有**包含**该文案的元素，
    # 而 `.first` 取文档顺序最靠前的那个，也就是最外层的祖先容器。实测：
    #     DIV.container                    rect=[0, 86, 929, 407]   ← .first 命中的就是这个
    #     ...
    #     DIV.geetest_tips_wrap            rect=[411, 288, 112, 22] ← 真按钮
    # 于是"随机落点"必然落在容器空白处，按钮根本没被点到，第二段弹窗自然不出现。
    # （旧实现用 `locator.click()` 点容器几何中心，恰好落在按钮区域内 —— 那是**运气**，
    #   一改成拟人落点就暴露了。）
    # 正确做法：要求 innerText **精确等于**文案，并在合格者里取**面积最小**的。
    _FIND_START_BTN_JS = r"""
        (texts) => {
            const want = new Set(texts);
            document.querySelectorAll('[data-wb-startbtn]').forEach(e => e.removeAttribute('data-wb-startbtn'));

            // 递归收集元素，**并穿透 shadow DOM**：极验部分版本把控件放进 shadow root，
            // 而 `document.querySelectorAll` 看不见 shadow root 内部 —— 表现是
            // "选择器什么都找不到"，但页面上肉眼可见按钮。
            const all = [];
            const walk = (root) => {
                all.push(...root.querySelectorAll('button, a, div, span, p'));
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) { all.push(...el.shadowRoot.querySelectorAll('button, a, div, span, p')); walk(el.shadowRoot); }
                }
            };
            walk(document);

            const hits = [];
            for (const el of all) {
                // innerText 对隐藏元素返回空串，退回 textContent 兜底
                const t = ((el.innerText || el.textContent || '') + '').trim();
                if (!t || t.length > 40) continue;
                const exact = want.has(t);
                if (!exact && ![...want].some(w => t.includes(w))) continue;
                const r = el.getBoundingClientRect();
                hits.push({ el, r, area: r.width * r.height, t, exact });
            }
            const pick = (pass) => {
                let best = null;
                for (const c of hits) {
                    if (!pass(c)) continue;
                    if (!best || c.area < best.area) best = c;
                }
                return best;
            };
            const fits = c => c.r.width >= 30 && c.r.height >= 14
                && c.r.width <= window.innerWidth * 0.5 && c.r.height <= window.innerHeight * 0.5;
            // 三级降级：精确+尺寸 → 精确 → 包含文案+尺寸。
            // 不再有"无条件兜底"那一级：宁可返回 not found 让上层报错并取证，
            // 也不要瞎点一个祖先容器（点空后第二段弹窗不出现，排查代价远高于报错）。
            const best = pick(c => c.exact && fits(c)) || pick(c => c.exact) || pick(c => fits(c));

            // 诊断信息随返回值带回 Python：日志里能直接看出"是没渲染、还是结构变了"
            const diag = hits.slice(0, 6).map(c => ({
                tag: c.el.tagName,
                cls: String(c.el.className || '').slice(0, 60),
                text: c.t.slice(0, 30),
                exact: c.exact,
                rect: [Math.round(c.r.x), Math.round(c.r.y), Math.round(c.r.width), Math.round(c.r.height)],
            }));
            if (!best) return { found: false, diag: diag, scanned: all.length };

            best.el.setAttribute('data-wb-startbtn', '1');
            const r = best.el.getBoundingClientRect();
            return {
                found: true, diag: diag, scanned: all.length,
                x: r.x, y: r.y, width: r.width, height: r.height,
                text: best.t, tag: best.el.tagName,
                cls: String(best.el.className || '').slice(0, 80),
            };
        }
    """

    # 判断极验当前处于哪一段（用于"等渲染"和"判断类型"）
    _GEETEST_STAGE_JS = r"""
        (texts) => {
            const body = document.body ? (document.body.innerText || '') : '';
            if (/请在下图|依次点击|按顺序点击|请按顺序|点击下图|依次点选|按提示顺序/.test(body)) return 'point';
            const want = new Set(texts);
            const all = [];
            const walk = (root) => {
                all.push(...root.querySelectorAll('button, a, div, span, p'));
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) { all.push(...el.shadowRoot.querySelectorAll('button, a, div, span, p')); walk(el.shadowRoot); }
                }
            };
            walk(document);
            for (const el of all) {
                const t = ((el.innerText || el.textContent || '') + '').trim();
                if (!t || t.length > 40) continue;
                if ([...want].some(w => t.includes(w))) {
                    const r = el.getBoundingClientRect();
                    if (r.width >= 8 && r.height >= 8) return 'button';
                }
            }
            // 滑块形态（天眼查也会出）：先识别出来明确报"类型不支持"，而不是含混地超时
            if (document.querySelector('[class*="geetest_slider"], [class*="slider_button"], .nc_wrapper')) return 'slider';
            return '';
        }
    """

    # 失败取证：把页面上所有验证码相关的元素捞出来（含 iframe），用于事后照真实结构改代码
    _GEETEST_SCAN_JS = r"""
        () => {
            const out = {
                url: location.href, title: document.title,
                visibility: document.visibilityState,
                body_head: (document.body ? (document.body.innerText || '') : '').replace(/\s+/g, ' ').slice(0, 400),
                elements: [], frames: [],
            };
            const sel = '[class*="geetest"], [class*="captcha"], [class*="sec-"], [class*="verify"], iframe';
            for (const el of document.querySelectorAll(sel)) {
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) continue;
                out.elements.push({
                    tag: el.tagName, cls: String(el.className || '').slice(0, 90), id: el.id || '',
                    rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                    text: ((el.innerText || el.textContent || '') + '').replace(/\s+/g, ' ').trim().slice(0, 70),
                });
            }
            out.elements = out.elements.slice(0, 40);
            out.frames = [...document.querySelectorAll('iframe')].map(f => f.src || '(no src)').slice(0, 10);
            return out;
        }
    """

    async def _wait_start_button(self, page, timeout: float = 12.0) -> dict:
        """轮询等「开始验证」按钮渲染出来。

        ⚠️ **必须轮询，不能只查一次**。`_captcha_present()` 只要 URL 里含 `/sorry/captcha`
        就立刻返回 True，而那一刻极验的 JS 往往还没跑完 —— 按钮尚不存在。
        原实现只 `evaluate` 一次就返回，于是**必然拿不到按钮**，而且失败时**不打任何日志**：
        上层只看到"没等到点选弹窗"，把"按钮没点上"误判成"不是点选类型"（实测踩到）。
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                box = await page.evaluate(self._FIND_START_BTN_JS, list(self.START_BTN_TEXTS))
            except Exception as e:
                box = {"found": False, "diag": [], "scanned": 0, "err": f"{type(e).__name__}: {e}"}
            if box and box.get("found"):
                return box
            if time.monotonic() >= deadline:
                return box or {"found": False, "diag": [], "scanned": 0}
            await asyncio.sleep(0.4)

    async def _wait_geetest_stage(self, page, timeout: float = 12.0) -> str:
        """等极验渲染，返回 'point' 点选 / 'button' 待点按钮 / 'slider' 滑块 / '' 等不到。"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                stage = await page.evaluate(self._GEETEST_STAGE_JS, list(self.START_BTN_TEXTS))
            except Exception:
                stage = ""
            if stage:
                return stage
            if time.monotonic() >= deadline:
                return ""
            await asyncio.sleep(0.4)

    async def _dump_captcha_forensics(self, page, reason: str,
                                      extra_png: bytes | None = None,
                                      extra_name: str = "captcha.png") -> str | None:
        """自动过码失败时**保留现场**：整页截图 + 验证码相关 DOM 摘要。

        验证码不是想触发就能触发的（实测分别在第 7 轮、第 2 轮才中），
        靠"等下次复现再调选择器"效率极低。现场落盘后，事后照真实结构改代码即可。

        `extra_png` 会额外落一份**裁好的验证码图**（也就是喂给打码平台的那张）——
        失败时看这张图就能判断"是打码员漏点了，还是提示本来就只有 2 个"。
        """
        try:
            import datetime
            import json
            import os

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            d = os.path.join("logs", f"tyc_captcha_fail_{ts}")
            os.makedirs(d, exist_ok=True)
            try:
                await page.screenshot(path=os.path.join(d, "page.png"))
            except Exception:
                pass
            if extra_png:
                try:
                    with open(os.path.join(d, extra_name), "wb") as f:
                        f.write(extra_png)
                except Exception:
                    pass
            try:
                info = await page.evaluate(self._GEETEST_SCAN_JS)
            except Exception as e:
                info = {"err": f"{type(e).__name__}: {e}"}
            info["reason"] = reason

            # 逐 frame 也扫一遍：极验部分版本把控件放进 iframe，主文档的
            # querySelectorAll 看不见它 —— 表现同样是"什么都找不到"。
            # 不做点击（点击跨 frame 要补偏移量），只用于判断"是不是 iframe 形态"。
            try:
                frames = []
                for fr in page.frames:
                    entry = {"url": (fr.url or "")[:160], "main": fr == page.main_frame}
                    try:
                        sub = await fr.evaluate(self._GEETEST_SCAN_JS) or {}
                        entry["body_head"] = sub.get("body_head", "")
                        entry["elements"] = (sub.get("elements") or [])[:12]
                    except Exception as e:
                        entry["err"] = f"{type(e).__name__}: {str(e)[:80]}"
                    frames.append(entry)
                info["frames_detail"] = frames
            except Exception:
                pass

            with open(os.path.join(d, "forensics.json"), "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
            print(f"      📸 [天眼查] 已保留验证码现场: {d}\\page.png + forensics.json")
            return d
        except Exception as e:
            print(f"      ⚠️ [天眼查] 现场取证失败: {type(e).__name__}: {e}")
            return None

    async def _click_start_button(self, page) -> bool:
        """第一段：点「点击按钮开始验证」。按文案匹配而非 class（站点改版也不会全废）。

        点击走 `_human_click_locator`：带曲线轨迹、落点在按钮内随机、有按压时长。
        （原来是 `locator.click()` —— 鼠标瞬移到几何中心、零按压，属自动化指纹。）
        """
        box = await self._wait_start_button(page, timeout=self.START_BTN_WAIT)
        if not box.get("found"):
            print(f"      ⚠️ [天眼查] 未找到「开始验证」按钮"
                  f"（扫描 {box.get('scanned', 0)} 个元素，含 shadow DOM）")
            for d in (box.get("diag") or [])[:5]:
                print(f"         · {d['tag']}.{d['cls'][:44]} rect={d['rect']} text={d['text']!r}")
            if box.get("err"):
                print(f"         · 异常: {box['err']}")
            return False

        print(f"      🎯 [天眼查] 定位到开始按钮: {box.get('tag', '?')}.{str(box.get('cls', ''))[:40]} "
              f"{box.get('width', 0):.0f}×{box.get('height', 0):.0f} 「{box.get('text', '')}」")
        loc = page.locator("[data-wb-startbtn]").first
        if await self._human_click_locator(page, loc):
            print(f"      🖱️ [天眼查] 已拟人点击「{box['text']}」")
            return True
        return False

    async def _auto_solve_captcha(self, page) -> bool:
        """自动过天眼查的两段式点选验证码。成功返回 True。

        流程：点开始按钮 → 等点选弹窗 → 截「含提示」的弹窗图 → 云码返回坐标
              → 按顺序点击 → 点确定 → 验证是否放行。

        —— 2026-09-17 真实验证通过（一次成功）——
        ```
        拦截 URL: tianyancha.com/sorry/captcha?from=%2Fcompany%2F2317992732&token=...
        弹窗容器: DIV.geetest_box_98340385 geetest_box geetest_flat  rect=[295,203,340,385]
        截图 341×385px → 页面 340×385px（比例 1.00, dpr=1）
        云码返回 3 个坐标，全部落在蝴蝶图形上 → 按序点击 → 点确定
        结果: ✅ 通过，且**自动回跳**到 from 参数指定的原目标
              https://www.tianyancha.com/company/2317992732
        ```
        几个实测结论：
        · 天眼查用的是**极验（Geetest）**，素材来自 `static.geetest.com/static_resources/icon_material/`。
          极验的点选是该平台最常见的形态，人工打码对它的识别质量不错。
        · 验证码的提示是**箭头图标**（如 ↙ ↘ ↑），语义是"按箭头方向点击朝向相符的图形"。
          原本担心打码员理解不了这种非文字提示 —— **实测能正确理解**，3 个坐标全中。
        · 通过后**自动回跳**到 `from` 指定的原页面，所以调用方不需要重新 goto，
          后续 `wait_for_selector('table...')` 可以直接等工商表格加载。
        · 截图尺寸与容器尺寸可能差 1px（341 vs 340），所以坐标换算必须用
          **真实图片宽高**（`_png_size`），不能假设等于容器尺寸。

        —— 2026-09-17 线上失败过一次，记下来避免重踩 ——
        现象：日志只打「没等到点选弹窗（可能本项目下不是点选类型）」，且**没有**
              「定位到开始按钮」那一行。
        根因：`_captcha_present()` 只要 URL 含 `/sorry/captcha` 就立刻为真，而那一刻
              极验的 JS 往往还没执行完 —— 按钮还不存在。`_click_start_button` 只
              `evaluate` 一次，拿不到就**静默返回 False**，返回值又被上层丢弃，
              于是真实原因（按钮没点上）被那句日志伪装成"不是点选类型"，带偏排查方向。
        已修：① 轮询等渲染（`_wait_start_button` / `_wait_geetest_stage`）；
              ② 不再丢弃按钮点击结果；
              ③ 交互前 `bring_to_front()`（后台标签页里极验可能不渲染控件）；
              ④ 失败时把现场落盘（截图 + DOM 摘要 + 逐 frame 扫描）。
        """
        solver = self._get_solver()
        if solver is None:
            return False

        try:
            # ⓪ 极验在**后台标签页**里可能不渲染控件（它读 document.visibilityState）。
            #    流水线本就是无人值守运行，把页面提到前台是安全且必要的。
            try:
                await page.bring_to_front()
            except Exception:
                pass

            # ① 等极验渲染出来，判断当前处于哪一段。
            #    **不能"查一次就决定"**：URL 命中 /sorry/captcha 时 JS 常常还没跑完，
            #    那一刻按钮、提示文案都还不存在（实测踩到的正是这个时序）。
            stage = await self._wait_geetest_stage(page, timeout=self.STAGE_WAIT_FIRST)

            if stage == "button":
                if not await self._click_start_button(page):
                    await self._dump_captcha_forensics(page, "start_button_not_found")
                    return False
                stage = await self._wait_geetest_stage(page, timeout=self.STAGE_WAIT_SECOND)   # 等第二段弹窗

            if stage != "point":
                why = {
                    "button": "按钮点了但第二段弹窗没出现",
                    "slider": "滑块类型（本项目只接了点选，未接滑块）",
                    "": "极验控件始终没渲染出来",
                }.get(stage, stage)
                print(f"      ⚠️ [天眼查] 点选弹窗未就绪 —— {why}，转人工")
                await self._dump_captcha_forensics(page, f"no_point_modal_{stage or 'unknown'}")
                return False

            await asyncio.sleep(random.uniform(0.6, 1.2))   # 等图形区渲染完

            # ③~⑦ 多轮尝试。**每轮都重新截图**：验证失败后极验会换一张新图和一组
            # 新提示，拿旧图重算等于把同一个错误再做一遍。
            # （原先只有一轮，失败即转人工 —— 实测踩到：提示 3 个图标、云码只返回 2 个点，
            #   当轮必失败，而换图重来一次就过了。）
            last_info: dict = {}
            for rnd in range(1, self.CAPTCHA_MAX_ROUNDS + 1):
                # 上一轮可能其实已放行，只是判定慢半拍
                if not await self._captcha_present(page):
                    return True
                ok, info = await self._solve_point_round(page, solver, rnd, self.CAPTCHA_MAX_ROUNDS)
                if ok:
                    return True
                last_info = info
                if rnd < self.CAPTCHA_MAX_ROUNDS:
                    print(f"      ↻ [天眼查] 第 {rnd} 轮未通过"
                          f"（{info.get('reason') or '未放行'}），换新图重试")
                    await self._reset_point_modal(page)
                    await self._human_rest(1.2, 2.4, desc="换新图前停顿")

            await self._dump_captcha_forensics(
                page, f"point_failed_{last_info.get('reason') or 'unknown'}",
                extra_png=last_info.get("png"), extra_name="last_round_captcha.png")
            print(f"      ⚠️ [天眼查] {self.CAPTCHA_MAX_ROUNDS} 轮均未通过"
                  f"（最后一轮：提示条 {last_info.get('hint_icons') or '?'} 个图标 / "
                  f"平台返回 {last_info.get('points') or 0} 个点；现场已落盘）")
            return False

        except CaptchaProviderError as e:
            print(f"      ⚠️ [天眼查] 打码平台错误: {e}")
            return False
        except Exception as e:
            print(f"      ⚠️ [天眼查] 自动过码异常: {type(e).__name__}: {e}")
            return False

    async def _solve_point_round(self, page, solver, rnd: int, total: int) -> tuple[bool, dict]:
        """跑一轮：定位弹窗 → 截图 → 打码 → 依次点击 → 点确定 → 等放行。

        返回 `(是否放行, 本轮信息)`。信息里带 `hint_icons`（提示条图标数 = **应点次数**）
        与 `points`（平台返回点数），用来识别"漏点" —— 两者不等时基本必失败，
        这种情况**直接换新图**，不浪费一次浏览器点击。
        """
        info: dict = {"round": rnd, "hint_icons": 0, "points": 0, "reason": "", "png": None}

        # ③ 定位弹窗（含提示栏 + 图片区）并截图
        box = await page.evaluate(self._FIND_MODAL_JS)
        if not box:
            info["reason"] = "no_modal"
            print(f"      ⚠️ [天眼查] 第 {rnd}/{total} 轮：未能定位验证码弹窗")
            return False, info
        info["hint_icons"] = int(box.get("hint_icons") or 0)

        el = page.locator("[data-wb-captcha]").first
        png = await el.screenshot()
        info["png"] = png
        size = self._png_size(png)
        if not size:
            info["reason"] = "bad_screenshot"
            print(f"      ⚠️ [天眼查] 第 {rnd}/{total} 轮：截图异常（非 PNG）")
            return False, info
        img_w, img_h = size
        # 图片像素 → CSS 像素的换算比例（用真实图片宽高，不假设 dpr）
        sx = box["width"] / float(img_w)
        sy = box["height"] / float(img_h)
        print(f"      🖼️ [天眼查] 第 {rnd}/{total} 轮截图 {img_w}×{img_h}px → 页面 "
              f"{box['width']:.0f}×{box['height']:.0f}px（比例 {sx:.2f}）")

        # ④ 交给打码平台
        points = await solver.solve_points(png)
        if not points:
            info["reason"] = "no_points"
            return False, info
        info["points"] = len(points)
        print(f"      🤖 [天眼查] 平台返回 {len(points)} 个坐标: "
              f"{', '.join(f'({x:.0f},{y:.0f})' for x, y in points)}")
        # 漏点检测（实测踩到的正是这条）：提示条 3 个图标、平台只给 2 个点 → 必失败
        if info["hint_icons"] and len(points) != info["hint_icons"]:
            info["reason"] = f"count_mismatch({info['hint_icons']}vs{len(points)})"
            print(f"      ⚠️ [天眼查] **点数不符**：提示条 {info['hint_icons']} 个图标，"
                  f"平台只返回 {len(points)} 个 → 判为漏识别，直接换新图")
            return False, info

        # ⑤ 按顺序依次点击。
        # 每一击都：曲线轨迹移动 → 到位小停顿 → 按下 → **停留** → 抬起；
        # 落点 = 平台给的图形中心 + 二维正态偏移（见 _jitter）。
        for i, (px, py) in enumerate(points, 1):
            jx, jy = self._jitter()
            cx = box["x"] + px * sx + jx
            cy = box["y"] + py * sy + jy
            await self._human_click(page, cx, cy)
            # 点完一个到点下一个之间：真人要先看一眼再移过去，别太急
            await asyncio.sleep(random.uniform(0.45, 1.30))
            print(f"         ✔ 第 {i} 次点击 ({cx:.0f}, {cy:.0f})"
                  f"{f'  偏移({jx:+.1f},{jy:+.1f})' if self.point_jitter_px > 0 else ''}")

        # ⑥ 点确定（先在本弹窗内找，再退回整页找）—— 同样走拟人点击
        # 点确定前先生理性地停顿一下（真人点完图形会看一眼再确认）
        await asyncio.sleep(random.uniform(0.35, 0.90))
        clicked = False
        for sel in ("button:has-text('确定')", "text=确定", "button:has-text('确认')"):
            try:
                loc = page.locator("[data-wb-captcha]").locator(sel).first
                if await loc.count():
                    clicked = await self._human_click_locator(page, loc)
                    break
            except Exception:
                continue
        if not clicked:
            for sel in ("button:has-text('确定')", "text=确定"):
                try:
                    loc = page.locator(sel).first
                    if await loc.count():
                        clicked = await self._human_click_locator(page, loc)
                        break
                except Exception:
                    continue
        if not clicked:
            info["reason"] = "no_confirm_btn"
            print("      ⚠️ [天眼查] 没找到「确定」按钮")
            return False, info

        # ⑦ 等放行。**短等**：失败时极验 1~2s 就出结果，等太久会拖慢换新图的节奏
        # （原先写死 15s，一轮白等十几秒）。
        waited = 0.0
        while waited < self.ROUND_RELEASE_WAIT:
            await asyncio.sleep(0.5)
            waited += 0.5
            if not await self._captcha_present(page):
                return True, info
        info["reason"] = "not_released"
        print(f"      ⚠️ [天眼查] 第 {rnd}/{total} 轮点击后 {waited:.0f}s 未放行")
        return False, info

    async def _reset_point_modal(self, page) -> bool:
        """换新图（best effort）。极验失败后通常会自己刷新，但有时要手动点刷新按钮。

        返回是否点到了刷新按钮；没点到也不算失败 —— 调用方随后还会等一段。
        """
        for sel in ('[class*="geetest_refresh"]', '[class*="geetest_reset"]',
                    '[class*="geetest_reload"]', 'button:has-text("刷新")',
                    'button:has-text("换一张")', '[aria-label*="刷新"]'):
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    if await self._human_click_locator(page, loc):
                        print(f"      🔄 [天眼查] 已点刷新换新图（{sel}）")
                        return True
            except Exception:
                continue
        return False

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

    # 天眼查不叫「经营状态」而叫「登记状态」（存续/在业/注销/吊销…），
    # 落表时统一映射到「经营状态」列，与爱企查的 status 对齐。
    STATUS_LABELS = ("登记状态", "经营状态", "企业状态")

    def _clean_status(self, raw_val: str) -> str:
        """登记状态提纯。

        天眼查的值常带噪声：`存续（在营、开业、在册）`、`存续 2025年报`、
        或把相邻徽标文字粘进来。这里只保留第一个状态词。
        """
        v = (raw_val or "").strip()
        if not v:
            return ""
        v = re.sub(r'[\s\n\r\t]+', ' ', v).strip()
        # 去掉尾部年报/年份噪声
        v = re.sub(r'\s*20\d{2}\s*年报.*$', '', v).strip()
        # 取括号前的主状态词，如 `存续（在营、开业、在册）` → `存续`
        m = re.match(r'^([\u4e00-\u9fa5]{2,6})', v)
        if m:
            return m.group(1)
        return ""

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

    async def _recover_card_after_captcha(self, page, search_kw: str):
        """结果页没企业卡片时，先怀疑「验证浮层异步渲染，刚才那一次查漏了」。

        为什么需要：天眼查的验证浮层不是导航过去的，而是在原页面上**异步弹出**的。
        实测（2026-09-17 14:33）：同一次导航里，`goto(domcontentloaded)` 返回后立刻查
        `_captcha_signal()` 是空的，等 1.5s 后才出现 `.geetest_holder`。
        流水线在 goto 后立刻调用 `_handle_captcha_if_needed()`，所以会**漏判**，
        随后"无卡片"分支把这家标成 blocked 跳过 —— 不误删，但白跑一家。

        返回：重取到的卡片元素（过码成功时）或 None。
        """
        try:
            if not await self._captcha_signal(page):
                return None
        except Exception:
            return None
        print(f"      🔐 [天眼查] 结果页出现验证浮层（goto 后那一查漏判了），"
              f"过码后重试取卡片: {search_kw}")
        try:
            if not await self._handle_captcha_if_needed(page):
                return None
        except Exception:
            return None
        await self._human_rest(1.5, 2.8, desc="过码后重试卡片")
        try:
            return await page.query_selector(self.CARD_SELECTOR)
        except Exception:
            return None

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
            "insured_count": "未公开",
            "business_status": "",     # 天眼查称「登记状态」，落表映射到「经营状态」列
            "customs_code": "",        # 海关注册编码（进出口信用，需点「详情」弹窗）
            "customs_reg_date": "",    # 海关注册日期
            # 诊断字段：上游 `_classify_result()` 靠 blocked 区分「被风控拦住」与「真的查无此企业」
            "blocked": False,
            "last_error": "",
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
            if not await self._handle_captcha_if_needed(page):
                # 必须显式标记 blocked：否则空 info 会被上游 `_classify_result()`
                # 判成 not_found（"站内查无此企业"），而今天加的
                # 「工商字段全空则不入库」会据此**删掉本来只是被风控拦住的正常商户**。
                info["blocked"] = True
                info["last_error"] = "captcha_not_passed"
                return info
            await page.mouse.wheel(0, random.randint(200, 450))
            await self._human_rest(2.0, 3.5, desc="卡片初筛")

            card_el = await page.query_selector(self.CARD_SELECTOR)
            if not card_el:
                # ① 先怀疑「验证浮层异步渲染，goto 后那一查漏判了」→ 过码后重取一次。
                #    这条只在页面"看起来坏了"时才走，正常路径零开销。
                card_el = await self._recover_card_after_captcha(page, search_kw)
            if not card_el:
                # ⚠️ 必须区分两种"没卡片"，否则会**误删正常商户**：
                #   ① 页面明确说「未找到/暂无数据」→ 真的站内查无此企业 → company_not_found（可删）
                #   ② 既没卡片也没提示            → 页面压根没渲染出来（多为被风控拦）→ blocked（不可删）
                # 这条口径与爱企查 9-16 的做法完全一致（当时的结论：no_result_cards 属于
                # 「环境异常」，若算成良性则熔断永不触发，会一直空转烧请求）。
                body_text = ""
                try:
                    body_text = await page.evaluate("document.body.innerText || ''")
                except Exception:
                    pass
                if re.search(r'未找到|没有找到|暂无数据|无相关结果|没有相关|暂无结果', body_text):
                    print(f"      ℹ️ [天眼查] 站内查无此企业: {search_kw}")
                    info["last_error"] = "company_not_found"
                else:
                    print(f"      ⚠️ [天眼查] 结果页既无企业卡片也无「未找到」提示，疑似未渲染/被拦: {search_kw}")
                    info["blocked"] = True
                    info["last_error"] = "no_result_cards"
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
                    if not await self._handle_captcha_if_needed(page):
                        # 同上：详情页被拦时也要标记 blocked，别让空结果被当成"查无此企业"
                        info["blocked"] = True
                        info["last_error"] = "captcha_not_passed"
                        return info

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
                                            } else if ((label.includes('登记状态') || label.includes('经营状态') || label.includes('企业状态')) && !result.status) {
                                                result.status = valCell.innerText ? valCell.innerText.trim() : '';
                                            }
                                        }
                                    }
                                }
                            }
                            // 兜底：登记状态在部分版本渲染成页头标签而非表格行，
                            // 表格里拿不到时退化为全文正则（容忍"登记状态：存续"这类写法）。
                            if (!result.status) {
                                const bodyText = document.body.innerText || '';
                                const text = bodyText.replace(/\\s+/g, ' ');
                                const m = text.match(/(?:登记状态|经营状态|企业状态)\\s*[:：]?\\s*([\\u4e00-\\u9fa5]{2,6})/);
                                if (m) result.status = m[1];
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

                    if table_data.get("status"):
                        info["business_status"] = self._clean_status(table_data["status"])

                    if not collected_phones:
                        detail_text = await page.evaluate("document.body.innerText")
                        for dp in re.findall(r'(?:电话|联系方式)\s*[:：]?\s*([+\d\s\-]+)', detail_text):
                            if "暂无" not in dp and "登录" not in dp and len(re.sub(r'\D', '', dp)) >= 7:
                                collected_phones.append(dp.strip())

                    # 海关信息（海关注册编码 / 注册日期）：在另一个页面，且要点「详情」弹窗。
                    # 放最后：它最可能失败，失败也不该影响已经读到的工商字段。
                    cid_m = re.search(r"/company/(\d+)", href or "")
                    if cid_m:
                        info.update(await self._fetch_customs(page, cid_m.group(1)))

            deduped = deduplicate_phone_list(collected_phones)
            if deduped:
                info["phone"] = " / ".join(deduped[:2])

            print(
                f"      ✅ [天眼查成功] 公司: {info['registered_company'] or '未查到'} | "
                f"注册资本: {info['registered_capital']} | "
                f"实缴资本: {info['paid_in_capital']} | "
                f"参保人数: {info['insured_count']} | "
                f"登记状态: {info['business_status'] or '未取到'}"
            )

        except Exception as e:
            print(f"      ⚠️ [天眼查检索异常] {search_kw}: {e}")
            # 异常（导航超时 / 渲染报错 / 选择器失效）属于"没查成"，不是"查不到" ——
            # 标记出来，免得空结果被上游当成「站内查无此企业」而删掉正常商户。
            info["blocked"] = True
            info["last_error"] = f"exception:{type(e).__name__}"

        # 单次检索冷却严格控制在 3.23 ~ 6.29 秒
        await self._human_rest(3.23, 6.29, desc="单次检索冷却")
        return info