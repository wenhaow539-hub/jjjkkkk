import asyncio
import base64
import random
import re
import time
from urllib.parse import quote_plus

from playwright.async_api import Locator, Page

import config
from enrichers.captcha_solver import CaptchaProviderError, CaptchaSolver
from utils.logger import get_logger

logger = get_logger("enricher.aiqicha")


class AiQiChaEnricher:
    """爱企查工商与触点数据增强器（Playwright CDP 渲染）。

    对外契约：`await search_and_enrich(page, company_name) -> dict`
    只使用调用方传入的 `page`，自己不开浏览器、不建 client（由调度器/Crawlee 负责）。

    ── 三条实测经验（2026-09 在真实站点验证，改动前请先读）────────────────────
    1. 反调试拦截是**间歇性**的：命中时显示“请关闭浏览器的调试窗口再访问页面！”再跳 about:blank。
       交错顺序重复实测 8 次，连"完全不伪装"也 8/8 通过 —— **无法把绕过归因于任何单一手段**
       （早先"console 拦截有效"的结论是测试顺序/节流造成的假象）。
       因此 ANTI_DEBUG_SCRIPT 只是低成本兜底；可靠做法是**能识别拦截并熔断**（见 _detect_antibot_page）。
       它还有两条硬要求：不得触碰页面对象、改写要像原生 —— 详见该常量的注释。
    2. 结果卡片是 `<a class="card">` 但**没有 href**，点击由 JS 处理；
       企业 ID 在 `data-log-title="item-<pid>"`，详情页可直接访问
       `https://aiqicha.baidu.com/company_detail_<pid>`。
    3. 搜索结果需要 6~10 秒才渲染出卡片，过早读取只会拿到空壳页面。
    4. 旋转验证码：**拖动的加密参数不需要我们逆向** —— 因为拖动用的是真实
       鼠标事件（CDP Input），页面自己的 JS 会采集轨迹、算出 `fs` 等加密值。
       我们要做的只有三件：取到图片 → 问打码平台"转正多少度" → 按
       `角度/360 × 轨道宽度` 拖到位。见 `_auto_solve_captcha`。
       未配置打码平台时自动退化为人工等待，不影响原有行为。
    5. 取验证码图片**优先用 img 的 src 直连下载**，而不是元素截图：
       元素截图可能带上防自动化水印（马赛克），会让角度识别全错。
       截图仅作最后兜底。
    ────────────────────────────────────────────────────────────────────────
    """

    SEARCH_URL = "https://aiqicha.baidu.com/s?q={kw}"
    DETAIL_URL = "https://aiqicha.baidu.com/company_detail_{pid}"
    CARD_SELECTOR = ".company-list .card"

    # ── 验证码"暂停等待"口径（对齐天眼查 _handle_captcha_if_needed）──────────
    #   天眼查：醒目框线提示 + `for _ in range(120): sleep(2)`（≈240s）+ 通过后缓和停顿
    #   爱企查原来只等 60s、提示也很弱，人对不上就会超时并被计成"环境异常"喂给熔断。
    CAPTCHA_WAIT_TIMEOUT = 240.0          # 人工等待上限（秒）；_resolve_wait 会用 config 覆盖
    # 验证通过后的缓和停顿。天眼查同款是 (3.23, 6.29)，但爱企查实测：过码后只缓 3~6 秒
    # 就紧接着翻结果/进详情，"过了一个码立刻又弹一个"——刚过完码的会话在风控眼里仍是
    # 高危会话，必须把第一段缓和拉长。配合 CAPTCHA_COOLDOWN_AFTER_PASS 一起起作用。
    CAPTCHA_REST_AFTER_PASS = (6.0, 10.0)
    # 过码通过后的**冷却窗口**（秒）：从通过那一刻起算。在窗口内发起的真实导航
    # （打开搜索页 / 进入详情页）都会先补足剩余冷却——这是防"连续弹码"的关键，
    # 因为弹一次码的代价（取图+识别+拖动，甚至转人工 240s）远大于多等几秒。
    CAPTCHA_COOLDOWN_AFTER_PASS = (9.0, 15.0)
    CAPTCHA_WAIT_HINT_EVERY = 20.0        # 每隔多少秒打印一次等待进度

    # ── 百度安全验证（旋转验证码）选择器 ────────────────────────────────
    # 多来源交叉验证：新版用 `passMod_spin-*` 系列类名，旧版用 `vcode-spin-*` 系列 id，
    # 所以两类都列上。全部按"可见元素"找，避免命中隐藏的模板节点。
    CAPTCHA_IMG_SELECTORS = (
        "img.passMod_spin-background",
        "img[class*='passMod_spin']",
        "img[id*='vcode-spin-img']",
        "img[class*='spin-img']",
    )
    CAPTCHA_SLIDER_SELECTORS = (
        ".passMod_slide-btn",
        "[id*='vcode-spin-button']",
        "[class*='slide-btn']",
        "[class*='spin-button']",
    )
    # 轨道容器：用来量「角度 → 像素」的换算基准（可用 CAPTCHA_TRACK_PX 覆盖）
    # ⚠️ 实测（2026-09，tuxing_v2）：真实轨道是 `.passMod_slide-control`（290x53），
    #    原来写的 `.passMod_slide-bar` 在这个版本**根本不存在**（count=0）→ 量不到行程 →
    #    退化成写死的 212px，而真实是 240px，每次少拖 13%、角度偏十几度，必然失败。
    CAPTCHA_TRACK_SELECTORS = (
        ".passMod_slide-control",
        "[class*='slide-control']",
        ".passMod_slide-bar",
        "[class*='slide-bar']",
        "[class*='slide-track']",
        "[class*='spin-slider']",
    )
    # 拖动像素的兜底基准：百度页内公式 `ac_c = round(角度 × 212 / 360, 2)` 的 212。
    # 仅当既量不到轨道、又读不到页面自报旋转比例时才用得上。
    CAPTCHA_TRACK_PX_FALLBACK = 212.0

    # 读「滑块位移 → 图片旋转角度」的**页面自报状态**，用于运行时自校准。
    # 实测：拖 120px → 图片 `transform: rotate(180deg)`，即 1.5°/px（360° ≈ 240px）。
    # 这个比例随版本/分辨率变化（212 / 238 / 240 都出现过），所以**绝不写死**，直接问页面。
    SLIDER_STATE_JS = r"""
    () => {
        const btn = document.querySelector('.passMod_slide-btn, [class*="slide-btn"]');
        const img = document.querySelector('img.passMod_spin-background, img[class*="spin-background"]');
        let dx = null;
        if (btn) {
            const m = /translateX\((-?[\d.]+)px\)/.exec(btn.getAttribute('style') || '');
            if (m) dx = parseFloat(m[1]);
        }
        let rot = null;
        if (img) {
            const m = /rotate\((-?[\d.]+)deg\)/.exec(img.getAttribute('style') || '');
            if (m) rot = parseFloat(m[1]);
            else {
                const t = getComputedStyle(img).transform;
                const v = t && t.match(/matrix\(([^)]+)\)/);
                if (v) { const p = v[1].split(',').map(Number); rot = Math.atan2(p[1], p[0]) * 180 / Math.PI; }
            }
        }
        return {dx: dx, rot: rot};
    }
    """

    # 把非方图居中裁成正方形。旋转角度模型要求方图，否则预测会系统性偏移。
    # 用 data URL 绘制到 canvas 不会污染画布（data URL 视为同源），所以不需要 CORS。
    SQUARE_CROP_JS = r"""
    (dataUrl) => new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => {
            try {
                const w = img.naturalWidth, h = img.naturalHeight;
                const side = Math.min(w, h);
                const sx = Math.floor((w - side) / 2), sy = Math.floor((h - side) / 2);
                const c = document.createElement('canvas');
                c.width = side; c.height = side;
                c.getContext('2d').drawImage(img, sx, sy, side, side, 0, 0, side, side);
                // 必须用 png：jpeg 不支持透明背景，会让角度识别结果完全错误
                resolve({ dataUrl: c.toDataURL('image/png'), w, h });
            } catch (e) { reject(String(e)); }
        };
        img.onerror = () => reject('image_load_failed');
        img.src = dataUrl;
    })
    """

    # ── 反检测脚本 ────────────────────────────────────────────────────────
    # 诚实说明（2026-09 实测）：
    #   * 爱企查确实存在拦截形态：命中时显示“请关闭浏览器的调试窗口再访问页面！”再跳 about:blank。
    #   * 但它是**间歇性**的：交错顺序重复测试 8 次，连"完全不伪装"也 8/8 通过 ——
    #     因此**无法把绕过归因于任何单一手段**（早先"console 拦截有效"的结论，
    #     很可能是测试顺序/节流造成的假象，不能当真）。
    #   * 所以这份脚本的定位是**低成本的兜底保险**，不是"已破解"的证明。
    #     真正可靠的是：命中拦截时能被识别（`_detect_antibot_page`）、并触发熔断而不是空转。
    #
    # 两条硬要求（改动时请守住）：
    #   ① 绝不触碰页面对象：不得对参数调用 String()/toString()/JSON.stringify ——
    #      那会触发对方埋的 getter / Proxy，反而把我们自己暴露出去（实测过：旧版 `String(a)`
    #      会触发 `toString` getter 与 Proxy 的 get）。
    #   ② 改写要"像原生"：toString 报 [native code]、name/length/property 形态与原生一致。
    ANTI_DEBUG_SCRIPT = """
    (() => {
        // ---------- ① 让被改写的函数看起来是原生 ----------
        const nativeToString = Function.prototype.toString;
        const fakeNames = new WeakMap();          // 闭包内，外部拿不到

        const hidePatch = (fn, name) => {
            try {
                Object.defineProperty(fn, 'name', { value: name, configurable: true });
                Object.defineProperty(fn, 'length', { value: 0, configurable: true });
            } catch (e) { /* 某些内核不允许，忽略 */ }
            fakeNames.set(fn, name);
            return fn;
        };

        const toStringShim = function toString() {
            if (fakeNames.has(this)) return 'function ' + fakeNames.get(this) + '() { [native code] }';
            return nativeToString.call(this);
        };
        hidePatch(toStringShim, 'toString');
        // 与原生描述符保持一致：writable=true, enumerable=false, configurable=true
        Object.defineProperty(Function.prototype, 'toString',
            { value: toStringShim, writable: true, enumerable: false, configurable: true });

        // ---------- ② console 包装：只做类型判断，绝不调用页面代码 ----------
        const PLACEHOLDER = '[object]';
        const safe = (a) => {
            const t = typeof a;
            if (a === null || t === 'string' || t === 'number' || t === 'boolean' || t === 'undefined') return a;
            if (t === 'bigint' || t === 'symbol') { try { return String(a); } catch (e) { return PLACEHOLDER; } }
            return PLACEHOLDER;   // object / function：一律占位，不触碰
        };
        ['log', 'info', 'warn', 'error', 'debug', 'dir', 'table', 'trace'].forEach((k) => {
            const orig = console[k];
            if (typeof orig !== 'function') return;
            const bound = orig.bind(console);
            const wrapper = function () {
                try { return bound(...Array.prototype.map.call(arguments, safe)); } catch (e) { return undefined; }
            };
            hidePatch(wrapper, k);
            // console.log 在 Chrome 里是**自有属性**（不是原型方法），描述符为 w/e/c 全 true，照抄
            try {
                Object.defineProperty(console, k,
                    { value: wrapper, writable: true, enumerable: true, configurable: true });
            } catch (e) { /* 忽略 */ }
        });

        // ---------- ③ 指纹：**只在检测到真实异常时才修正** ----------
        // 实测（CDP 附着真实 Chrome + 无伪装）原生状态本来就正常：
        //   navigator.webdriver === false（且是 Navigator.prototype 上的 accessor）
        //   navigator.languages === ['zh-CN','zh']    navigator.plugins.length === 5
        //   window.chrome 的自有键 = loadTimes / csi / app（**本来就没有 runtime**）
        //   WebGL = ANGLE (Intel, ...) 硬件渲染
        // 因此下面每一项都带守卫：条件不成立就一行都不动。
        // 教训：把本来就真实的值改成假值（或给 navigator 挂自有属性掩盖原型 accessor）
        //       只会制造**新的不一致**，比不伪装更容易被识别。
        //       尤其不要补 window.chrome.runtime —— 正常页面没有它，补上即破绽。
        try {
            if (navigator.webdriver === true) {          // 仅 --enable-automation 启动时为 true
                Object.defineProperty(Navigator.prototype, 'webdriver',
                    { get: () => false, configurable: true });
            }
        } catch (e) { /* 忽略 */ }

        try {
            if (!navigator.languages || navigator.languages.length === 0) {
                Object.defineProperty(Navigator.prototype, 'languages',
                    { get: () => ['zh-CN', 'zh'], configurable: true });
            }
        } catch (e) { /* 忽略 */ }

        try {
            if (!navigator.plugins || navigator.plugins.length === 0) {   // headless 的典型特征
                Object.defineProperty(Navigator.prototype, 'plugins',
                    { get: () => [1, 2, 3, 4, 5].map((i) => ({
                        name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format',
                    })), configurable: true });
            }
        } catch (e) { /* 忽略 */ }

        // WebGL：只在**软件渲染**（SwiftShader / llvmpipe 等"无 GPU"特征）时才伪装。
        // 硬件渲染时保持真实值 —— 伪造 vendor/renderer 反而会与其他 WebGL 报告值对不上。
        try {
            const GL_SOFT = /SwiftShader|Software|llvmpipe|Basic Render|Mesa/i;
            const readRenderer = () => {
                const gl = document.createElement('canvas').getContext('webgl');
                if (!gl) return '';
                const ext = gl.getExtension('WEBGL_debug_renderer_info');
                return String(ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER));
            };
            if (GL_SOFT.test(readRenderer())) {
                [window.WebGLRenderingContext, window.WebGL2RenderingContext].forEach((Ctor) => {
                    const proto = Ctor && Ctor.prototype;
                    if (!proto || !proto.getParameter) return;
                    const orig = proto.getParameter;
                    const patchedGetParam = function (p) {
                        const v = orig.call(this, p);
                        if (p === 37445) return 'Intel Inc.';                       // UNMASKED_VENDOR_WEBGL
                        if (p === 37446) return 'Intel Iris OpenGL Engine';         // UNMASKED_RENDERER_WEBGL
                        return v;
                    };
                    hidePatch(patchedGetParam, 'getParameter');
                    Object.defineProperty(proto, 'getParameter',
                        { value: patchedGetParam, writable: true, enumerable: false, configurable: true });
                });
            }
        } catch (e) { /* 忽略 */ }
    })();
    """

    # 卡片文本里的「标签：值」
    CARD_FIELDS = {
        "legal_person": ("法定代表人", "负责人", "经营者"),
        "reg_capital": ("注册资本",),
        "setup_date": ("成立时间", "成立日期"),
        "phone": ("电话", "联系电话"),
        "email": ("邮箱", "电子邮箱"),
        "status": ("经营状态", "登记状态"),
    }
    # 详情页文本里的「标签：值」。实测这些标签**大多不带冒号**（如 `实缴资本 -`、
    # `参保人数 12人`、`经营状态 开业`、`注册地址 深圳市…`），所以冒号必须可选。
    DETAIL_FIELDS = {
        "legal_person": ("法定代表人", "负责人", "经营者"),
        "reg_capital": ("注册资本",),
        "paid_capital": ("实缴资本",),
        "insured_users": ("参保人数",),
        "status": ("经营状态", "登记状态"),
        "reg_address": ("注册地址", "企业地址", "地址"),
        "phone": ("电话", "联系电话"),
        "email": ("邮箱", "电子邮箱"),
        "credit_code": ("统一社会信用代码",),
        "setup_date": ("成立日期", "成立时间"),
    }

    # 「搜索结果页浏览完 → 点击进入企业详情」之间的随机停顿区间（秒）。
    # 真人从看到搜索结果到点进某家企业，天然有数秒的不规则停顿；
    # 固定间隔本身就是可检测的规律，所以取随机区间。
    DETAIL_ENTER_DELAY = (3.0, 5.0)

    # 详情页结构化提取：DOM 键值对（.label / .person-title 的兄弟节点）
    # + 纯文本「标签 [冒号可选] 值」。两者合并，各取所长。
    # 说明：法定代表人节点后面先是头像首字（如「李」），名字在同一行的下一个兄弟节点，
    #      所以兄弟链要向后探 4 个；纯文本正则只能拿到单字，必须靠 DOM 兜住。
    DETAIL_EXTRACT_JS = r"""
    () => {
        const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
        const PLACEHOLDERS = ['-', '--', '暂无', '未公开', '未披露', '无', '暂无网址', '查看地图'];
        // 带字段名的占位符（`暂无注册资本`/`暂无电话`）也要视为未披露，否则会当成有效值混进表
        const PLACEHOLDER_PREFIXES = ['暂无', '未公开', '未披露', '未公示', '无数据'];
        // 页面上紧贴值、无空格的可点噪声（如 `注册资本：50万(元)历史变动`），必须剥掉
        const NOISE_TAIL = ['历史变动', '查看地图', '附近公司', '获取更多邮箱', '进入官网', '更多', '展开'];
        const clean = (v) => {
            let x = norm(v);
            let prev;
            do {
                prev = x;
                NOISE_TAIL.forEach((n) => { if (x.endsWith(n)) x = x.slice(0, -n.length).trim(); });
            } while (x !== prev);
            if (!x || PLACEHOLDERS.includes(x)) return '';
            if (PLACEHOLDER_PREFIXES.some((p) => x.startsWith(p))) return '';
            return x;
        };

        // 已知标签白名单：只在精确命中时才认，避免误抓同名文本
        const KNOWN = ['法定代表人', '负责人', '经营者', '注册资本', '实缴资本', '参保人数',
                       '经营状态', '登记状态', '注册地址', '企业地址', '地址', '成立日期',
                       '成立时间', '统一社会信用代码', '电话', '联系电话', '邮箱', '电子邮箱'];
        // 值本身是另一条标签 → 说明该字段缺值（页面把两条标签挨在一起渲染），必须丢弃。
        // 实测踩到：个体户注册资本为空时 `注册资本 实缴资本` 会被正则吞成 `实缴资本`。
        const isLabel = (v) => !!v && KNOWN.some((k) => v === k || v.startsWith(k));

        // ① DOM 键值对：覆盖 .label/.person-title（页头联系方式）与 td/th（工商信息表格）
        const domPairs = {};
        document.querySelectorAll('.label, .person-title, td, th').forEach((el) => {
            const label = norm(el.innerText).replace(/[:：]$/, '');
            if (!label || label.length > 14 || !KNOWN.includes(label)) return;
            let n = el.nextElementSibling;
            for (let i = 0; i < 4 && n; i++) {
                const t = clean(n.innerText);
                if (t && t.length > 1 && !isLabel(t)) {
                    if (!domPairs[label]) domPairs[label] = t;
                    break;
                }
                n = n.nextElementSibling;
            }
        });

        // ② 纯文本「标签 值」（冒号可选；值取到空格为止，中文地址不含空格）
        const text = norm(document.body ? document.body.innerText : '');
        const grab = (labels) => {
            for (const L of labels) {
                const m = text.match(new RegExp(L + '\\s*[:：]?\\s*([^\\s]{1,80})'));
                if (m) {
                    const v = clean(m[1]);
                    if (v && !isLabel(v)) return v;
                }
            }
            return '';
        };
        const pick = (labels) => {
            for (const L of labels) {
                if (domPairs[L]) return domPairs[L];   // DOM 值更干净，优先
            }
            return grab(labels);
        };

        const out = {
            legal_person: pick(['法定代表人', '负责人', '经营者']),
            reg_capital: pick(['注册资本']),
            paid_capital: pick(['实缴资本']),
            insured_users: pick(['参保人数']),
            status: pick(['经营状态', '登记状态']),
            reg_address: pick(['注册地址', '企业地址', '地址']),
            phone: pick(['电话', '联系电话']),
            email: pick(['邮箱', '电子邮箱']),
            credit_code: pick(['统一社会信用代码']),
            setup_date: pick(['成立日期', '成立时间']),
        };
        // 邮箱兜底：页面上常出现多个邮箱，取第一个
        if (!out.email) {
            const m = text.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/);
            if (m) out.email = m[0];
        }
        // 电话兜底
        if (!out.phone) {
            const m = text.match(/(?:(?:\+|00)?86[\s-]?)?(1[3-9]\d{9}|0\d{2,3}-?\d{7,8})/);
            if (m) out.phone = m[1];
        }
        // 法定代表人兜底：放弃单字头像首字
        if (out.legal_person && out.legal_person.length <= 1) out.legal_person = '';
        return out;
    }
    """
    # 值为占位符时视为「未披露」
    EMPTY_TOKENS = {"", "-", "--", "暂无", "未公开", "未披露", "无", "查看地图"}
    # 「未披露」类占位符的**前缀**：页面会写 `暂无注册资本`、`暂无电话` 这种带字段名的占位，
    # 它们不含任何信息，必须归一到空，否则会以"看起来有值"的形态混进表里。
    PLACEHOLDER_PREFIXES = ("暂无", "未公开", "未披露", "未公示", "无数据")
    # 页面上紧贴值、无空格的可点噪声（如 `注册资本：50万(元)历史变动`）
    NOISE_TAIL = ("历史变动", "查看地图", "附近公司", "获取更多邮箱", "进入官网", "更多", "展开")

    def __init__(
        self,
        *,
        captcha_mode: str = "auto",
        captcha_provider: str | None = None,
        captcha_max_attempts: int = 3,
        captcha_verify_timeout: float = 10.0,
    ) -> None:
        """
        captcha_mode:
            auto   —— 先尝试打码平台自动识别（需在 .env 配好凭据），失败再转人工等待
            manual —— 不自动识别，直接提示并等待人工滑过（改动前的行为）
            off    —— 无人值守：识别到验证码立即放弃该企业，不等待（计入熔断）
        captcha_provider: auto|ttshitu|yunma|none，None 视作 auto（由 config 决定）
        captcha_max_attempts: 自动识别的"取新图重来"轮数（失败会换新图，故非单纯重试）
        captcha_verify_timeout: 拖动后等待验证结果的最长秒数
        """
        self.last_error = ""
        self.blocked = False          # 是否被反爬/验证码拦下（供调度器决定是否切环境）
        self.last_page_url = ""

        self.captcha_mode = (captcha_mode or "auto").strip().lower()
        if self.captcha_mode not in ("auto", "manual", "off"):
            self.captcha_mode = "auto"
        self.captcha_provider = captcha_provider
        self.captcha_max_attempts = max(1, int(captcha_max_attempts))
        self.captcha_verify_timeout = max(2.0, float(captcha_verify_timeout))
        # 人工等待上限：配置优先（.env 的 CAPTCHA_WAIT_SECONDS），否则用类常量
        try:
            self.captcha_wait = float(
                getattr(config, "CAPTCHA_WAIT_SECONDS", self.CAPTCHA_WAIT_TIMEOUT)
                or self.CAPTCHA_WAIT_TIMEOUT
            )
        except (TypeError, ValueError):
            self.captcha_wait = self.CAPTCHA_WAIT_TIMEOUT
        # 落点随机偏移（px）。默认 0：见 config.py 的说明 —— 一般不建议开，
        # 因为平台识别本身就有几度误差，人为偏移只会吃掉有限的容差余量。
        try:
            self.captcha_landing_jitter = max(
                0.0, float(getattr(config, "CAPTCHA_LANDING_JITTER_PX", 0) or 0)
            )
        except (TypeError, ValueError):
            self.captcha_landing_jitter = 0.0
        self._solver: CaptchaSolver | None = None
        self._solver_resolved = False
        # 过码冷却：monotonic 时间戳。0 = 不在冷却期。见 CAPTCHA_COOLDOWN_AFTER_PASS。
        self._captcha_cool_until = 0.0

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    async def _human_delay(self, min_s: float = 1.0, max_s: float = 2.0) -> None:
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def _human_browse(self, page: Page, *, scrolls: tuple = (1, 2)) -> None:
        """模拟真人浏览：落一次鼠标位置 + 1~2 次不规则滚动 + 随机停顿。

        两个实际作用（不是为了"玄学拟人"）：
        ① 产生自然的交互事件序列，而不是"导航完就立刻抽 DOM"；
        ② **触发懒渲染** —— 详情页的工商信息表格是懒加载的，滚动能帮我们拿全字段。
        滚动失败不影响主流程（它不是取数的必需动作）。
        """
        try:
            result = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
            width = int((result or [1280, 800])[0]) or 1280
            height = int((result or [1280, 800])[1]) or 800
        except Exception:
            width, height = 1280, 800
        try:
            # 真实滚轮事件都带坐标，先把鼠标落到页面中部附近
            await page.mouse.move(
                random.randint(int(width * 0.25), max(int(width * 0.25) + 1, int(width * 0.75))),
                random.randint(int(height * 0.2), max(int(height * 0.2) + 1, int(height * 0.6))),
            )
            await self._human_delay(0.15, 0.4)
            for _ in range(random.randint(*scrolls)):
                await page.mouse.wheel(0, random.randint(180, 520))
                await self._human_delay(0.25, 0.7)
            # 回翻一点：真人不会一路滚到底
            await page.mouse.wheel(0, -random.randint(60, 200))
            await self._human_delay(0.2, 0.5)
        except Exception as e:
            logger.debug(f"[爱企查] 拟人化滚动跳过（不影响取数）: {e!r}")

    @staticmethod
    def _clean_value(value: str) -> str:
        if not value:
            return ""
        v = str(value).strip().strip("，,；;")
        # 反复剥离紧贴值的可点噪声（页面常把 `历史变动` 直接接在数值后面，无空格）
        prev = None
        while v != prev:
            prev = v
            for noise in AiQiChaEnricher.NOISE_TAIL:
                if v.endswith(noise):
                    v = v[: -len(noise)].strip()
        if not v or v in AiQiChaEnricher.EMPTY_TOKENS:
            return ""
        # 带字段名的占位符（`暂无注册资本` / `暂无电话`）同样视为未披露
        if v.startswith(AiQiChaEnricher.PLACEHOLDER_PREFIXES):
            return ""
        return v

    @classmethod
    def _all_labels(cls) -> tuple:
        """CARD_FIELDS + DETAIL_FIELDS 里所有标签，长的排前面（用于「值是不是标签」判定）。"""
        cached = getattr(cls, "_ALL_LABELS_CACHE", None)
        if cached is None:
            s = set()
            for mapping in (cls.CARD_FIELDS, cls.DETAIL_FIELDS):
                for labels in mapping.values():
                    s.update(labels)
            cached = tuple(sorted(s, key=len, reverse=True))
            cls._ALL_LABELS_CACHE = cached
        return cached

    @classmethod
    def _looks_like_label(cls, value: str) -> bool:
        """值本身是不是**另一条标签**。

        实测踩到：某个体户的注册资本为空时，页面把两条标签挨在一起
        （`注册资本 实缴资本`），正则 `注册资本\\s*[:：]?\\s*(\\S+)` 就把 `实缴资本`
        当成值吞了 —— 结果 `注册资本='实缴资本'` 这种垃圾值进了表。
        """
        v = (value or "").strip()
        return any(v == lab or v.startswith(lab) for lab in cls._all_labels())

    @classmethod
    def _extract_labeled(cls, text: str, labels: tuple) -> str:
        """从整页文本里按「标签[：] 值」取值（冒号可选；值取到空格为止，中文地址不含空格）。"""
        if not text:
            return ""
        for label in labels:
            m = re.search(rf"{label}\s*[:：]?\s*([^\s]{{1,80}})", text)
            if m:
                v = cls._clean_value(m.group(1))
                if v and not cls._looks_like_label(v):
                    return v
        return ""

    @classmethod
    def _extract_all(cls, text: str, mapping: dict) -> dict:
        out = {}
        for key, labels in mapping.items():
            v = cls._extract_labeled(text, labels)
            if v:
                out[key] = v
        # 邮箱/电话单独兜底（页面上不一定带标签）
        if not out.get("email"):
            m = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text or "")
            if m:
                out["email"] = m.group(0)
        if not out.get("phone"):
            m = re.search(r"(?:(?:\+|00)?86[\s-]?)?(1[3-9]\d{9}|0\d{2,3}-?\d{7,8})", text or "")
            if m:
                out["phone"] = m.group(1)
        return out

    # ------------------------------------------------------------------ #
    # 验证码：自动打码（可选）+ 人工兜底
    # ------------------------------------------------------------------ #
    @staticmethod
    def _frames(page: Page) -> list:
        """主框架 + 所有子 iframe。

        **验证码可能被嵌在 iframe 里**（overlay 形式），而 Playwright 的
        `page.locator()` / `page.evaluate()` **只作用于主框架** —— 不扫 frame 就会
        "明明检测到了验证码、却取不到图/找不到滑块"。
        """
        try:
            frames = list(page.frames)
        except Exception:
            return [page.main_frame]
        if not frames:
            return [page.main_frame]
        main = page.main_frame
        return [main] + [f for f in frames if f is not main]

    async def _captcha_state(self, page: Page) -> bool | None:
        """三态检测：True=仍有验证码；False=**确实**没有；None=**判定不了**（页面正在跳转/刷新）。

        为什么必须区分 None 与 False：旧写法把 `evaluate` 抛异常当成"没有验证码"，
        于是**验证码页自己一刷新就被误判成"已通过"** → 流程继续 → 立刻又被拦 →
        在浏览器里就表现为"一直在刷新图像验证码"。
        """
        unknown = False
        for fr in self._frames(page):
            try:
                if await fr.evaluate(
                    r"""() => !!(document.querySelector('.vcode-spin-button, .passMod_slide-btn, [id*="vcode-spin-button"], [class*="slide-btn"], [class*="spin-button"], #passMod_spin, [class*="vcode"]')
                            || document.title.includes('安全验证')
                            || /滑动验证|旋转图片|拖动左侧滑块/.test(document.body ? document.body.innerText : ''))"""
                ):
                    return True
            except Exception:
                unknown = True          # 该 frame 读不到 —— 不代表没有，不能当作"已通过"
        return None if unknown else False

    async def _captcha_present(self, page: Page) -> bool:
        """当前页（含 iframe）是否**确实**在显示验证码（用于"要不要处理"的门槛）。

        只有"确实检测到"才返回 True —— 判定不了（None）按"没有"处理，避免把仍在加载的
        iframe 误当成验证码、白等人工 60 秒。
        **反过来**，"是否已通过"的判定必须用 `_captcha_state(...) is False`
        （见 `_manual_wait_captcha` / `_wait_captcha_outcome`）——那里把"读不到"
        当成"没有"会让验证码页一刷新就被误判成通过。

        刻意**不**把 `img.passMod_spin-background` 算进来：验证通过后图片元素可能
        还留在页面上（只有滑块/容器消失），把它当信号会让"已通过"被误判成"还在验证"。
        判断"是否还需要解决"看的是**可交互控件**，不是图片。
        """
        return (await self._captcha_state(page)) is True

    # 控件就绪判定：图片有可用 src，且滑块已不在 loading 态。
    # 实测（2026-09）控件是**异步**出现的：刚出现时 img 还没有 src、滑块带
    # `passMod_slide-btn-loading`；此时取图必然失败 —— 真实流水线里那句
    # "⚠️ [爱企查] 未能取到验证码图片" 就是这么来的（我手工测试先等了 2.5s 才没暴露）。
    CAPTCHA_READY_JS = r"""
    () => {
        const img = document.querySelector('img.passMod_spin-background, img[class*="spin-background"], img[id*="vcode-spin-img"]');
        const btn = document.querySelector('.passMod_slide-btn, [class*="slide-btn"], [id*="vcode-spin-button"]');
        const src = img ? String(img.src || '') : '';
        const iOk = !!img && src.length > 32 && img.getBoundingClientRect().width > 10;
        const bOk = !!btn && btn.getBoundingClientRect().width > 10
                    && !/loading/i.test(String(btn.className || ''));
        return {img: iOk, btn: bOk, ready: iOk && bOk,
                nw: img ? img.naturalWidth : 0,
                cls: btn ? String(btn.className).slice(0, 60) : ''};
    }
    """

    async def _wait_captcha_ready(self, page: Page, timeout: float = 6.0) -> bool:
        """等验证码控件真正渲染好（含 iframe）。超时返回 False，调用方**仍应尝试**取图。

        这是"best effort"等待：它只减少"控件还没出来就去取图"的失败，
        不构成新的失败路径（等不到也照常往下走）。
        """
        deadline = time.monotonic() + timeout
        last = {}
        while True:
            for fr in self._frames(page):
                try:
                    last = await fr.evaluate(self.CAPTCHA_READY_JS) or {}
                except Exception:
                    continue
                if last.get("ready"):
                    return True
            if time.monotonic() >= deadline:
                logger.debug(f"[爱企查] 验证码控件 {timeout:.0f}s 内未判定为就绪，仍尝试取图: {last}")
                return False
            await asyncio.sleep(0.2)

    def _resolve_wait(self, timeout: float | None) -> float:
        """把"人工等待秒数"的 None（用配置默认）解析成具体秒数；<=0 表示不等待。"""
        if timeout is None:
            return self.captcha_wait
        try:
            return max(0.0, float(timeout))
        except (TypeError, ValueError):
            return self.captcha_wait

    async def check_and_wait_captcha(self, page: Page, timeout: float | None = None) -> bool:
        """检测验证码并处理。True=可继续；False=未通过（调用方应标记失败并计入熔断）。

        处理顺序：自动打码（captcha_mode=auto 且打码平台已配置）→ 人工等待（timeout>0）→ 放弃。
        `timeout` 是**人工等待**的最长秒数（默认 60，与调度器熔断口径一致）。
        """
        if not await self._captcha_present(page):
            return True
        return await self._handle_captcha(page, timeout=self._resolve_wait(timeout))

    async def _handle_captcha(self, page: Page, *, timeout: float) -> bool:
        if self.captcha_mode == "off":
            self.blocked = True
            self.last_error = "captcha_skipped(off)"
            print("      ⏭️ [爱企查] 命中验证码，captcha_mode=off：跳过该企业、不等待")
            return False

        if self.captcha_mode == "auto":
            if await self._auto_solve_captcha(page):
                await self._comfort_rest("自动过码通过后的缓和停顿")
                self._arm_captcha_cooldown("自动过码")
                return True
            if self._get_solver() is None:
                print("      ℹ️ [爱企查] 未启用打码平台（.env 未配凭据）→ 转人工等待")
            else:
                print("      ⚠️ [爱企查] 自动打码未通过 → 转人工等待")

        if timeout and timeout > 0:
            if await self._manual_wait_captcha(page, timeout):
                await self._comfort_rest("人工验证通过后的缓和停顿")
                self._arm_captcha_cooldown("人工过码")
                return True
            return False

        self.blocked = True
        self.last_error = "captcha_unresolved"
        print("      ❌ [爱企查] 验证码未解决（未启用人工等待），本企业标记失败")
        return False

    async def _manual_wait_captcha(self, page: Page, timeout: float) -> bool:
        """**暂停并等待人工过验证**（口径对齐天眼查 `_handle_captcha_if_needed`）。

        与天眼查一致的三件事：① 醒目框线提示，明确告诉人去哪个窗口做什么；
        ② 长等待（默认 240s，别人为 60s 就对不上）；③ 通过后加一段缓和停顿再继续。

        **只有"确确实实没有验证码"才算通过**（`_captcha_state` 返回 False），
        "判定不了"（页面正在刷新/跳转）继续等 —— 否则验证码页一刷新就会被误判成
        "已通过"，流程继续、立刻又被拦，表现为"一直在刷新图像验证码"。
        """
        print("\n" + "!" * 60)
        print("🚨 [爱企查风控触发] 检测到滑块 / 旋转验证码！")
        print("👉 请切回已打开的 Chrome 浏览器窗口，【手动滑动完成验证】。")
        print("⏳ 流水线已自动暂停等待，验证通过后会自动恢复运行...")
        print(f"   （最多等待 {timeout:.0f} 秒；无人值守可用 --captcha-mode off 直接跳过）")
        print("!" * 60 + "\n")

        waited = 0.0
        step = 2.0
        hint_every = max(step, float(self.CAPTCHA_WAIT_HINT_EVERY))
        next_hint = hint_every
        while waited < timeout:
            await asyncio.sleep(step)
            waited += step
            try:
                if page.is_closed():
                    print(f"      ✅ [爱企查] 验证页已关闭（等待 {waited:.0f}s），恢复流程")
                    return True
            except Exception:
                pass
            state = await self._captcha_state(page)
            if state is False:
                print(f"      ✅ [爱企查] 人工验证成功（等待 {waited:.0f}s），恢复自动化抓取！")
                return True
            if waited >= next_hint:
                next_hint += hint_every
                if state is None:
                    # 与"确实还在"区分开，避免被误读成"卡住不动"
                    print(f"      ℹ️ [爱企查] 验证页正在刷新/跳转，继续等待…"
                          f"（{waited:.0f}/{timeout:.0f}s）")
                else:
                    print(f"      ⏳ [爱企查] 仍在等待人工验证…（{waited:.0f}/{timeout:.0f}s）")
        self.blocked = True
        self.last_error = f"captcha_timeout({timeout:.0f}s)"
        print(f"      ❌ [爱企查] 人工验证等待超时（{timeout:.0f}s），本企业标记为重试")
        return False

    async def _comfort_rest(self, desc: str) -> None:
        """验证通过后的缓和停顿（比天眼查的 (3.23, 6.29) 更长，理由见常量注释）。

        作用不是"装样子"：刚过完验证就立刻发下一个请求，最容易立刻再被拦。
        """
        lo, hi = self.CAPTCHA_REST_AFTER_PASS
        sec = round(random.uniform(lo, hi), 2)
        print(f"      ⏱️ [爱企查缓和停顿] {desc}，等待 {sec} 秒...")
        await asyncio.sleep(sec)

    def _arm_captcha_cooldown(self, source: str) -> None:
        """过码通过时启用冷却窗口（本次会话内的下一次真实导航会被拦下补足）。"""
        lo, hi = self.CAPTCHA_COOLDOWN_AFTER_PASS
        self._captcha_cool_until = time.monotonic() + random.uniform(lo, hi)
        logger.debug(f"[爱企查] {source}通过，已启用导航冷却窗口")

    async def _respect_captcha_cooldown(self, desc: str) -> None:
        """真实导航（打开搜索页/进详情页）前补足过码冷却。

        这是"过了一个验证码立刻又弹一个"的直接对策：刚过完码的会话立刻高频
        导航，是风控再次弹码的最强信号。冷却没走完就先补足（带少量抖动，
        避免"冷却一结束瞬间就发起导航"这种新的规律）。
        """
        wait = self._captcha_cool_until - time.monotonic()
        if wait <= 0:
            return
        wait += random.uniform(0.0, 1.5)
        print(f"      ⏱️ [爱企查] {desc}前冷却补足 {wait:.1f} 秒（刚过完验证码，放缓防再弹）")
        await asyncio.sleep(wait)
        self._captcha_cool_until = 0.0

    # ---- 自动打码 ------------------------------------------------------ #
    def _get_solver(self) -> CaptchaSolver | None:
        """懒加载打码平台客户端（每个实例只解析一次配置）。"""
        if self._solver_resolved:
            return self._solver
        self._solver_resolved = True
        try:
            self._solver = CaptchaSolver.from_config(self.captcha_provider)
        except Exception as e:
            logger.warning(f"⚠️ [爱企查] 打码平台初始化失败，将走人工等待: {e!r}")
            self._solver = None
        return self._solver

    async def _find_visible(self, page: Page, selectors) -> Locator | None:
        """按选择器顺序找第一个**可见**元素，**含所有 iframe**。找不到返回 None。

        （既避免命中隐藏模板节点，也覆盖"验证码被嵌在 iframe 里"的情况：
        Playwright 的 `page.locator()` 只搜主框架，不扫 frame 就会一无所获。）
        """
        for fr in self._frames(page):
            for sel in selectors:
                try:
                    loc = fr.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        return loc
                except Exception:
                    continue
        return None

    @staticmethod
    def _looks_like_image(data: bytes) -> bool:
        """按**魔术字节**判断是不是图片。

        曾经用 `len(data) > 512` 当门槛，结果纯色小图（压缩后不到 300 字节）被误杀 ——
        大小根本不能说明问题；而 HTML 错误页动辄几 KB，反而能过关。所以按格式头判断。
        """
        if not data or len(data) < 64:
            return False
        return bool(
            data.startswith(b"\x89PNG\r\n\x1a\n")            # PNG
            or data.startswith(b"\xff\xd8\xff")              # JPEG
            or data.startswith(b"GIF8")                      # GIF
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")   # WebP
            or data.startswith(b"BM")                        # BMP
        )

    async def _maybe_square_crop(self, page: Page, image_bytes: bytes) -> bytes:
        """按 CAPTCHA_SQUARE_CROP 决定是否把图片居中裁成正方形；失败则原样返回。

        旋转角度模型要求方图（否则预测系统性偏移）。用 data URL 画到 canvas
        不会污染画布（data URL 视为同源），所以不需要 CORS 改造。
        """
        mode = str(getattr(config, "CAPTCHA_SQUARE_CROP", "auto") or "auto").lower()
        if mode == "off":
            return image_bytes
        try:
            data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
            out = await page.evaluate(self.SQUARE_CROP_JS, data_url) or {}
            w, h = int(out.get("w") or 0), int(out.get("h") or 0)
            if mode == "auto" and (not w or not h or abs(w - h) / max(w, h) <= 0.05):
                return image_bytes            # 本来就接近正方形，不折腾
            new_url = str(out.get("dataUrl") or "")
            if new_url.startswith("data:image"):
                return base64.b64decode(new_url.split(",", 1)[1])
        except Exception as e:
            logger.debug(f"[爱企查] 验证码方图预处理跳过（用原图）: {e!r}")
        return image_bytes

    async def _grab_captcha_image(self, page: Page) -> bytes | None:
        """取验证码图片字节。

        **优先级很重要：src 直连下载 > 元素截图**。
        原因：对页面元素截图可能把"防自动化水印/马赛克"一起截进去，会让角度识别完全错误；
        而 img 的 src 是服务端原图（多个开源实现的共同经验）。
        截图仅作最后兜底，并会打 debug 日志标明。
        """
        el = await self._find_visible(page, self.CAPTCHA_IMG_SELECTORS)
        if el is not None:
            src = ""
            try:
                src = (await el.get_attribute("src")) or ""
                if not src:
                    src = (await el.get_attribute("data-src")) or ""
            except Exception:
                src = ""

            if src.startswith("data:image"):
                try:
                    return await self._maybe_square_crop(page, base64.b64decode(src.split(",", 1)[1]))
                except Exception as e:
                    logger.debug(f"[爱企查] data URL 解码失败: {e!r}")
            elif src:
                try:
                    # 用浏览器上下文的请求（自动带 Cookie），并补 Referer —— 不带会被换成带水印的图
                    resp = await page.context.request.get(
                        src, headers={"Referer": "https://passport.baidu.com/"}, timeout=15000
                    )
                    if resp.ok:
                        body = await resp.body()
                        if self._looks_like_image(body):
                            return await self._maybe_square_crop(page, body)
                        logger.debug(f"[爱企查] 验证码 src 返回的不是图片（{len(body)} 字节，"
                                     f"头 8 字节={body[:8]!r}），回退截图")
                except Exception as e:
                    logger.debug(f"[爱企查] 验证码图直连下载失败，回退截图: {e!r}")

            try:
                shot = await el.screenshot(type="png")
                if self._looks_like_image(shot):
                    logger.debug("⚠️ [爱企查] 改用元素截图作为验证码图（可能含防自动化水印）")
                    return await self._maybe_square_crop(page, shot)
            except Exception as e:
                logger.debug(f"[爱企查] 验证码元素截图失败: {e!r}")

        logger.warning("⚠️ [爱企查] 未能取到验证码图片")
        return None

    async def _slider_geometry(self, page: Page) -> dict:
        """量出滑块与轨道的几何关系。

        返回 {"travel": 转一整圈对应的行程, "offset": 滑块相对轨道左端的偏移, "inset": 左侧内缩}。
        量不到的字段为 0，调用方据此回退（**优先用页面自报的旋转比例，几何只是兜底**）。

        行程的算法有个坑：不是「轨道宽 − 滑块宽」。实测 control=290、滑块=46、左内缩=3，
        而 100px 位移对应 rotate(151.2deg) → 真实行程 ≈ 240px（= 290 − 46 − 2×3 再取整），
        直接相减得到的 244 会偏 1.7%。所以这里减掉左右两侧内缩。
        """
        geom = {"travel": 0.0, "offset": 0.0, "inset": 0.0}
        try:
            handle = await self._find_visible(page, self.CAPTCHA_SLIDER_SELECTORS)
            if handle is None:
                return geom
            hbox = await handle.bounding_box(timeout=3000)
            if not hbox:
                return geom

            # ① 优先按选择器找轨道；② 找不到就用滑块父元素（实测新版的轨道就是滑块父元素
            #    `.passMod_slide-control`，所以这是可靠的通用兜底 —— 老版本选择器失效时也能活）
            tbox = None
            track = await self._find_visible(page, self.CAPTCHA_TRACK_SELECTORS)
            if track is not None:
                tbox = await track.bounding_box(timeout=3000)
            if not tbox or float(tbox.get("width") or 0) <= 0:
                tbox = await handle.evaluate(
                    """el => {
                        const p = el.parentElement;
                        if (!p) return null;
                        const r = p.getBoundingClientRect();
                        return {x: r.x, y: r.y, width: r.width, height: r.height};
                    }"""
                )
            if not tbox or float(tbox.get("width") or 0) <= 0:
                return geom

            inset = max(0.0, float(hbox["x"]) - float(tbox["x"]))
            travel = float(tbox["width"]) - float(hbox["width"]) - 2.0 * inset
            if travel <= 40:                     # 太小的值说明没量对，宁可用基准
                return geom
            geom["travel"] = travel
            geom["inset"] = inset
            # 滑块必须落在轨道内，偏移才可信（否则可能是误命中了别的元素）
            offset = float(hbox["x"]) - float(tbox["x"])
            if -2.0 <= offset <= travel + 2.0:
                geom["offset"] = max(0.0, min(travel, offset))
        except Exception as e:
            logger.debug(f"[爱企查] 滑块几何测量失败: {e!r}")
        return geom

    async def _captcha_track_px(self, page: Page) -> float:
        """「转一整圈（360°）」对应的拖动像素数（轨道可用行程）。

        优先级：CAPTCHA_TRACK_PX 配置 > 运行时量 DOM > 212（百度页内 ac_c 公式的基准）。
        之所以要量而不是写死：不同版本/分辨率下轨道宽度并不都是 212，
        公开实现里出现的 0.58 / 0.66 系数差异就来自这里。
        """
        try:
            override = float(getattr(config, "CAPTCHA_TRACK_PX", 0) or 0)
        except Exception:
            override = 0.0
        if override > 0:
            return override
        geom = await self._slider_geometry(page)
        if geom["travel"] > 40:
            return geom["travel"]
        logger.debug(f"[爱企查] 轨道宽度测量不可信，回退基准 {self.CAPTCHA_TRACK_PX_FALLBACK}px")
        return self.CAPTCHA_TRACK_PX_FALLBACK

    async def _slide_state(self, page: Page) -> dict:
        """读页面自报的滑块位移与图片旋转角（用于自校准）。读不到返回 {}。"""
        try:
            return await page.evaluate(self.SLIDER_STATE_JS) or {}
        except Exception:
            return {}

    async def _move_segment(self, page: Page, x0: float, y0: float,
                            from_px: float, to_px: float, *, fast: bool = False) -> None:
        """从 from_px 滑到 to_px（相对起点），轨迹模拟真人。

        easeOutCubic（起步快收尾慢）+ 纵向 ±1px 抖动；非探测段再加 3~7px 过冲后回正。
        必须用真实鼠标事件（而非 JS 改样式）：页面靠这些事件的轨迹生成提交用的加密参数。
        """
        delta = to_px - from_px
        if abs(delta) < 0.5:
            return
        sign = 1.0 if delta >= 0 else -1.0
        amount = abs(delta)
        steps = random.randint(6, 9) if fast else random.randint(14, 28)
        peak = amount if fast else amount + random.uniform(2.0, 9.0)
        points = [peak * (1.0 - (1.0 - i / steps) ** 3) for i in range(1, steps + 1)]
        if not fast:
            points += [amount - (peak - amount) * k / 3.0 for k in (2, 1)]   # 回正
            points.append(amount)
        for px in points:
            try:
                # 纵向抖动 ±1.6px：滑块高 46px、按点在中心，这点偏移很安全；
                # 太小（±1px 经坐标取整后只剩 2 种取值）会显得"贴着一根水平线拖"
                await page.mouse.move(x0 + from_px + sign * px, y0 + random.uniform(-1.6, 1.6))
            except Exception:
                return
            await asyncio.sleep(random.uniform(0.008, 0.022))

    async def _drag_to_angle(self, page: Page, handle: Locator, angle: float) -> float:
        """把滑块拖到「让图片转正」的位置。返回实际使用的位移（px）。

        **自校准**（这是实测踩出来的关键修正）：先小拖一段，然后直接读页面自己报的
        `translateX(Dpx)` 与 `rotate(Xdeg)`，算出真实的「度/像素」再决定最终位移。

        为什么不写死：实测百度 tuxing_v2 是 **1.5°/px（360° ≈ 240px）**，
        而公开资料里 212 / 238 都出现过；写错一个值每次就偏十几度，必然过不了。
        这个比例随版本与分辨率变化，所以唯一的可靠做法是**问页面本身**。

        同时按**增量**拖（不是按距离）：滑块起始位置未必在轨道最左端。
        """
        try:
            box = await handle.bounding_box(timeout=3000)
        except Exception:
            box = None
        if not box:
            raise RuntimeError("滑块 bounding_box 为空，无法拖动")
        x0 = box["x"] + box["width"] / 2.0
        y0 = box["y"] + box["height"] / 2.0

        angle = float(angle) % 360.0
        geom = await self._slider_geometry(page)
        travel = geom["travel"] if geom["travel"] > 40 else self.CAPTCHA_TRACK_PX_FALLBACK
        target = angle / 360.0 * travel                 # 几何估算（兜底用）

        await page.mouse.move(x0, y0)
        await self._human_delay(0.12, 0.3)
        await page.mouse.down()

        # ① 探测段（仅在目标够长时才探测）：小拖一段后读页面自报状态，反推真实的度/像素。
        #    比例与停顿都**随机化** —— 固定的"50% 处冻结 0.35s"会形成可检测的节奏。
        #    目标很短时不探测：那时几何误差折算到角度已可忽略（<1°）。
        probe = 0.0
        deg_per_px = None
        if target >= 30.0:
            probe = min(travel, max(18.0, target * random.uniform(0.32, 0.62)))
            await self._move_segment(page, x0, y0, 0.0, probe, fast=True)
            await asyncio.sleep(random.uniform(0.22, 0.50))   # 等 CSS 过渡跑完再读
            st = await self._slide_state(page)
            dx, rot = st.get("dx"), st.get("rot")
            if all(isinstance(v, (int, float)) for v in (dx, rot)) and abs(dx) > 10 and abs(rot) > 5:
                ratio = abs(float(rot)) / abs(float(dx))
                if 0.2 < ratio < 20:                        # 健康区间，排除读出垃圾值
                    deg_per_px = ratio
            if deg_per_px:
                calibrated = angle / deg_per_px
                logger.debug(f"[爱企查] 自校准: {dx:.0f}px → {rot:.1f}°（{deg_per_px:.4f}°/px），"
                             f"目标 {angle:.1f}° → {calibrated:.0f}px（几何估算 {target:.0f}px）")
                target = calibrated

        target = max(0.0, min(target, travel))

        # ② 主段：两种真人模式**随机选** —— 恒定"过冲后回正"本身也是一种规律
        if abs(target - probe) > 1.0:
            if random.random() < 0.35:
                # 模式 A：差一点停下 → 短停顿 → 向前补一小段（真人常见的修正）
                mid = probe + (target - probe) * random.uniform(0.85, 0.94)
                await self._move_segment(page, x0, y0, probe, mid, fast=True)
                await asyncio.sleep(random.uniform(0.10, 0.32))
                await self._move_segment(page, x0, y0, mid, target, fast=True)
            else:
                # 模式 B：冲过头再回正
                await self._move_segment(page, x0, y0, probe, target, fast=False)

        # ③ 落点：精确到位后做 1~2 次微调。
        #    两个要点：真人不会"一步到位后僵住"；而且**最后一点也必须有纵向抖动** ——
        #    原来落点 y 恒等于按下点（其余点都有 ±1px），是个很干净的规整特征。
        await page.mouse.move(x0 + target, y0 + random.uniform(-1.6, 1.6))
        for _ in range(random.randint(1, 2)):
            await asyncio.sleep(random.uniform(0.03, 0.10))
            await page.mouse.move(x0 + target + random.uniform(-1.0, 1.0),
                                  y0 + random.uniform(-1.4, 1.4))
        await asyncio.sleep(random.uniform(0.03, 0.10))
        # 终点 x 必须精确（服务端读的是最终位移）。可选落点偏移见 CAPTCHA_LANDING_JITTER_PX。
        final_x = target + random.uniform(-1.0, 1.0) * self.captcha_landing_jitter
        await page.mouse.move(x0 + final_x, y0 + random.uniform(-1.4, 1.4))
        await self._human_delay(0.05, 0.18)
        await page.mouse.up()
        return final_x

    # 「滑块还在」的判定（用于"是否已通过"）。必须扫所有 frame：
    # 若验证码嵌在 iframe 里而只查主文档，会**立刻误判成"已通过"**（主文档里本来就找不到滑块）。
    SLIDER_JS = r"""() => !!document.querySelector(".passMod_slide-btn, [id*='vcode-spin-button'], [class*='slide-btn']")"""

    async def _slider_state(self, page: Page) -> bool | None:
        """三态：True=滑块还在；False=**确实**没了；None=**判定不了**（frame 正在跳转/刷新）。

        和 `_captcha_state` 同理：把"读不到"当成"没了"，会让验证码页一刷新就被判成通过。
        """
        unknown = False
        for fr in self._frames(page):
            try:
                if await fr.evaluate(self.SLIDER_JS):
                    return True
            except Exception:
                unknown = True
        return None if unknown else False

    async def _wait_captcha_outcome(self, page: Page, *, timeout: float) -> bool:
        """拖动后等结果：滑块**确实**消失 / 页面跳走 = 通过；超时仍显示 = 未通过。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            try:
                if page.is_closed():
                    return True
            except Exception:
                return True          # 页面正在跳转/被销毁 → 视为已通过
            if (await self._slider_state(page)) is False:
                return True
        # 超时前最后确认一次：可能刚好在收尾阶段（同样只认"确实没有"）
        return (await self._captcha_state(page)) is False

    async def _auto_solve_captcha(self, page: Page) -> bool:
        """用打码平台自动过旋转验证码。通过 True；未通过/不可用 False（转人工）。

        注意这里是**多轮尝试**而不是"同一张图重试"：百度验证失败后会刷新出
        新的验证码图片，所以每一轮都要重新取图、重新识别。
        """
        solver = self._get_solver()
        if solver is None:
            return False

        for attempt in range(1, self.captcha_max_attempts + 1):
            try:
                # 控件是**异步**渲染的：刚检测到就取图必然失败（真实流水线里正是这么失败的）。
                # 这里先等它就绪；等不到也照常往下走（best effort，不制造新的失败路径）。
                await self._wait_captcha_ready(page)
                image = await self._grab_captcha_image(page)
                if not image:
                    print("      ⚠️ [爱企查] 取不到验证码图片"
                          "（控件未渲染完 / 在未加载的 iframe 里）→ 转人工等待")
                    return False

                angle = await solver.solve_rotate(image)
                if angle is None:
                    return False

                # 平台返回的是"转正所需顺时针角度"。CAPTCHA_ANGLE_SIGN=-1 时取补角
                # （相当于反向拖动），因为滑块只能从左端往右拖。
                sign = float(getattr(config, "CAPTCHA_ANGLE_SIGN", 1.0) or 1.0)
                if sign < 0:
                    angle = (360.0 - angle) % 360.0

                handle = await self._find_visible(page, self.CAPTCHA_SLIDER_SELECTORS)
                if handle is None:
                    print("      ⚠️ [爱企查] 找不到滑块，转人工等待")
                    return False

                print(f"      🤖 [爱企查] 打码识别 {angle:.1f}°，开始拖动"
                      f"（第 {attempt}/{self.captcha_max_attempts} 轮）")
                used = await self._drag_to_angle(page, handle, angle)
                print(f"         已拖到 {used:.0f}px（角度→像素由页面自校准）")

                if await self._wait_captcha_outcome(page, timeout=self.captcha_verify_timeout):
                    print("      ✅ [爱企查] 自动打码通过")
                    return True          # 缓和停顿由 _handle_captcha 统一加

                print(f"      ↻ [爱企查] 第 {attempt} 轮未通过，换新图重试")
                await self._human_delay(0.8, 1.3)
            except CaptchaProviderError as e:
                print(f"      ⚠️ [爱企查] 打码平台错误: {e}")
                return False
            except Exception as e:
                print(f"      ⚠️ [爱企查] 自动过码异常: {type(e).__name__}: {str(e)[:80]}")
                return False
        return False

    # 验证码页（百度安全验证）可能被开在**另一个标签页**，原标签页只留空白。
    # 只检查当前页会把这种情况误判成「查无此企业」，导致熔断永远不触发。
    CAPTCHA_URL_MARKERS = ("wappass.baidu.com", "/captcha/", "verify")

    async def _find_captcha_page(self, page: Page):
        """在整个浏览器上下文里找百度安全验证页（含它自己）。找不到返回 None。"""
        try:
            context = page.context
            pages = list(context.pages) if context else [page]
        except Exception:
            pages = [page]

        # 把当前页排在前面，命中即返回
        ordered = [page] + [p for p in pages if p is not page]
        for candidate in ordered:
            try:
                url = (candidate.url or "").lower()
                if not any(m in url for m in self.CAPTCHA_URL_MARKERS):
                    continue
                info = await candidate.evaluate(
                    r"""() => ({title: document.title,
                                text: (document.body ? document.body.innerText : '').slice(0, 300)})"""
                )
                blob = f"{info.get('title', '')} {info.get('text', '')}"
                if any(k in blob for k in ("安全验证", "滑动验证", "旋转图片", "请输入验证码")):
                    return candidate
            except Exception:
                continue
        return None

    async def _detect_antibot_page(self, page: Page, captcha_timeout: float | None = None) -> bool:
        """识别拦截：① 反调试页（当前页）② 验证码页（当前页或**其它标签页**）。

        命中验证码时会打印指引并等待人工处理（最多 captcha_timeout 秒）；
        处理通过则返回 False（可继续），超时或确认被拦则返回 True。
        """
        try:
            text = await page.evaluate(
                "() => (document.body ? document.body.innerText : '').slice(0, 200)"
            )
        except Exception:
            text = ""
        if text and ("调试窗口" in text or "请关闭浏览器" in text):
            self.blocked = True
            self.last_error = "antibot_debug_window"
            print("      🚫 [爱企查] 命中反调试拦截（请关闭浏览器的调试窗口）——本页无数据")
            return True

        captcha_page = await self._find_captcha_page(page)
        if captcha_page is None:
            return False

        elsewhere = captcha_page is not page
        print(f"      🔒 [爱企查] 检测到百度安全验证页"
              f"{'（开在另一个标签页）' if elsewhere else ''}: {captcha_page.url[:80]}")
        if await self.check_and_wait_captcha(captcha_page, timeout=captcha_timeout):
            print("      ✅ [爱企查] 验证已通过，继续")
            return False

        self.blocked = True
        self.last_error = "captcha_other_tab" if elsewhere else "captcha_timeout"
        print("      ❌ [爱企查] 验证未通过，本家标记失败（计入熔断）")
        return True

    # ------------------------------------------------------------------ #
    # 搜索页
    # ------------------------------------------------------------------ #
    async def _pick_card(self, page: Page, company_name: str) -> dict:
        """在结果列表里选出最匹配的卡片，返回 {pid, name, text, matched}。"""
        return await page.evaluate(
            r"""(args) => {
                const [target, selector] = args;
                const norm = (s) => (s || '').replace(/\s+/g, '').replace(/[（(].*?[)）]/g, '');
                const t = norm(target);
                const cards = Array.from(document.querySelectorAll(selector));
                const parsed = cards.map((c) => {
                    const titleEl = c.querySelector('h3.title, h3, .title');
                    const name = titleEl ? norm(titleEl.innerText) : '';
                    const logTitle = c.getAttribute('data-log-title') || '';
                    const pid = (logTitle.match(/item-(\d+)/) || [])[1] || '';
                    return {name, pid, text: (c.innerText || '').replace(/\s+/g, ' ')};
                });
                const exact = parsed.find((c) => c.name && c.name === t);
                if (exact) return {...exact, matched: true};
                const partial = parsed.find((c) => c.name && (c.name.includes(t) || t.includes(c.name)));
                if (partial) return {...partial, matched: true};
                if (parsed.length) return {...parsed[0], matched: false};
                return null;
            }""",
            [company_name.strip(), self.CARD_SELECTOR],
        )

    async def search_and_enrich(
        self,
        page: Page,
        company_name: str,
        *,
        render_timeout: float = 25.0,
        captcha_timeout: float | None = None,
    ) -> dict:
        """在当前传入的 CDP 标签页上检索并提取爱企查工商数据。

        返回标准字典（字段名与项目其它 enricher 对齐）：
            legal_person / reg_capital / paid_capital / insured_users /
            phone / email / reg_address / status / source
        附加（非破坏性）：matched_name / pid / credit_code / setup_date /
                          matched / blocked / last_error
        """
        data = {
            "legal_person": "",
            "reg_capital": "",
            "paid_capital": "",
            "insured_users": "",
            "phone": "",
            "email": "",
            "reg_address": "",
            "status": "",
            "source": "aiqicha",
            # —— 诊断字段 ——
            "matched_name": "",
            "pid": "",
            "credit_code": "",
            "setup_date": "",
            "matched": False,
            "blocked": False,
            "last_error": "",
        }
        self.last_error = ""
        self.blocked = False

        clean_name = (company_name or "").strip()
        if len(clean_name) < 4:
            self.last_error = "name_too_short"
            data["last_error"] = self.last_error
            return data

        try:
            # 反检测脚本必须在首个导航之前注入（add_init_script 会在每次新文档创建时先于页面脚本执行）。
            # 只注入一份：旧版还有一个更弱的 STEALTH_SCRIPT，两份一起会重复设置 navigator.webdriver
            # （undefined / false 冲突），已合并进 ANTI_DEBUG_SCRIPT。
            await page.add_init_script(self.ANTI_DEBUG_SCRIPT)

            # 1) 搜索列表页（若刚过完验证码，先补足冷却窗口再导航）
            await self._respect_captcha_cooldown("打开搜索页")
            await page.goto(self.SEARCH_URL.format(kw=quote_plus(clean_name)),
                            wait_until="domcontentloaded", timeout=35000)
            self.last_page_url = page.url
            if not await self.check_and_wait_captcha(page, timeout=captcha_timeout):
                data["blocked"] = True
                data["last_error"] = self.last_error
                return data
            if await self._detect_antibot_page(page, captcha_timeout=captcha_timeout):
                data["blocked"] = True
                data["last_error"] = self.last_error
                return data

            # 2) 等结果卡片真正渲染（实测需 6~10s，早读只会拿到空壳）。
            #    同时识别“确实查不到”的提示，避免不存在的企业白等满超时。
            try:
                await page.wait_for_function(
                    r"""(sel) => document.querySelector(sel)
                        || /暂无数据|没有找到|未找到相关|换个词试试|无相关结果/.test(
                               document.body ? document.body.innerText : '')""",
                    arg=self.CARD_SELECTOR,
                    timeout=render_timeout * 1000,
                )
            except Exception:
                self.last_error = "no_result_cards"
                data["last_error"] = self.last_error
                print(f"      ⚠️ [爱企查] {render_timeout:.0f}s 内未渲染出结果卡片: {clean_name}")
                return data

            if not await page.locator(self.CARD_SELECTOR).count():
                self.last_error = "company_not_found"
                data["last_error"] = self.last_error
                print(f"      ℹ️ [爱企查] 站内查无此企业: {clean_name}")
                return data

            # 结果页上先"看一眼再选"：滚动 + 停顿（而不是导航完立刻抽 DOM）
            await self._human_browse(page)

            card = await self._pick_card(page, clean_name)
            if not card or not card.get("pid"):
                self.last_error = "card_without_pid"
                data["last_error"] = self.last_error
                print(f"      ⚠️ [爱企查] 未定位到企业卡片/pid: {clean_name}")
                return data

            data["pid"] = card["pid"]
            data["matched"] = bool(card.get("matched"))
            data["matched_name"] = card.get("name", "")
            # 卡片本身已带法定代表人/注册资本/电话/邮箱，作为可靠兜底
            data.update(self._extract_all(card.get("text", ""), self.CARD_FIELDS))

            # 进入企业详情前的随机停顿（3~5 秒）：模拟真人"看完结果再点进去"的节奏。
            # 放在选卡之后、导航之前 —— 上一家详情页 → 本次搜索 → 停顿 → 进详情，
            # 让相邻两次详情访问之间的间隔不再均匀。
            delay = round(random.uniform(*self.DETAIL_ENTER_DELAY), 2)
            print(f"      ⏱️ [爱企查] 进入企业详情前随机停顿 {delay} 秒...")
            await asyncio.sleep(delay)

            # 3) 详情页（用 pid 直达；卡片 <a> 没有 href，无法走链接）
            #    导航前先补足过码冷却 —— 详情页是"过码后立刻再弹码"的最高发位置
            await self._respect_captcha_cooldown("进入企业详情页")
            detail_url = self.DETAIL_URL.format(pid=card["pid"])
            await page.goto(detail_url, wait_until="domcontentloaded", timeout=35000)
            self.last_page_url = page.url
            if not await self.check_and_wait_captcha(page, timeout=captcha_timeout):
                data["blocked"] = True
                data["last_error"] = self.last_error
                return data
            if await self._detect_antibot_page(page, captcha_timeout=captcha_timeout):
                data["blocked"] = True
                data["last_error"] = self.last_error
                return data

            # 等「工商信息表格」真正渲染出来。注意不能用「注册资本」当判断信号：
            # 页头在表格之前就出现该字样，会导致表格尚未渲染就开始读取，
            # 从而丢掉参保人数 / 经营状态 / 实缴资本（实测第 3 家企业就是这样）。
            try:
                await page.wait_for_function(
                    "() => /参保人数|统一社会信用代码|实缴资本|成立日期/.test(document.body.innerText)",
                    timeout=render_timeout * 1000,
                )
            except Exception:
                self.last_error = "detail_not_rendered"
            # 滚动一下再读：既拟人，也能把懒加载的工商信息表格催出来
            await self._human_browse(page)

            detail_fields = await page.evaluate(self.DETAIL_EXTRACT_JS)
            # 二次补读：表格为懒渲染，若表格字段仍为空，再等一拍重读一次
            if not any((detail_fields or {}).get(k) for k in ("paid_capital", "insured_users", "status")):
                await self._human_delay(1.5, 2.0)
                retry = await page.evaluate(self.DETAIL_EXTRACT_JS) or {}
                for k, v in retry.items():
                    if v and not (detail_fields or {}).get(k):
                        detail_fields[k] = v

            # 卡片值优先（详情页的“法定代表人”会渲染成头像首字，容易取到单字）
            for k, v in (detail_fields or {}).items():
                if not v:
                    continue
                if k == "legal_person" and data.get("legal_person"):
                    continue
                if len(v) <= 1 and k in ("legal_person", "status"):
                    continue
                data[k] = v

        except Exception as e:
            self.last_error = f"{type(e).__name__}:{str(e)[:80]}"
            print(f"      ⚠️ [爱企查提取异常] {clean_name}: {e}")

        data["blocked"] = self.blocked
        data["last_error"] = self.last_error
        if not any(data.get(k) for k in ("legal_person", "reg_capital", "phone", "reg_address")):
            print(f"      ⚠️ [爱企查] 未取到有效字段: {clean_name}（last_error={self.last_error or 'empty'}）")
        return data

    # 兼容旧调用名
    async def enrich(self, page: Page, company_name: str) -> dict:
        return await self.search_and_enrich(page, company_name)
