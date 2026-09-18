import asyncio
import contextlib
import random
from dataclasses import dataclass, field, replace
from datetime import datetime
import re

import httpx
import pandas as pd
from playwright.async_api import async_playwright

import config
from core.browser import BrowserProfile, ensure_chrome_running
from core.factory import CrawlerFactory
import crawlers
from enrichers.aiqicha import AiQiChaEnricher
from enrichers.evaluator import EvaluatedSupplier, evaluate_supplier_icp
from enrichers.tianyancha import TianyanchaEnricher
from enrichers.website import WebsiteEnricher
from exporters.excel import export_leads_to_excel
from models import RawSupplierLead
from utils.dedup import commit_lead_fingerprints, rollback_fingerprints
from utils.logger import STREAM_TAG, get_logger, stream_context

logger = get_logger("pipeline")


@dataclass
class StreamContext:
    """一条流水线的「流上下文」——双浏览器并行时两条流各持一份。

    存在的意义是让 `run_pipeline` 的**业务逻辑一行不改**：单流时它是 None
    （走与改造前完全相同的代码路径），双流时只在这里注入差异。

    共享的东西（excel_lock）由外壳建好传进来；每流独有的东西（tag / profile）各自一份。
    """

    tag: str                                   # "A" / "B"，用于日志前缀
    profile: BrowserProfile | None = None      # 本流用哪台浏览器
    excel_lock: asyncio.Lock | None = None     # ⚠️ 两条流共享同一把
    do_legacy_recheck: bool = True             # 存量报表复核只让一条流做

    def lock(self):
        """落盘用的锁。单流（无 lock）时返回 nullcontext，行为不变。"""
        return self.excel_lock if self.excel_lock is not None else contextlib.nullcontext()

    @property
    def name(self) -> str:
        return self.profile.name if self.profile else self.tag

# —— 工商补全数据源 ——
# aiqicha    : 爱企查（默认）
# tianyancha : 天眼查（改动前的行为，保留可切回）
# both       : 双源漏斗——先爱企查，关键字段仍缺失的再用天眼查兜底
ENRICH_SOURCES = ("aiqicha", "tianyancha", "both", "hybrid")
ENRICH_SOURCE_LABELS = {
    "aiqicha": "爱企查",
    "tianyancha": "天眼查",
    "both": "双源(爱企查→天眼查兜底)",
    "hybrid": "双源(天眼查/爱企查 按家轮流)",
}

# —— 验证码策略 ——
# auto   : 先尝试打码平台自动识别（需在 .env 配凭据），失败再人工等待
# manual : 不自动识别，直接提示并等待人工滑过（改动前的行为）
# off    : 无人值守——命中验证码立即跳过该家，不等待
CAPTCHA_MODES = ("auto", "manual", "off")
CAPTCHA_MODE_LABELS = {
    "auto": "自动打码（失败转人工）",
    "manual": "仅人工等待",
    "off": "无人值守（跳过不等待）",
}

# 企业之间的拟人化不规则等待（秒）：单账号单 IP 下避免触发风控
ENRICH_INTERVAL_RANGE = (3.5, 6.5)
# 连续多少家判定为"环境异常"时熔断（验证码不通过 / 被反爬拦截 / 请求异常）
ENRICH_FUSE_THRESHOLD = 3
# both 模式下判定"关键字段仍缺失"的口径
AIQICHA_MISSING_KEYS = ("tyc_phone", "registered_capital")
# 属于「正常的查无此企业」，不计入熔断。
# 注意：`no_result_cards` 不在此列——enricher 已能识别「暂无数据/没有找到」并回 company_not_found，
# 若等到超时都没卡片也没提示，说明页面根本没渲染出来（多为被风控拦下），必须计入熔断。
BENIGN_ERRORS = {"company_not_found", "name_too_short"}


def _default_eval(lead: RawSupplierLead) -> EvaluatedSupplier:
    return EvaluatedSupplier(
        clean_company_name=lead.registered_company or "",
        industry="通用制造业",
        is_factory=True,
        confidence_score=0.8,
        summary="未执行大模型质检或降级回退",
    )


def _new_record(lead: RawSupplierLead) -> dict:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "store_url": lead.store_url,
        "company": lead.company,
        "platform": lead.platform,
        "lead": lead.model_dump(),
        "website_enriched": False,
        "enrich": {
            "site_phone": "",
            "tyc_phone": "",
            "email": "",
            "icp": "无",
            "contact_person": "",
            "contact_title": "",
            "registered_capital": "",
            "paid_in_capital": "",
            "insured_count": "",
            "created_at": now_str,
            # —— 工商补全来源与状态（爱企查切换新增）——
            "data_source": "",        # 数据来源：爱企查 / 天眼查（落表）
            "business_status": "",    # 经营状态（落表）
            "customs_code": "",       # 海关注册编码（落表）
            "customs_reg_date": "",   # 海关注册日期（落表）
            # 以下仅存于 dict，暂不落表，便于后续扩展与排查
            "registered_company": "",
            "credit_code": "",
            "setup_date": "",
            "enrich_error": "",
        },
        "llm_evaluated": False,
        "eval": None,
        "tyc_enriched": False,
        "enrich_verdict": "",      # ok / not_found / failed，由 _enrich_business 写入
    }


def _record_key(store_url: str, company: str = "") -> str:
    return (store_url or company or "").strip().rstrip("/")


# --------------------------------------------------------------------------- #
# 「环球资源没爬到中文工商名」的处置
# --------------------------------------------------------------------------- #
# 用户确认的口径：
#   ① 环球资源没爬到中文名 → 同轮内立即重爬一次（crawler 内部已做）；
#   ② 重爬仍无 → 不入库 / 删除；
#   ③ **只认环球资源**——爱企查或大模型补到中文名不算"有"，不阻止删除；
#   ④ 指纹库**候选即写、剔除时回滚**（2026-09-17 用户口径，早上那版是"入库后才写"）：
#      采集端 `dedup.add()` 在候选被接受时就登记（同一轮/后续批次不再重复采到同一家，
#      省掉白跑的详情 + 独立站 + 两家补全）；
#      这里剔除时调 `rollback_fingerprints()` 撤掉 ⇒ 下一轮还能重新采到、再给一次机会。
#      两条合起来 = 「不再白采」+「不永久丢掉」，比只写一端更好。
def _has_chinese(text: str) -> bool:
    return bool(re.search(r'[\u4e00-\u9fa5]', text or ""))


def _rollback_dropped_fingerprints(leads: list) -> int:
    """剔除时撤销指纹，返回撤掉的条数。

    ⚠️ 英文名和中文工商名**两个写法都要撤**：候选阶段写的是英文名，
    详情阶段还会补一条中文名；只撤一个会留下半个黑洞。

    撤不掉的（本来就没写进去）会静默跳过 —— `remove_many` 只删命中的。
    """
    names: list[str] = []
    for ld in leads:
        for attr in ("company", "registered_company"):
            v = str(getattr(ld, attr, "") or "").strip()
            if v:
                names.append(v)
    return rollback_fingerprints(names) if names else 0


# —— 工商字段的"伪值"归一 ——
# 两家 enricher 对"没有数据"的处理**刚好相反**：
#   · 爱企查：占位符归一成空串（`aiqicha.PLACEHOLDER_PREFIXES`）
#   · 天眼查：空值归一成 `未公开` / `-`（`_clean_capital()` 等，等于把"没有"写成了"有"）
# 不统一拦掉会同时踩三个坑：
#   ① 切天眼查后表里填满 `未公开` 这种伪值；
#   ② `_business_result_empty()` 判定"工商字段全空"时会因伪值非空而产生漏判；
#   ③ `_classify_result()` 恒判 ok ⇒ 「站内查无此企业」永远不会出现。
PLACEHOLDER_EXACT = {
    "-", "--", "—", "/", "无", "暂无", "未公开", "未公布", "未披露", "未公示", "无数据",
    "null", "none", "n/a", "na", "登录", "查看", "详情",
}
PLACEHOLDER_PREFIXES = ("暂无", "未公开", "未公布", "未披露", "未公示", "无数据")


def _real_value(value) -> str:
    """取工商字段的真实值：占位符一律当作"没有值"（返回空串）。"""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if s in PLACEHOLDER_EXACT or s.lower() in PLACEHOLDER_EXACT:
        return ""
    if s.startswith(PLACEHOLDER_PREFIXES):
        return ""
    return s


# 落表的「工商类」字段。判定"工商补全有没有拿到东西"只看这几个 ——
# 不含 email 与注册地址：那两个可能来自独立站探测或 GS 详情页，不能算爱企查的产出。
BUSINESS_RESULT_FIELDS = (
    "registered_capital", "paid_in_capital", "insured_count",
    "business_status", "contact_person", "tyc_phone",
)

# 多轮补全（both 模式）取"最保守"的结论：ok > failed > not_found。
# 只有**每一轮都判查无**才认作查无；只要有一轮是环境异常（验证码/被拦）就不删，
# 免得把"没查成"误当成"查不到"而删掉本可以拿到的数据。
_VERDICT_RANK = {"ok": 2, "failed": 1, "not_found": 0}


def _business_result_empty(rec: dict) -> bool:
    """工商类字段是否全空（`未公开`/`-` 这类伪值算空）。"""
    enrich = rec.get("enrich") or {}
    return not any(_real_value(enrich.get(k)) for k in BUSINESS_RESULT_FIELDS)


def _cell_str(value) -> str:
    """Excel 单元格 → 干净字符串。

    **必须显式处理 NaN**：pandas 把空单元格读成 float('nan')，而 `str(nan)` 得到
    字符串 `"nan"` —— 它非空、非 None，`if name:` 判定为"有值"。
    于是所有"公司中文名为空"的历史行都会被误判成有名字而跳过复核，
    整个清理逻辑静默失效（实测踩到，靠单测才发现）。
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none") else s


def _existing_rows_missing_name(output_file: str) -> list[tuple[str, str]]:
    """扫出现有报表里「公司中文名」为空的历史行，返回 [(平台网址, 公司英文名)]。

    **为什么必须单独扫一遍 Excel**：这些公司的指纹早就写进 seen_hashes.txt，
    采集阶段 `dedup.is_seen()` 恒真 → 永远跳过它们，光靠"再跑一次采集"碰不到。
    只能拿着报表里的「平台网址」回锅重抓（crawler.refetch_company_names）。
    """
    import os

    if not output_file or not os.path.exists(output_file):
        return []
    try:
        df = pd.read_excel(output_file, dtype=str)
    except Exception as e:
        logger.warning(f"⚠️ [无中文名复核] 读取现有报表失败，跳过历史行复核: {e!r}")
        return []
    if "公司中文名" not in df.columns or "平台网址" not in df.columns:
        return []

    out = []
    for _, row in df.iterrows():
        url = _cell_str(row.get("平台网址"))
        name = _cell_str(row.get("公司中文名"))
        if name or not url.startswith("http"):
            continue
        # 只复核环球资源的行：其它平台没有这个重抓方法，别乱发请求
        if "globalsources.com" not in url.lower():
            continue
        out.append((url, _cell_str(row.get("公司英文名"))))
    return out


def _backfill_company_names(output_file: str, url_to_name: dict) -> int:
    """把复核补回的中文名写回现有报表（按「平台网址」匹配，**只填空不覆盖**）。

    必须在导出前改：导出走的是"读旧表 + 拼新表、旧行优先"，
    这里不回填的话，补回的名字会在合并时被旧行的空值顶掉。
    """
    import openpyxl

    if not url_to_name:
        return 0
    try:
        wb = openpyxl.load_workbook(output_file)
    except Exception as e:
        logger.warning(f"⚠️ [无中文名复核] 打开报表失败，回填跳过: {e!r}")
        return 0

    ws = wb.active
    headers = [c.value for c in ws[1]]
    try:
        i_url, i_name = headers.index("平台网址"), headers.index("公司中文名")
    except ValueError:
        return 0

    filled = 0
    for row in ws.iter_rows(min_row=2):
        url = str(row[i_url].value or "").strip()
        name = url_to_name.get(url)
        if name and not str(row[i_name].value or "").strip():
            row[i_name].value = name
            row[i_name].number_format = "@"
            filled += 1
    if not filled:
        return 0
    try:
        wb.save(output_file)
    except PermissionError:
        logger.error("❌ [无中文名复核] 报表被占用（大概率正被 Excel 打开），中文名回填未保存。")
        return 0
    return filled


async def _recheck_legacy_missing_name(crawler, output_file: str) -> set[str]:
    """存量报表里「无中文名」的行：按平台网址回锅重抓一次。

    - 补回的 → 写回报表的「公司中文名」列；
    - 仍取不到的 → 返回其「平台网址」，交由导出阶段删除。
    """
    if not hasattr(crawler, "refetch_company_names"):
        return set()
    legacy = _existing_rows_missing_name(output_file)
    if not legacy:
        return set()

    logger.info(f"\n🔁 [无中文名复核] 存量报表有 {len(legacy)} 行缺中文工商名"
                f"（指纹已在库，正常采集不会再碰到）→ 按平台网址回锅重抓一次")
    url_to_name = await crawler.refetch_company_names([u for u, _ in legacy])

    recovered = {u: n for u, n in url_to_name.items() if n}
    still_missing = [u for u, _ in legacy if not url_to_name.get(u)]

    if recovered:
        filled = _backfill_company_names(output_file, recovered)
        logger.info(f"      ✅ 重爬补回 {filled} 行中文名，已写回报表的「公司中文名」列")
    if still_missing:
        name_by_url = dict(legacy)
        logger.info(f"      🗑️ 重爬后仍无中文名的 {len(still_missing)} 行将被删除：")
        for u in still_missing:
            logger.info(f"         ✗ {name_by_url.get(u) or '(无英文名)'} | {u}")
    else:
        logger.info("      ✅ 本次全部补回，无需删除任何行")
    return set(still_missing)


def _search_target(rec: dict) -> tuple[str, str]:
    """确定工商检索用的企业名，返回 (名称, 来源标记)。

    **优先使用环球资源详情页爬到的中文工商全称**（`lead.registered_company`）——
    那是站点给出的真实登记名，命中率最高。
    大模型质检产出的 `clean_company_name` 只是**英文名的翻译**，可能与真实登记名不一致
    （实测出现过按译名去查"站内查无此企业"，而换个名字就能查到），因此仅作兜底。
    """
    lead = RawSupplierLead(**rec["lead"])
    eval_data = rec.get("eval") or {}
    gs_name = (lead.registered_company or "").strip()
    llm_name = (eval_data.get("clean_company_name") or "").strip()
    if gs_name:
        return gs_name, "GS工商名"
    if llm_name:
        return llm_name, "LLM译名"
    return (lead.company or "").strip(), "英文名"


# --------------------------------------------------------------------------- #
# 工商补全：结果映射（纯函数，可离线单测）
# --------------------------------------------------------------------------- #
def _apply_tianyancha(rec: dict, info: dict) -> None:
    """天眼查返回 → rec。

    与改动前的差异只有两处：
      ① 所有取值都过 `_real_value()` —— 天眼查把"没有数据"归一成 `未公开`/`-`，
         直接落表会在表里留下伪值，并让「工商字段全空则不入库」判定失效；
      ② 新增 `business_status`（天眼查叫「登记状态」）映射到「经营状态」列，
         与爱企查的 status 对齐。
    其余映射保持原样。
    """
    if not info:
        info = {}
    enrich = rec["enrich"]
    lead = RawSupplierLead(**rec["lead"])

    phone = _real_value(info.get("phone"))
    if phone:
        enrich["tyc_phone"] = phone
    email = _real_value(info.get("email"))
    if not enrich.get("email") and email:
        enrich["email"] = email
    person = _real_value(info.get("contact_person"))
    if person:
        enrich["contact_person"] = person
        enrich["contact_title"] = info.get("contact_title") or "法定代表人"
    reg_name = _real_value(info.get("registered_company"))
    if reg_name:
        enrich["registered_company"] = reg_name
    addr = _real_value(info.get("registered_address"))
    if not lead.registered_address and addr:
        lead.registered_address = addr
        rec["lead"] = lead.model_dump()

    for src_key, dst_key in (("registered_capital", "registered_capital"),
                             ("paid_in_capital", "paid_in_capital"),
                             ("insured_count", "insured_count"),
                             ("business_status", "business_status"),
                             ("customs_code", "customs_code"),
                             ("customs_reg_date", "customs_reg_date")):
        v = _real_value(info.get(src_key))
        if v:
            enrich[dst_key] = v

    if any(_real_value(info.get(k)) for k in
           ("phone", "email", "contact_person", "registered_capital", "insured_count",
            "business_status", "registered_address")):
        enrich["data_source"] = "天眼查"


def _apply_aiqicha(rec: dict, info: dict) -> None:
    """爱企查返回 → rec。

    字段名与天眼查不同，这里做一次映射：
        legal_person→contact_person / reg_capital→registered_capital /
        paid_capital→paid_in_capital / insured_users→insured_count /
        reg_address→lead.registered_address / status→business_status
    「仅在为空时写入」的字段：email、lead.registered_address、registered_company。
    """
    if not info:
        info = {}
    enrich = rec["enrich"]
    lead = RawSupplierLead(**rec["lead"])

    # 取值一律过 _real_value()：爱企查虽已在 enricher 内归一占位符，这里再兜一层，
    # 保证两家数据源的落表口径完全一致（`未公开`/`-`/`无` 都不算值）。
    if _real_value(info.get("legal_person")):
        enrich["contact_person"] = info["legal_person"]
        enrich["contact_title"] = "法定代表人"
    if _real_value(info.get("phone")):
        enrich["tyc_phone"] = info["phone"]
    if not enrich.get("email") and _real_value(info.get("email")):
        enrich["email"] = info["email"]
    if _real_value(info.get("reg_capital")):
        enrich["registered_capital"] = info["reg_capital"]
    if _real_value(info.get("paid_capital")):
        enrich["paid_in_capital"] = info["paid_capital"]
    if _real_value(info.get("insured_users")):
        enrich["insured_count"] = info["insured_users"]
    if not lead.registered_address and _real_value(info.get("reg_address")):
        lead.registered_address = info["reg_address"]
        rec["lead"] = lead.model_dump()
    if _real_value(info.get("status")):
        enrich["business_status"] = info["status"]
    # 海关信息（进出口信用）：爱企查在「经营状况」tab，天眼查要点「详情」
    if _real_value(info.get("customs_code")):
        enrich["customs_code"] = info["customs_code"]
    if _real_value(info.get("customs_reg_date")):
        enrich["customs_reg_date"] = info["customs_reg_date"]
    # 爱企查卡片上的企业名就是工商登记名；仅当原线索没有中文全称时才采用，
    # 避免覆盖 LLM 质检（clean_company_name）与已有数据
    if not lead.registered_company and info.get("matched_name"):
        enrich["registered_company"] = info["matched_name"]

    # 只存不落表的辅助字段
    for src_key, dst_key in (("credit_code", "credit_code"), ("setup_date", "setup_date"),
                             ("last_error", "enrich_error")):
        if info.get(src_key):
            enrich[dst_key] = info[src_key]

    if any(_real_value(info.get(k)) for k in
           ("legal_person", "phone", "reg_capital", "reg_address", "insured_users",
            "paid_capital", "status")):
        enrich["data_source"] = "爱企查"


def _missing_business_keys(rec: dict) -> bool:
    """both 模式：关键字段是否仍缺失（缺电话或缺注册资本即视为缺失）。"""
    enrich = rec.get("enrich") or {}
    return not (enrich.get("tyc_phone") and enrich.get("registered_capital"))


def _classify_result(info: dict, raised: bool) -> str:
    """把一次补全结果归类：ok / not_found / failed。

    只有 ok / not_found 会重置熔断计数，failed 才计入；查无此企业属于正常结果，
    不该拖垮整批。判定顺序刻意如此：先看是否有数据，再看是否"正常的查无此企业"，
    最后才把其它错误（含渲染超时）算作环境异常。
    """
    if raised:
        return "failed"
    info = info or {}
    err = str(info.get("last_error") or "")
    if info.get("blocked") or err.startswith("captcha_"):
        return "failed"
    # 判据必须过 `_real_value()`：天眼查把"没有数据"写成 `未公开`/`-`，
    # 直接用 `info.get(k)` 判真假会恒为真 → 永远返回 ok → 「站内查无此企业」再也不出现，
    # 「工商全空则不入库」的门槛也跟着失效（两个坑是同一个根因）。
    if any(_real_value(info.get(k)) for k in (
            "legal_person", "phone", "reg_capital", "reg_address", "insured_users",
            "contact_person", "registered_capital", "paid_capital", "insured_count",
            "registered_address", "status", "business_status")):
        return "ok"
    if err in BENIGN_ERRORS:
        return "not_found"
    if err:
        return "failed"
    return "not_found"


def _split_alternating(items: list) -> tuple[list, list]:
    """按家**交替**把列表分成两份：偶数下标 → A，奇数下标 → B。

    30 家 → (15, 15)；27 家 → (14, 13)。用交替而不是切片，是为了避免
    "GS 列表前排的商户更活跃"导致两家拿到的样本不等价。
    """
    return list(items[0::2]), list(items[1::2])


def _passes_remaining(passes: list, current_name: str) -> bool:
    """当前趟之后，`passes` 里是否还有**别的数据源**没跑。

    用途：熔断时判断"该不该彻底停"。hybrid 两趟（天眼查 / 爱企查）是**互相独立**的
    数据源，一趟被反爬拦下不该连坐另一趟 —— 详见 `_enrich_business` 里的踩坑注释。
    """
    names = [n for n, _ in passes]
    if current_name not in names:
        return False
    return any(n != current_name for n in names[names.index(current_name) + 1:])


async def _enrich_business(
    needing: list,
    source: str,
    *,
    page,
    interval: tuple = ENRICH_INTERVAL_RANGE,
    fuse: int = ENRICH_FUSE_THRESHOLD,
    captcha_mode: str = "auto",
    captcha_provider: str | None = None,
    captcha_wait: float | None = None,
    priority_source: str | None = None,
) -> dict:
    """按数据源逐家补全工商数据（节流 + 熔断）。

    - source="aiqicha"    ：只跑爱企查
    - source="tianyancha" ：只跑天眼查（改动前行为）
    - source="both"       ：先爱企查，关键字段仍缺失的再用天眼查兜底
    熔断：连续 `fuse` 家环境异常（验证码超时/被拦/请求异常）→ 中止剩余补全，
    但只记录异常、不抛异常，让已采数据继续落盘。

    priority_source：hybrid 模式下**先跑哪一家**（"tianyancha" / "aiqicha"）。
        双流并行时两条流要**用不同顺序**，否则它们会几乎同时进入爱企查、
        **同时弹验证码**（实测 2026-09-18：两个浏览器窗口一起弹图形验证码，
        人工过码时两个窗口互相抢焦点）。错开后任一时刻只有一条流在爱企查。

    验证码口径（captcha_mode，仅对爱企查生效）：
        auto   —— 先用打码平台自动识别（需 .env 配置），失败再人工等待 captcha_wait 秒
        manual —— 直接人工等待 captcha_wait 秒
        off    —— 无人值守：命中验证码立即跳过该家（不计入人工等待）
    """
    stats = {"source": source, "total": len(needing), "ok": 0, "not_found": 0,
             "failed": 0, "aborted": False, "skipped": 0}

    passes: list = []
    if source == "tianyancha":
        passes.append(("tianyancha", needing))
    elif source == "both":
        passes.append(("aiqicha", needing))
        passes.append(("tianyancha", None))  # None = 动态取“仍缺失”的子集
    elif source == "hybrid":
        # 按家**交替**分派：第 1 家→天眼查，第 2 家→爱企查，第 3 家→天眼查…
        # 为什么交替而不是"前后各半"：GS 列表页前排的商户通常更活跃，前后切开会让
        # 两家拿到的样本不等价；交替则最均衡。
        # 两家**串行**（同一浏览器内，天眼查命中验证码时要 bring_to_front 抢焦点）。
        #
        # 顺序由 `priority_source` 决定：两条流用相反顺序，避免同时挤在爱企查上。
        tyc_recs, aiqc_recs = _split_alternating(needing)
        aiqicha_first = (priority_source == "aiqicha")
        if aiqicha_first:
            passes.append(("aiqicha", aiqc_recs))
            passes.append(("tianyancha", tyc_recs))
        else:
            passes.append(("tianyancha", tyc_recs))
            passes.append(("aiqicha", aiqc_recs))
        logger.info(f"      🔀 [分流] 本批 {len(needing)} 家按家交替分配："
                    f"天眼查 {len(tyc_recs)} 家 / 爱企查 {len(aiqc_recs)} 家（串行执行）"
                    f"｜本流顺序：{'爱企查 → 天眼查' if aiqicha_first else '天眼查 → 爱企查'}"
                    f"{'（与另一条流相反，避免同时弹验证码）' if priority_source else ''}")
    else:  # 默认 aiqicha
        passes.append(("aiqicha", needing))

    for name, recs in passes:
        # ⚠️ 这里**绝不能读 `stats["aborted"]` 来决定要不要跑下一趟**：
        #    hybrid 模式是「两趟串行」——爱企查趟熔断了，天眼查那趟**仍应照跑**
        #    （两源独立，一个被反爬拦下不构成另一个也挂了的理由）。
        #    实测踩坑（2026-09-18）：B 流批 5 爱企查连撞 3 次 ERR_EMPTY_RESPONSE 熔断，
        #    旧的 `break` 直接把天眼查那 12 家也跳过了 → 24 家里 18 家工商字段全空入库。
        #    所以熔断只中止**当前趟**（本趟局部的 `abort_this_pass`），剩下的趟继续跑，
        #    各趟熔断计数独立。`stats["aborted"]` 只是**全局**标记（供上层提示"本批有趟熔断了"）。
        #    ⚠️ 第一版修复写成 `if stats["aborted"] and not _passes_remaining(...)`：逻辑反了，
        #       上一趟置 True 后下一趟照旧被拦 —— 被测试 U3 当场顶回，别再犯。
        abort_this_pass = False

        if recs is not None and not recs:
            logger.info(f"      ↩️ [分流] {ENRICH_SOURCE_LABELS.get(name, name)} 本次没有分到商户，跳过")
            continue

        if recs is None:
            recs = [r for r in needing if _missing_business_keys(r)]
            if not recs:
                logger.info("      ✅ [双源] 爱企查已覆盖关键字段，无需天眼查兜底")
                continue
            logger.info(f"      🔻 [双源] 仍有 {len(recs)} 家关键字段缺失，转天眼查兜底")

        if name == "aiqicha":
            enricher = AiQiChaEnricher(captcha_mode=captcha_mode, captcha_provider=captcha_provider)
        else:
            enricher = TianyanchaEnricher(
                captcha_mode=captcha_mode,
                captcha_provider=captcha_provider,
                captcha_wait=captcha_wait,
            )
        apply_fn = _apply_aiqicha if name == "aiqicha" else _apply_tianyancha
        label = ENRICH_SOURCE_LABELS.get(name, name)
        consecutive_failed = 0

        for idx, rec in enumerate(recs, 1):
            target, name_origin = _search_target(rec)

            info, raised = {}, False
            try:
                if name == "aiqicha":
                    info = await enricher.search_and_enrich(
                        page, target, captcha_timeout=captcha_wait
                    )
                else:
                    info = await enricher.search_and_enrich(page, target)
            except Exception as e:
                raised = True
                logger.warning(f"      ⚠️ [{label} {idx}/{len(recs)}] 补全异常: {target} ({e!r})")

            apply_fn(rec, info)
            rec["tyc_enriched"] = True

            verdict = _classify_result(info, raised)
            stats[verdict] += 1
            # 把判定结果留在 rec 上，供上游决定「工商全空的行走不进库」。
            # 多轮取最保守结论（见 _VERDICT_RANK），避免把"没查成"当成"查不到"。
            if _VERDICT_RANK.get(verdict, 0) > _VERDICT_RANK.get(rec.get("enrich_verdict"), -1):
                rec["enrich_verdict"] = verdict
            if verdict == "failed":
                consecutive_failed += 1
                logger.warning(f"      ⚠️ [{label} {idx}/{len(recs)}] 未取到数据: {target}"
                               f"（原因 {info.get('last_error') or '异常'}）")
            else:
                consecutive_failed = 0
                logger.info(f"      🏢 [{label} {idx}/{len(recs)}] {target}（{name_origin}） → "
                            f"{'已补全' if verdict == 'ok' else '站内查无此企业'}")

            await asyncio.sleep(random.uniform(*interval))

            if consecutive_failed >= fuse:
                stats["aborted"] = True
                abort_this_pass = True
                stats["skipped"] += len(recs) - idx
                logger.error(
                    f"      🛑 [熔断] 连续 {consecutive_failed} 家{label}环境异常（验证码/反爬/请求失败），"
                    f"已中止本趟剩余 {len(recs) - idx} 家补全。"
                    + (f"\n         本趟中止**不影响**另一数据源："
                       f"{_passes_remaining(passes, name) and '后面还有一趟会继续跑' or '已是最后一趟'}。"
                       if len(passes) > 1 else "")
                    + f"\n         处置建议：在对应的 Chrome 窗口中人工过验证码后重跑，"
                      f"或改用其它浏览器环境（--cdp-url）。已采集的数据仍会正常落盘。"
                )
                break

        if abort_this_pass:
            logger.warning(
                f"      ⏭️ [{label}] 本趟熔断中止；"
                f"未处理的家**未执行补全**，将按原策略保留入库（不算「查不到」）。"
            )

    return stats


# —— 浏览器"中途死掉"的判据 ——
# 触发场景（实测 2026-09-18）：Chrome 窗口被关 / 进程崩溃 / 被系统回收 → 正在跑的
# `page.goto` 立刻抛 TargetClosedError。异常类型名 + 消息文本双判据，因为 Playwright
# 在不同版本里抛的类名不一致（`TargetClosedError` / `Error`），只认类型会漏。
_BROWSER_DEATH_HINTS = (
    "TargetClosedError",
    "Target page, context or browser has been closed",
    "Browser has been closed",
    "browser has been closed",
    "Target closed",
)


def _looks_like_browser_death(err: BaseException) -> bool:
    """这个异常是不是"浏览器没了"（而不是页面结构变了/网络抖动之类的业务失败）。"""
    if type(err).__name__ in ("TargetClosedError", "BrowserClosedError"):
        return True
    msg = str(err)
    return any(h in msg for h in _BROWSER_DEATH_HINTS)


async def _scrape_with_selfheal(crawler, *, keyword: str, max_count: int, batch_no: int):
    """采集这一批；若因**浏览器中途死掉**失败，重拉本流浏览器后重试一次。

    为什么需要它（补的是哪一段）：批次边界本来就会 `ensure_chrome_running`（浏览器没了
    会自动重拉），所以"上一批结束之后才死"已能自愈。真正没兜住的是「**采集中途**死掉」——
    那时异常直接冒出去，`run_pipeline` 没有批次级 try，异常一路到 `run_dual_pipeline`
    的 `gather`，结果是**这条流整条结束**：剩下的批次全不跑，而日志上只有一行流结束汇总。

    重拉是安全的：多流模式下 `reuse_nearby=False` + 端口归属校验，决定了
    `ensure_chrome_running` 只会拉起**自己的**浏览器，绝不会去抢另一条流的那台
    （这正是上一次事故的根因，已单独修掉）。

    ⚠️ 一个消除不掉的副作用，必须说清楚：重试会**重扫一次列表页**，
    已被写到指纹库的候选会被 `dedup` 挡住（不会重复入库），但"**崩溃前已写指纹、
    还没返回给上游的那些家**"本批就捞不回来了 —— 它们的指纹还在，所以下一轮也不会再采到。
    要彻底避免只能靠缩小批次（--batch-size 调小 → 单批暴露面更小）。
    """
    try:
        return await crawler.scrape(keyword=keyword, max_count=max_count)
    except Exception as e:
        if not _looks_like_browser_death(e):
            # 不是浏览器的问题（页面结构变了、解析异常…）→ 重试没有意义，原样抛出。
            raise
        logger.error(
            f"\n🚨 [自愈] 第 {batch_no} 批采集中途**浏览器被关闭**：{type(e).__name__}\n"
            f"   现象：该流自己的 Chrome 已消失（窗口被关 / 崩溃 / 被系统回收）。\n"
            f"   处置：自动重新拉起本流浏览器（端口 {getattr(crawler, 'cdp_port', '?')}、"
            f"profile {getattr(crawler, 'profile_dir', '?')}）并重试本批一次。\n"
            f"   注意：重试会重扫一遍列表页；崩溃前已写指纹、尚未返回的候选本批捞不回来。"
        )
        # 重拉（多流下只会重拉自己的；若端口已归属别的流会抛错 —— 那是有意为之，
        # 宁可中止这条流也不静默共用另一条流的浏览器）。返回值会写回 crawler.cdp_port。
        crawler.ensure_chrome_running()
        logger.info(f"   ♻️ [自愈] 浏览器已就绪（端口 {getattr(crawler, 'cdp_port', '?')}），"
                    f"重试第 {batch_no} 批采集…")
        return await crawler.scrape(keyword=keyword, max_count=max_count)


async def _prepare_batch(
    crawler,
    *,
    keyword: str,
    scrape_count: int,
    enrich_websites: bool,
    drop_missing_name: bool,
    batch_no: int,
) -> dict:
    """**轻活**：采集 → 无中文名剔除 → 独立站探测 → LLM 质检。返回 `records`。

    这一段之所以能单独切出来做「预取」：它几乎不占用补全那条链路 ——
    GS 采集走**自己新建的标签页**（`crawler.scrape`），独立站是 httpx，LLM 是外部 API。
    而两家工商补全（重活）主要时间花在浏览器里的等待（每家族 3~6s 冷却 +
    每 17~20 家 45~60s 大休眠 + 验证码），那期间浏览器基本是**空转**的，
    正好用来把下一批的轻活跑掉 —— 这就是「异步预取」的全部收益来源。

    `scrape_count` 是**本批的采集上限**，不一定是 `--batch-size`：
    末批只差几家时上游会传差额（避免整批采满导致超采，实测目标 60 结果入了 84）。
    """
    # 走自愈包装：**采集中途浏览器被关掉**时会重拉本流浏览器并重试一次，
    # 而不是让异常冒出去把这条流整条结束掉（见 _scrape_with_selfheal）。
    # 本函数其余部分（独立站 httpx / LLM）不碰浏览器，所以自愈点只需要包这一句。
    fresh_leads: list[RawSupplierLead] = await _scrape_with_selfheal(
        crawler, keyword=keyword, max_count=scrape_count, batch_no=batch_no,
    )

    # 1.5 「环球资源没爬到中文名 → 重爬一次 → 仍无则剔除」
    # crawler 内部已经同轮重试过一次（含冷却），到这里仍没有中文名的就是确认取不到的。
    #
    # ⚠️ 判定必须是「**含中文**」而不是「非空」——实测踩到：
    # 香港/离岸主体的 GS「Registered Company Name」是纯英文
    # （`SHENZHEN XINLIKE SILICONE PRODUCT CO., LIMITED`），
    # 按"非空"判定会放它过关；随后 `_search_target()` 返回这个英文名，
    # `needing` 的 has_chinese 过滤判假 → **整家被跳过工商补全** →
    # 表里留下一条「中文名有（LLM 译名回填）、工商字段全空」的废行。
    if drop_missing_name:
        kept, dropped = [], []
        for ld in fresh_leads:
            name = (ld.registered_company or "").strip()
            (kept if _has_chinese(name) else dropped).append(ld)
        if dropped:
            rolled = _rollback_dropped_fingerprints(dropped)
            logger.info(f"\n🧹 [入库前过滤] {len(dropped)} 家没有中文工商名，本次不入库"
                        f"；已撤销其指纹 {rolled} 条 ⇒ 下一轮还会重新采到、再试一次"
                        f"（想留着这些行入库请加 --keep-missing-name）")
            for ld in dropped:
                raw = (ld.registered_company or "").strip()
                why = f"GS 只给了英文名 {raw!r}（多为香港/离岸主体）" if raw else "GS 未爬到工商名"
                logger.info(f"      ✗ 剔除: {ld.company} | {ld.store_url} —— {why}")
        fresh_leads = kept

    if not fresh_leads:
        # 不在这里 return：存量报表里那些「无中文名」的历史行，恰恰要靠下面第 5 步
        # （按平台网址回锅重抓）才有机会被清理。新采为空时仍应跑完这一步。
        logger.info("💡 本次没有可入库的新商户（无中文名的已被剔除），仅执行存量复核。")

    records: dict = {}
    for lead in fresh_leads:
        key = _record_key(lead.store_url, lead.company)
        if key not in records:
            records[key] = _new_record(lead)

    # 2. 独立站穿透探测
    if enrich_websites:
        pending = list(records.values())
        logger.info(f"\n🌐 [Enrichment 1/2] 异步穿透独立站探测商业邮箱、官网联系方式与备案... (本次处理 {len(pending)} 家)")
        enricher = WebsiteEnricher(concurrency=5)
        async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=10.0) as client:

            async def enrich_one(rec: dict):
                lead = RawSupplierLead(**rec["lead"])
                try:
                    s_res = await enricher.enrich_lead(client, lead.official_website)
                except Exception as e:
                    logger.warning(f"      ⚠️ [独立站探测异常] {lead.company} ({e!r})")
                    s_res = {"email": "", "site_phone": "", "icp": "网址打不开"}
                rec["enrich"]["email"] = s_res.get("email", "")
                rec["enrich"]["site_phone"] = s_res.get("site_phone", "")
                rec["enrich"]["icp"] = s_res.get("icp", "无")
                rec["website_enriched"] = True

            await asyncio.gather(*[enrich_one(rec) for rec in pending])

    # 3. 大模型工商质检与行业归纳
    eval_pending = list(records.values())
    logger.info(f"\n🧠 [LLM 质检] 调用 DeepSeek 模型规范工商全称与主营行业归纳... (本次处理 {len(eval_pending)} 家)")
    for idx, rec in enumerate(eval_pending, 1):
        lead = RawSupplierLead(**rec["lead"])
        try:
            eval_res = await evaluate_supplier_icp(
                lead=lead,
                api_key=config.OPENAI_API_KEY,
                base_url=config.OPENAI_BASE_URL,
                model=config.MODEL_NAME,
            )
        except Exception as e:
            logger.warning(f"      ⚠️ [{idx}/{len(eval_pending)}] 质检跳过异常: {lead.company} ({e})")
            eval_res = _default_eval(lead)
        display_name = eval_res.clean_company_name or "无官方中文名/离岸主体"
        logger.info(f"      ✨ [{idx}/{len(eval_pending)}] 质检结果: {lead.company} -> {display_name} | 行业: 【{eval_res.industry}】")
        rec["eval"] = eval_res.model_dump()
        rec["llm_evaluated"] = True

    return records


async def _finish_batch(
    crawler,
    records: dict,
    *,
    keyword: str,
    output_file: str,
    enrich_tianyancha: bool,
    source: str,
    mode: str,
    captcha_provider: str | None,
    captcha_wait: float | None,
    drop_missing_name: bool,
    batch_no: int,
    stream_ctx: StreamContext | None = None,
) -> int:
    """**重活**：两家工商补全 → 工商全空剔除 → 存量复核 → 落盘 → 兜底写指纹。

    返回本批**真正新增**的入库家数（由导出器回填，不是 `len(leads_data)`）。

    慢在浏览器等待上（每家族 3~6s 冷却 / 每 17~20 家 45~60s 大休眠 / 验证码），
    所以调用方会在这段时间里**并行预取下一批的轻活**（见 `_prepare_batch`）。
    """
    # 4. 工商数据补全（默认爱企查；可切天眼查 / 双源）
    if enrich_tianyancha:
        needing = []
        origin_count: dict = {}
        for rec in records.values():
            target, origin = _search_target(rec)
            has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', target))
            if has_chinese:
                needing.append(rec)
                origin_count[origin] = origin_count.get(origin, 0) + 1

        if not needing:
            logger.info("\n🏢 [Enrichment 2/2] 商户无大陆主体，安全跳过工商补全。")
        else:
            source_label = ENRICH_SOURCE_LABELS[source]
            logger.info(f"\n🏢 [Enrichment 2/2] 启动{source_label}检索 "
                        f"(共 {len(needing)} 家企业补全资本、人数、触点与经营状态)...")
            logger.info(f"      🔤 检索名来源: {origin_count}"
                        f"（优先用环球资源爬到的中文工商名；GS 缺失时才回退 LLM 译名）")
            if origin_count.get("LLM译名"):
                logger.warning(f"      ⚠️ 有 {origin_count['LLM译名']} 家没有 GS 中文工商名，"
                               f"只能用大模型译名去查，命中率可能偏低")
            # ⚠️ 必须把 profile_dir / reuse_nearby 一起传下去。
            # 原来只传了 port，于是这里永远退回默认目录 `./chrome_debug_profile`：
            # 双浏览器时浏览器 B 的补全阶段会拿错 profile（甚至把 A 的浏览器拉起来），
            # 表现为「B 用了 A 的登录态」且**不报任何错**。
            #
            # owner=crawler.stream_tag：端口归属校验。若这条流的浏览器已死、回退时
            # 抓到另一条流的端口，这里会**直接抛错中止该流**，而不是让两条流悄悄共用一台。
            _before_port = crawler.cdp_port
            # ⚠️⚠️ 必须**接住返回值并回写** `crawler.cdp_port`。
            # `ensure_chrome_running` 返回的是**实际使用的端口**，它可能在两种情况下不同于入参：
            #   ① 该流的浏览器已死，重新拉起时因原端口被别的程序占着而改用了别的端口；
            #   ② 原端口被非 DevTools 进程占用 → find_available_port 另找一个。
            # 不回写的后果是下面 `connect_over_cdp` 仍然去连**旧端口**（那儿已经没有浏览器了），
            # 补全整批失败 —— 而且 `if crawler.cdp_port != _before_port` 恒为 False，
            # 连"端口变了"这行唯一的现场信号都不会打印（这里曾经就是死代码）。
            crawler.cdp_port = ensure_chrome_running(
                port=crawler.cdp_port, platform_name=source_label,
                profile_dir=crawler.profile_dir,
                proxy=crawler.proxy,
                reuse_nearby=crawler.reuse_nearby,
                owner=getattr(crawler, "stream_tag", None) or None,
            )
            if crawler.cdp_port != _before_port:
                logger.error(
                    f"🚨 [补全阶段] 调试端口由 {_before_port} 变为 {crawler.cdp_port}："
                    f"该流自己的浏览器已经没了（窗口被关 / 崩溃 / 端口被抢），"
                    f"已在 {crawler.cdp_port} 上重新拉起并继续。"
                    f"若这行**不是你预期的**，请检查 Chrome 崩溃记录或内存占用。"
                )

            async with async_playwright() as p:
                browser = None
                try:
                    browser = await p.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{crawler.cdp_port}", timeout=30000
                    )
                except Exception as e:
                    logger.error(
                        f"❌ [Enrichment 2/2] 无法附着 CDP 浏览器（端口 {crawler.cdp_port}）: {type(e).__name__}\n"
                        f"   最常见原因：该浏览器里存在“无响应/卡死”的标签页——"
                        f"Playwright 必须初始化所有已存在页面，任何一个卡死都会导致附着超时。\n"
                        f"   处置：在该 Chrome 窗口里关闭无响应的标签页后重跑（其余已采集数据仍会落盘）。"
                    )

                if browser is not None:
                    context = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = await context.new_page()
                    try:
                        stats = await _enrich_business(
                            needing, source, page=page,
                            captcha_mode=mode,
                            captcha_provider=captcha_provider,
                            captcha_wait=captcha_wait,
                            # 双流用相反的补全顺序，避免两条流同时挤在爱企查上弹验证码
                            priority_source=(stream_ctx.profile.enrich_priority
                                             if stream_ctx and stream_ctx.profile else None),
                        )
                        logger.info(
                            f"      📊 [工商补全] 完成: 成功 {stats['ok']} | 查无此企业 {stats['not_found']} | "
                            f"失败 {stats['failed']} | 未处理 {stats['skipped']}"
                            f"{'（已熔断）' if stats['aborted'] else ''}"
                        )
                    finally:
                        await page.close()

    # 4.5 工商补全后工商类字段**全空**的行 → 不入库
    # 用户口径：在工商库里查不到信息的商户没有价值，不进报表。
    if drop_missing_name and records:
        kept, dropped, unattempted = {}, [], []
        for key, rec in records.items():
            if not rec.get("tyc_enriched"):
                # 从没查过（熔断跳过 / --no-enrich）—— 这不是"查不到"，是"没查成"，
                # 删掉等于把环境问题算成数据问题，故保留并提示。
                unattempted.append(rec)
                kept[key] = rec
                continue
            if _business_result_empty(rec) and rec.get("enrich_verdict") != "failed":
                dropped.append(rec)
            else:
                kept[key] = rec
        if dropped:
            rolled = _rollback_dropped_fingerprints([RawSupplierLead(**r["lead"]) for r in dropped])
            logger.info(f"\n🧹 [入库前过滤] {len(dropped)} 家在工商库中查不到任何信息"
                        f"（注册资本/实缴/参保/经营状态/联系人/电话 全空），本次不入库"
                        f"；已撤销其指纹 {rolled} 条 ⇒ 下一轮还会重新采到、再试一次")
            for rec in dropped:
                ld = RawSupplierLead(**rec["lead"])
                logger.info(f"      ✗ 剔除: {ld.company} | {ld.store_url}"
                            f" —— 判定 {rec.get('enrich_verdict') or '无数据'}")
        if unattempted:
            logger.warning(f"      ⚠️ 另有 {len(unattempted)} 家**未执行**工商补全（熔断跳过或 --no-enrich），"
                           f"已保留入库（它们不算'查不到'）。要清理请重跑补齐后再判定。")
        records = kept

    # 5. 存量报表里「无中文名」的历史行：重爬一次，仍取不到则删除
    # ⚠️ 只在**第 1 批**做：它是"全量扫报表 + 逐条重抓"，分批跑多次等于把同一批历史行
    #    白抓多遍（清理不掉的会一直留在表里，每批都被重新抓到）。
    # 双浏览器时再收紧一层：**只有主浏览器那条流做**。两条流都做等于把同一批历史行
    # 在两张浏览器里各抓一遍，而结果（drop_urls）还是同一份，纯浪费。
    drop_urls: set[str] = set()
    _may_recheck = (stream_ctx is None or stream_ctx.do_legacy_recheck)
    if drop_missing_name and batch_no == 1 and _may_recheck:
        drop_urls = await _recheck_legacy_missing_name(crawler, output_file)
    elif drop_missing_name and batch_no == 1 and not _may_recheck:
        logger.info("      ℹ️ 存量报表复核已交给另一条流执行，本流跳过（避免同一批历史行被抓两遍）")

    # 6. 落盘报表导出
    leads_data = [RawSupplierLead(**rec["lead"]) for rec in records.values()]
    enriched_results = [rec["enrich"] for rec in records.values()]
    eval_results = [
        EvaluatedSupplier(**rec["eval"]) if rec.get("eval") else _default_eval(RawSupplierLead(**rec["lead"]))
        for rec in records.values()
    ]
    if not leads_data and not drop_urls:
        # 本批没有可入库的新行、也没有存量要删 —— 跳过写表（避免无意义的读+写 xlsx，
        # 也避免报表被 Excel 占用时白白报一次 PermissionError）。
        logger.info("      ⏭️ 本批既无新行入库、也无存量要删，跳过写表")
        return 0

    export_stats: dict = {}
    try:
        # ⚠️ 落盘必须串行化：`export_leads_to_excel` 是**读整表 → 合并 → 去重 → 写回**，
        #    两条流并发调用会产生 read-modify-write 竞态 —— 后写的把先写的那批行整段覆盖掉，
        #    报表**静默丢行**。加锁后同一时刻只有一条流在重写 xlsx。
        #    第二次开双流时才会暴露这个 bug（单流时锁是 nullcontext，行为不变）。
        #
        # 用 to_thread 是因为它是同步的 pandas 读写：直接 await 会堵住事件循环，
        # 让另一条流的补全冷却/验证码计时整体停摆（白白拖慢双流）。
        async with (stream_ctx.lock() if stream_ctx else contextlib.nullcontext()):
            await asyncio.to_thread(
                export_leads_to_excel,
                leads_data=leads_data,
                enriched_results=enriched_results,
                eval_results=eval_results,
                keyword=keyword,
                output_file=output_file,
                drop_urls=drop_urls,
                stats=export_stats,
            )
    except Exception as e:
        # ⚠️ 「候选即写」带来的新风险：指纹已经在采集阶段写下了，但报表**没落盘** ⇒
        #    这批等于"处理过却没进报表"。必须把指纹撤掉，否则下次（以及以后每次）
        #    都会被 `is_seen()` 跳过 —— 数据就白丢了。
        #    最常见的触发场景：报表正被 Excel 打开（`~$` 锁文件）导致写盘失败。
        rolled = _rollback_dropped_fingerprints(leads_data)
        logger.error(
            f"❌ [落盘失败] {type(e).__name__}: {e}\n"
            f"   已回滚本批 {len(leads_data)} 家的指纹（{rolled} 条）⇒ 报表修好后重跑还能采到它们"
        )
        raise

    # 7. 兜底确认指纹库。
    # 主路径已改成「候选即写（采集端 `dedup.add`）+ 剔除时回滚（上面的
    # `_rollback_dropped_fingerprints`）」，所以走到这里时，入库的家**基本都已在库里**，
    # 这一步只作兜底：无论哪条采集路径、中间被谁改过逻辑，
    # "进了报表的公司一定在指纹库里" 这件事都要成立。重复写法因哈希重复，返回 0。
    written = commit_lead_fingerprints(leads_data)
    logger.info(f"🔑 [指纹库] 兜底确认：本批入库的 {len(leads_data)} 家都已在指纹库中"
                f"（本次新增 {written} 条 —— 候选阶段已写过，正常应为 0；"
                f"被剔除的家已在上面回滚）")

    # 返回**真正新增**的家数（导出器回填）；分批循环用它累加进度
    return int(export_stats.get("added", 0))


async def run_pipeline(
    keyword: str = "monitor",
    platform: str = "globalsources",
    max_count: int = 5,
    output_file: str = "suppliers_leads.xlsx",
    enrich_websites: bool = True,
    enrich_tianyancha: bool = True,
    enrich_source: str = "tianyancha",
    resume: bool = True,
    captcha_mode: str = "auto",
    captcha_provider: str | None = None,
    captcha_wait: float | None = None,
    drop_missing_name: bool = True,
    batch_size: int = 30,
    reset_pages: bool = False,
    async_prefetch: bool = True,
    stream_ctx: StreamContext | None = None,
    profile: BrowserProfile | None = None,
):
    """分批采集 → 批内分流两家 → 每批入库，直到**累计入库**达到目标。

    用户口径（2026-09-17）：
        「搜索 120 个，环球资源先搜索 30 个，然后把 15 个分给天眼查、15 个分给爱企查，
          查完入库，然后接着如此，一直到查完为止」
        → `-n 120 --batch-size 30 --enrich-source hybrid`

    ⚠️ `max_count` 是**最终入库的目标家数**，不是 GS 采集数。每批都会剔除
    「无中文工商名」「工商库查空」的家，所以 `采集数 ≠ 入库数`；按采集数计会在剔除率
    高时提前收工、达不到目标。

    ⚠️ 被剔除的家**不进指纹库**（用户口径：只对成功入库的写指纹），所以下一批会把它们
    重新采到、再占一次名额。为此设了「连续 2 批零新增就停」的兜底，避免空转。

    分批的额外好处：每批查完立刻入库 + 写指纹，中断时前面的批次成果不会丢。

    stream_ctx / profile：**双浏览器并行**时才传（见 `run_dual_pipeline`）。
    默认全为 None ⇒ 单流，代码路径与改造前逐字节一致（锁退化为空、无日志前缀、
    浏览器用 config 里的第一个 profile）。
    """
    if profile is None and config.BROWSER_PROFILES:
        # 单流：用第一个 profile（默认仍是 9222 + ./chrome_debug_profile，与历史一致）
        profile = config.BROWSER_PROFILES[0]
    if stream_ctx is None and profile is not None and profile.lane_stride > 1:
        # ⚠️ 车道只在「确实有另一条流在跑」时才成立。
        #    直接调 run_pipeline（单流）却沿用 A 的 lane_stride=2 的话，只会扫第 1、3、5… 页，
        #    **偶数页无人负责 → 漏采**（不会报错，只是采得少，很难发现）。
        #    这里把车道还原成"全页"，让单流语义与改造前完全一致。
        profile = replace(profile, lane_offset=1, lane_stride=1)

    total_count = max(1, int(max_count or 1))
    batch_size = max(1, int(batch_size or 1))
    plan_batches = (total_count + batch_size - 1) // batch_size

    source = (enrich_source or "tianyancha").strip().lower()
    if source not in ENRICH_SOURCES:
        logger.warning(f"⚠️ [Pipeline] 未知数据源 {enrich_source!r}，回退为 tianyancha（可选: {', '.join(ENRICH_SOURCES)}）")
        source = "tianyancha"

    mode = (captcha_mode or "auto").strip().lower()
    if mode not in CAPTCHA_MODES:
        logger.warning(f"⚠️ [Pipeline] 未知验证码模式 {captcha_mode!r}，回退为 auto（可选: {', '.join(CAPTCHA_MODES)}）")
        mode = "auto"

    logger.info("=======================================================")
    logger.info("🚀 [Pipeline] 启动自动化多平台采集流水线（分批模式）")
    logger.info(f"🌐 目标平台: {platform} | 关键词: {keyword}")
    logger.info(f"🎯 采集计划: 最终入库 {total_count} 家｜每批采集 {batch_size} 家｜"
                f"预计 {plan_batches} 批（不足则自动补批）")
    logger.info(f"🧩 工商补全数据源: {ENRICH_SOURCE_LABELS[source]}"
                f"{'（已关闭，--no-enrich）' if not enrich_tianyancha else ''}")
    if source == "hybrid":
        logger.info(f"      🔀 分流口径: 每批按家**交替**分配（第1家→天眼查，第2家→爱企查，…），"
                    f"两家**串行**执行（共用同一个浏览器，并行会互相打断验证码）")
    elif source in ("tianyancha", "aiqicha"):
        # 显式提示：否则"没分流"只体现在上面那一行数据源名里，很容易被忽略
        # （实测踩到：入口脚本忘了传 enrich_source，26 家全给了天眼查没人发现）
        logger.info(f"      ℹ️ 当前是**单数据源、不分流**（全部交给{ENRICH_SOURCE_LABELS[source]}）。"
                    f"要「天眼查 / 爱企查 各分一半」请设 enrich_source='hybrid'"
                    f"（CLI：--enrich-source hybrid）")
    wait_secs = max(0.0, captcha_wait if captcha_wait is not None else config.CAPTCHA_WAIT_SECONDS)
    if source == "tianyancha":
        # 天眼查是**两段式点选**验证码（先点按钮 → 再按箭头提示顺序点图形），
        # 走云码的人工点选接口 type=30009（约 0.025 元/次，比旋转类型贵，且不报错退费）。
        logger.info(f"🔐 验证码策略: {CAPTCHA_MODE_LABELS[mode]}（人工等待上限 {wait_secs:.0f}s）"
                    f"｜天眼查为两段式**点选**，需云码 type={config.YUNMA_POINT_TYPE}；"
                    f"另有每 17~20 家 45~60s 大休眠")
        logger.info(f"   {config.captcha_status()}")
    else:
        logger.info(f"🔐 验证码策略: {CAPTCHA_MODE_LABELS[mode]}（人工等待 {wait_secs:.0f}s） | {config.captcha_status()}")
    logger.info(f"💾 断点续采: {'开' if resume else '关'} | 输出: {output_file}")
    logger.info(f"⚡ 异步预取: {'开（下一批的采集/独立站/LLM 与本批工商补全并行）' if async_prefetch else '关（严格串行）'}")
    logger.info("=======================================================")

    # —— 给本协程打上流标识（双浏览器并行时日志会交错，靠这个区分是哪一台）——
    # ⚠️ 只设不复位：`run_dual_pipeline` 用 asyncio.gather 起两条流，gather 会把每个协程
    #    包成独立 Task，而 Task 运行在**上下文副本**里 —— 在这里 set 不会污染外层，
    #    也不会串到另一条流。单流（stream_ctx=None）时压根不设，输出与改造前一致。
    if stream_ctx is not None:
        stream_context(stream_ctx.tag)

    if profile is not None and len(config.BROWSER_PROFILES) > 1:
        logger.info(f"🖥️ 本流使用浏览器 {profile.name}｜CDP 端口 {profile.port}｜"
                    f"profile {profile.profile_dir}｜"
                    f"{'代理 ' + profile.proxy if profile.proxy else '本机直连'}")

    logger.info(f"🧹 入库门槛: " + (
        "要求「GS 有中文工商名」且「工商库能查到信息」，不满足即剔除"
        if drop_missing_name else "不设门槛，全部入库（--keep-missing-name）"))

    crawler = CrawlerFactory.get_crawler(
        platform,
        cdp_port=profile.port,
        profile_dir=profile.profile_dir,
        proxy=profile.proxy,
        reuse_nearby=profile.reuse_nearby,
        stream_tag=profile.name,
        lane_offset=profile.lane_offset,
        lane_stride=profile.lane_stride,
    )
    if reset_pages and hasattr(crawler, "reset_page_cursor"):
        cleared = crawler.reset_page_cursor()
        logger.info(f"📑 [翻页进度] 已按要求重置（清掉 {cleared} 条）→ 本批从第 1 页重新扫")
    elif plan_batches > 1 and hasattr(crawler, "reset_page_cursor"):
        if profile.lane_stride > 1:
            logger.info(f"📑 本流负责**车道** 第 {profile.lane_offset}、"
                        f"{profile.lane_offset + profile.lane_stride}、… 页（步长 {profile.lane_stride}），"
                        f"与另一条流扫的页**不相交**；批次间承接本车道进度。"
                        f"要重扫本车道请加 --reset-pages")
        else:
            logger.info("📑 翻页进度在批次间**承接**：第 2 批起从上次停下的页继续，不重扫前面的页。"
                        "代价：靠前页上被剔除的家不再重试（要重扫加 --reset-pages）")

    ingested = 0
    batch_no = 0
    empty_streak = 0
    scraped_total = 0   # 各批采集上限之和（用于收尾统计，避免再用 batch_no × batch_size 估算）
    # 兜底上限：正常只需 plan_batches 批，留 3 倍余量应对"剔除太多需要补批"。
    # 没有这个上限，"连续零新增但每批都采到几家又被全剔"会变成无限循环。
    max_batches = plan_batches * 3 + 3

    # 预取：`pending` 存"轻活已备好、还没做工商补全"的下一批
    pending = None                 # (records, scrape_count, batch_no)
    prefetch: asyncio.Task | None = None
    prefetch_take = 0
    prefetch_no = 0

    while ingested < total_count and batch_no < max_batches:
        batch_no += 1
        remaining = total_count - ingested
        # —— 末批按差额收口 ——
        # 实测（2026-09-17）：目标 60，批 1 入 26、批 2 入 28（累计 54），
        # 批 3 明明只差 6 家却整批采了 30 → **最终入库 84，超采 24 家**。
        # 所以当差额小于一个批次时，只采差额。
        # 代价：这批若又被剔除几家，可能还要再开一个小批次补（每个小批次约 3s 翻页 +
        # 十几秒详情）—— 比超采 24 家划算得多。
        take = min(batch_size, remaining)
        last_gap = take < batch_size
        logger.info("")
        logger.info("─" * 62)
        logger.info(f"📦 [批次 {batch_no}/{plan_batches}] 累计入库 {ingested}/{total_count}"
                    f"｜本批采集上限 {take} 家"
                    f"{'（**末批只补差额**，避免超采）' if last_gap else ''}"
                    f"｜还差 {remaining} 家")
        if async_prefetch and pending is None and prefetch is None:
            logger.info("      ⚡ 本批工商补全期间会**并行预取**下一批的「采集 + 独立站 + LLM」"
                        "（各自独立标签页；补全大半时间在冷却等待，浏览器是空转的）")
            logger.info("         ↳ 期间两边的日志会**交错**，属正常现象")
        logger.info("─" * 62)

        # ① 本批 records：优先用上一批补全期间预取好的
        if pending is not None:
            records, prefetched_n, _pn = pending
            pending = None
            logger.info("      ♻️ 使用预取结果 —— 本批无需再等「采集 / 独立站 / LLM」")
            # ⚠️ 这里**不能**把 `take` 换成预取时的数量！
            # 预取时算 `prefetch_take` 用的 `ingested` 是**旧的**（那一刻本批还没入库），
            # 所以它可能比本批实际需要的 `take` 大。不截断的话「末批只补差额」就白做了：
            #   批 4 开始还差 41 家 → 预取 30 家；
            #   批 4 实际只入 26 家，累计 105/120 → 末批只需 15 家，
            #   却把预取来的 30 家全吃下去 → **又超采 15 家**（表头还写着"上限 15 家"）。
            # 多出来的必须退还：它们没进报表，指纹也要撤掉，否则下轮会被 is_seen() 跳过。
            if len(records) > take:
                extra_keys = list(records.keys())[take:]
                extra = [records.pop(k) for k in extra_keys]
                rolled = _rollback_dropped_fingerprints(
                    [RawSupplierLead(**r["lead"]) for r in extra])
                scraped_total -= len(extra)
                logger.info(f"      ✂️ 预取 {prefetched_n} 家 > 本批需要 {take} 家，"
                            f"退还 {len(extra)} 家（已撤销指纹 {rolled} 条 ⇒ 下一轮还能重新采到）")
        else:
            records = await _prepare_batch(
                crawler,
                keyword=keyword,
                scrape_count=take,
                enrich_websites=enrich_websites,
                drop_missing_name=drop_missing_name,
                batch_no=batch_no,
            )
            scraped_total += take

        # ② 挂起下一批的轻活 —— **真正的异步发生在这里**：
        #    它和下面的 `_finish_batch`（两家补全，最慢的一段）并行跑。
        #
        # 只在**确实还需要**下一批时才预取，两个条件都是为了避免白预取：
        #   · `ingested + take >= total_count`：本批若能达标（乐观上界），就不再预取 ——
        #     否则最后一批永远会多备一批，白跑采集+质检，还得回滚指纹；
        #   · `empty_streak == 0`：上一批零新增，说明正在熔断边缘，别浪费一次采集。
        if (async_prefetch and empty_streak == 0
                and (ingested + take) < total_count and batch_no + 1 <= max_batches):
            prefetch_take = min(batch_size, total_count - ingested)
            prefetch_no = batch_no + 1
            prefetch = asyncio.create_task(_prepare_batch(
                crawler,
                keyword=keyword,
                scrape_count=prefetch_take,
                enrich_websites=enrich_websites,
                drop_missing_name=drop_missing_name,
                batch_no=prefetch_no,
            ))
            scraped_total += prefetch_take
            logger.info(f"      ⚡ [预取] 第 {prefetch_no} 批的轻活已挂后台（上限 {prefetch_take} 家）")

        # ③ 重活：两家补全 → 剔除 → 落盘 → 写指纹
        added = await _finish_batch(
            crawler,
            records,
            keyword=keyword,
            output_file=output_file,
            enrich_tianyancha=enrich_tianyancha,
            source=source,
            mode=mode,
            captcha_provider=captcha_provider,
            captcha_wait=captcha_wait,
            drop_missing_name=drop_missing_name,
            batch_no=batch_no,
            stream_ctx=stream_ctx,
        )
        ingested += added
        logger.info(f"📦 [批次 {batch_no}] 结束：本批真正新增入库 {added} 家，累计 {ingested}/{total_count}")

        # ④ 收预取结果。补全期间它已在并行跑，这里最多等它的尾巴。
        if prefetch is not None:
            try:
                records_next = await prefetch
                if ingested >= total_count:
                    # 目标已达成 → 这批预取用不上了。**必须回滚它的候选指纹**：
                    # 候选阶段已写进指纹库，不撤的话这些家下次会被 `is_seen()` 跳过 ——
                    # "采集了却没进报表"，等于白丢（这正是今天改成"候选即写"后要注意的）。
                    rolled = _rollback_dropped_fingerprints(
                        [RawSupplierLead(**r["lead"]) for r in records_next.values()])
                    # 挂预取时就把 prefetch_take 计进了 scraped_total，这里要扣回来：
                    # 否则收尾那句"各批采集上限合计 N 家候选"会把**没真正用过**的批次算进去，
                    # 看起来像超采（用户就是靠这个数字判断有没有多采的）。
                    scraped_total -= prefetch_take
                    logger.info(f"      🧹 目标已达成，丢弃预取的第 {prefetch_no} 批"
                                f"（{len(records_next)} 家）；已回滚其指纹 {rolled} 条"
                                f" ⇒ 下次（或 --reset-pages 后）还能采到它们")
                else:
                    pending = (records_next, prefetch_take, prefetch_no)
            except Exception as e:
                logger.warning(f"      ⚠️ 预取第 {prefetch_no} 批失败"
                               f"（{type(e).__name__}: {e}）→ 下一批改为按部就班执行")
                pending = None
            finally:
                prefetch = None
                prefetch_take = 0

        if added <= 0:
            empty_streak += 1
            if empty_streak >= 2:
                logger.warning(
                    f"\n⛔ [Pipeline] 连续 {empty_streak} 批零新增，停止补批。\n"
                    f"   ⚠️ 先排查这一条（最容易误判）：**GS 翻页撞了上限**"
                    f"（`GS_MAX_SEARCH_PAGES`，当前 {config.GS_MAX_SEARCH_PAGES} 页）。\n"
                    f"      判据：在上面日志里搜「已翻到翻页上限」—— 命中就是它："
                    f"池子还在后面，只是代码不往下翻了。\n"
                    f"      处置：把 .env 的 `GS_MAX_SEARCH_PAGES` 调大后重跑（不用 --reset-pages，"
                    f"游标会从断点继续）。实测 2026-09-18：`phone+广东` 有 62 页，"
                    f"写死的 25 页只覆盖了六成，26 页之后从没被扫过。\n"
                    f"   其它可能：候选池里剩下的都是「已在报表里」或「采到就被剔除」的家"
                    f"（被剔除的家进了指纹库又回滚，会被反复采到）。\n"
                    f"   可尝试：换关键词 / 换地区 / 查看是不是两家都在触发验证码。"
                )
                break
        else:
            empty_streak = 0
        if ingested < total_count and batch_no < max_batches:
            logger.info(f"   ↻ 未达目标，继续下一批 ...")

    # —— 循环退出时的收尾：把"备好了但没用上"的批次**已登记的候选指纹撤掉** ——
    # 候选阶段就写指纹了（见 crawlers/globalsources.py 的 `dedup.add`），不撤的话这些家
    # 下轮会被 `is_seen()` 跳过：「采集了却没进报表」= 白丢。这正是第 ③ 个坑要防的事，
    # 但原来只覆盖了「目标达成」那一条路径 —— 循环因 `max_batches` 用尽而退出时同样会遗留。
    if pending is not None:
        records_left, n_left, no_left = pending
        rolled = _rollback_dropped_fingerprints(
            [RawSupplierLead(**r["lead"]) for r in records_left.values()])
        scraped_total -= n_left   # 同理：只统计真正用上的批次
        logger.info(f"      🧹 收尾：第 {no_left} 批已备好但未使用（{len(records_left)} 家），"
                    f"已回滚其指纹 {rolled} 条 ⇒ 下一轮可重新采到")
        pending = None

    # 走到这里 prefetch 通常已是 None（正常路径上 step ④ 会把它 await 干净并置空）。
    # 保留这个兜底只为防御异常路径：**cancel 会丢掉那批 records，故其候选指纹无法回滚**，
    # 所以只告警不静默。
    if prefetch is not None:
        prefetch.cancel()
        logger.warning(f"      ⚠️ 预取的第 {prefetch_no} 批被中止：该批已写入的候选指纹"
                       f"**未回滚**，如需重新采到它们请删除 seen_hashes.txt")

    logger.info("")
    logger.info("=" * 62)
    if ingested >= total_count:
        logger.info(f"🏁 [Pipeline] 目标达成：累计入库 {ingested} 家（共 {batch_no} 批，"
                    f"各批采集上限合计 {scraped_total} 家候选）")
    else:
        logger.info(f"🏁 [Pipeline] 提前结束：累计入库 {ingested}/{total_count} 家（共 {batch_no} 批，"
                    f"各批采集上限合计 {scraped_total} 家候选）")
    logger.info(f"💾 报表: {output_file}")
    logger.info("=" * 62)


# =========================================================================== #
# 双浏览器并行（单进程内）
# =========================================================================== #
async def run_dual_pipeline(
    keyword: str = "monitor",
    platform: str = "globalsources",
    max_count: int = 5,
    output_file: str = "suppliers_leads.xlsx",
    profiles: list | None = None,
    **common,
) -> None:
    """在**一个进程里并行跑两条完整流水线**，每条用一台自己的 Chrome。

    为什么这样拆：`run_pipeline` 的逻辑一行都不用改，双流的差异全部收敛在这里
    （浏览器配置 + 共享落盘锁 + 日志前缀 + 存量复核归属）。单流时这个函数直接
    降级为一次普通的 `run_pipeline`，行为与改造前一致。

    ⚠️ `max_count` 是**每条流各自**的目标（用户口径）：`-n 120` → 两条流各入库 120，
    合计约 240。想只要 120 总量就把 `-n` 设成 60。

    两条流共享的东西只有一件事：**报表落盘锁**。因为 `export_leads_to_excel` 是
    「读整表 → 合并 → 去重 → 写回」，两条流并发调用会互相覆盖、静默丢行。
    指纹库（`dedup` 单例）在单进程内是线程/协程安全的，两流共享反而能防重复采集。
    """
    profiles = profiles if profiles is not None else list(config.BROWSER_PROFILES)
    profiles = [p for p in profiles if p and getattr(p, "enabled", True)]

    if len(profiles) <= 1:
        logger.info("ℹ️ 只配置了 1 台浏览器 → 按**单流**执行（要并行请设 NUM_STREAMS=2）")
        return await run_pipeline(
            keyword=keyword, platform=platform, max_count=max_count,
            output_file=output_file, **common,
        )

    # —— 启动前检查登录态 ——
    # 浏览器 B 的 profile 是全新的（登录态跟随 --user-data-dir），没有登录标记就跑，
    # 天眼查/爱企查会全线失败，而且失败形态是"查无此企业/拿不到字段" —— 很容易被
    # 误判成数据问题。所以没登录的家先提示 + 等一会儿，仍不行就**只跳过它**。
    from utils.browser_setup import is_logged_in, marker_summary, wait_for_login

    ready: list = []
    for p in profiles:
        if is_logged_in(p):
            ready.append(p)
            continue
        if await wait_for_login(p, config.LOGIN_WAIT_SECONDS):
            ready.append(p)
        else:
            logger.warning(f"⚠️ [双浏览器] 因未登录，本次跳过浏览器 {p.name}（其余继续跑）")
    if not ready:
        logger.error("❌ [双浏览器] 没有任何一台浏览器可用（都未登录）→ 退回单流。"
                     "请先跑 `python main.py --login-browser B`。")
        return await run_pipeline(
            keyword=keyword, platform=platform, max_count=max_count,
            output_file=output_file, **common,
        )
    profiles = ready

    total = len(profiles)
    logger.info("=" * 62)
    logger.info(f"🖥️🖥️ [双浏览器并行] 启动 {total} 条流水线（每条各自入库 {max_count} 家，"
                f"合计约 {max_count * total} 家）")

    # —— 顺序预启动两台浏览器（**必须在起流之前，且串行**）——
    # 两个理由：
    #  ① 消除并发抢占端口的竞态。`ensure_chrome_running` 在目标端口被非 DevTools 占用时
    #     会 `find_available_port` 另找一个空闲端口；两条流同时做这件事，完全可能**抢到
    #     同一个**空端口 → 第二台启动失败或两台连到同一台上，而日志里看不出异常。
    #  ② 能在开工前就确认"两台确实落在不同端口"。若两台落到同一个 CDP 端口，就是
    #     **双流退化成共用一台浏览器**（代理配置、账号、IP 全部失效），必须立刻报出来 ——
    #     这种退化以前只会在数据上表现为"没什么提升"，极难察觉。
    resolved: list = []
    for p in profiles:
        rp = ensure_chrome_running(
            port=p.port, profile_dir=p.profile_dir,
            platform_name=f"预启动({p.name})",
            proxy=p.proxy, reuse_nearby=p.reuse_nearby,
        )
        if rp != p.port:
            logger.warning(f"   ⚠️ 浏览器 {p.name} 请求端口 {p.port} 被占用 → 实际使用 {rp}")
        resolved.append(replace(p, port=rp))
    profiles = resolved

    _port_map: dict = {}
    for p in profiles:
        _port_map.setdefault(p.port, []).append(p.name)
    _clash = {port: names for port, names in _port_map.items() if len(names) > 1}
    if _clash:
        logger.error(
            f"   ❌ 有两台浏览器落在**同一个 CDP 端口** {_clash} → 双流会退化成共用一台浏览器"
            f"（代理、账号、IP 全部失效）。\n"
            f"      处置：在 .env 里给它们配不同的 BROWSER_*_PORT，或关掉另一台已占端口的浏览器后重跑。"
        )
        return

    for p in profiles:
        _prio = "爱企查 → 天眼查" if p.enrich_priority == "aiqicha" else "天眼查 → 爱企查"
        logger.info(f"   · 浏览器 {p.name}: 端口 {p.port}｜profile {p.profile_dir}｜"
                    f"{('代理 ' + p.proxy) if p.proxy else '本机直连'}｜"
                    f"扫第 {p.lane_offset}、{p.lane_offset + p.lane_stride}、… 页｜"
                    f"补全顺序 {_prio}"
                    f"{f'｜延迟 {p.start_delay:.0f}s 启动' if p.start_delay else ''}")
        logger.info(f"       登录态: {marker_summary(p)}")
    _prios = {p.enrich_priority for p in profiles}
    if len(profiles) > 1 and len(_prios) > 1:
        logger.info("   ✅ 两条流的补全顺序**相反** → 任一时刻只有一条流在访问爱企查，"
                    "不会同时弹验证码")
    elif len(profiles) > 1:
        logger.warning("   ⚠️ 两条流的补全顺序**相同** → 它们会几乎同时进入爱企查、"
                       "可能同时弹验证码。可在 .env 里给其中一台设 "
                       "`BROWSER_B_ENRICH_PRIORITY=aiqicha` 来错开")
    if not any(p.proxy for p in profiles):
        logger.warning("   ⚠️ 没有任何一台配了代理（BROWSER_B_PROXY 为空）→ 两台都用本机 IP。"
                       "双流仍能跑，但**没有分摊出口 IP 的风控风险**。拿到代理后填进 .env 即可。")
    logger.info("=" * 62)

    # ⚠️ 两流共享同一把落盘锁（见函数文档）
    excel_lock = asyncio.Lock()

    async def _run_one(idx: int, prof: BrowserProfile) -> None:
        if prof.start_delay > 0:
            # 错峰启动：错开两台首次翻页/验证码爆发的时间点。
            # 手动过码时尤其有用 —— 两个窗口同时弹验证码会互相抢焦点。
            logger.info(f"   ⏳ 浏览器 {prof.name} 延迟 {prof.start_delay:.0f}s 启动（错峰）")
            await asyncio.sleep(prof.start_delay)
        ctx = StreamContext(
            tag=prof.name,
            profile=prof,
            excel_lock=excel_lock,
            # 存量报表复核只让第一条流做：它是"全量扫表 + 逐条重抓"，
            # 两条流都做等于把同一批历史行抓两遍，而 drop_urls 还是同一份。
            do_legacy_recheck=(idx == 0),
        )
        await run_pipeline(
            keyword=keyword, platform=platform, max_count=max_count,
            output_file=output_file, stream_ctx=ctx, profile=prof, **common,
        )

    # return_exceptions=True：一条流挂了（比如 B 的代理失效）不该把另一条也拖死，
    # 已经入库的数据仍然有效。异常在这里统一汇总打印。
    results = await asyncio.gather(
        *(_run_one(i, p) for i, p in enumerate(profiles)),
        return_exceptions=True,
    )

    logger.info("")
    logger.info("=" * 62)
    failed = []
    for prof, res in zip(profiles, results):
        if isinstance(res, BaseException):
            failed.append(prof.name)
            logger.error(f"❌ [双浏览器] 浏览器 {prof.name} 这条流异常结束："
                         f"{type(res).__name__}: {res}")
        else:
            logger.info(f"✅ [双浏览器] 浏览器 {prof.name} 这条流已结束")
    if failed:
        logger.warning(f"⚠️ 有 {len(failed)} 条流未正常结束（{'/'.join(failed)}）—— "
                       f"已入库的数据不受影响，可单独重跑该流。")
    logger.info(f"💾 报表: {output_file}")
    logger.info("=" * 62)


if __name__ == "__main__":
    asyncio.run(run_pipeline(keyword="chair", max_count=3, enrich_websites=True, enrich_tianyancha=True))