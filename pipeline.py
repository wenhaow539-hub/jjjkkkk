import asyncio
import random
from datetime import datetime
import re

import httpx
import pandas as pd
from playwright.async_api import async_playwright

import config
from core.browser import ensure_chrome_running
from core.factory import CrawlerFactory
import crawlers
from enrichers.aiqicha import AiQiChaEnricher
from enrichers.evaluator import EvaluatedSupplier, evaluate_supplier_icp
from enrichers.tianyancha import TianyanchaEnricher
from enrichers.website import WebsiteEnricher
from exporters.excel import export_leads_to_excel
from models import RawSupplierLead
from utils.dedup import commit_lead_fingerprints
from utils.logger import get_logger

logger = get_logger("pipeline")

# —— 工商补全数据源 ——
# aiqicha    : 爱企查（默认）
# tianyancha : 天眼查（改动前的行为，保留可切回）
# both       : 双源漏斗——先爱企查，关键字段仍缺失的再用天眼查兜底
ENRICH_SOURCES = ("aiqicha", "tianyancha", "both")
ENRICH_SOURCE_LABELS = {"aiqicha": "爱企查", "tianyancha": "天眼查", "both": "双源(爱企查→天眼查)"}

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
#   ④ 指纹库**只在成功入库后**才写（见 `utils.dedup.commit_lead_fingerprints`）。
#      所以被这里剔除的家**不会**进指纹库 ⇒ 下一轮还会被重新采到、再给一次机会。
#      有意如此：宁可多花几次详情请求，也不要"一次没爬到就永久丢掉这家"。
def _has_chinese(text: str) -> bool:
    return bool(re.search(r'[\u4e00-\u9fa5]', text or ""))


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
) -> dict:
    """按数据源逐家补全工商数据（节流 + 熔断）。

    - source="aiqicha"    ：只跑爱企查
    - source="tianyancha" ：只跑天眼查（改动前行为）
    - source="both"       ：先爱企查，关键字段仍缺失的再用天眼查兜底
    熔断：连续 `fuse` 家环境异常（验证码超时/被拦/请求异常）→ 中止剩余补全，
    但只记录错误、不抛异常，让已采数据继续落盘。

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
    else:  # 默认 aiqicha
        passes.append(("aiqicha", needing))

    for name, recs in passes:
        if stats["aborted"]:
            break

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
                stats["skipped"] += len(recs) - idx
                logger.error(
                    f"      🛑 [熔断] 连续 {consecutive_failed} 家{label}环境异常（验证码/反爬/请求失败），"
                    f"已中止剩余 {len(recs) - idx} 家补全。\n"
                    f"         处置建议：在对应的 Chrome 窗口中人工过验证码后重跑，"
                    f"或改用其它浏览器环境（--cdp-url）。已采集的数据仍会正常落盘。"
                )
                break

    return stats


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
):
    source = (enrich_source or "tianyancha").strip().lower()
    if source not in ENRICH_SOURCES:
        logger.warning(f"⚠️ [Pipeline] 未知数据源 {enrich_source!r}，回退为 tianyancha（可选: {', '.join(ENRICH_SOURCES)}）")
        source = "tianyancha"

    mode = (captcha_mode or "auto").strip().lower()
    if mode not in CAPTCHA_MODES:
        logger.warning(f"⚠️ [Pipeline] 未知验证码模式 {captcha_mode!r}，回退为 auto（可选: {', '.join(CAPTCHA_MODES)}）")
        mode = "auto"

    logger.info("=======================================================")
    logger.info("🚀 [Pipeline] 启动自动化多平台采集流水线")
    logger.info(f"🌐 目标平台: {platform} | 关键词: {keyword} | 本次计划采集: {max_count}")
    logger.info(f"🧩 工商补全数据源: {ENRICH_SOURCE_LABELS[source]}"
                f"{'（已关闭，--no-enrich）' if not enrich_tianyancha else ''}")
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
    logger.info("=======================================================")

    logger.info(f"🧹 入库门槛: " + (
        "要求「GS 有中文工商名」且「工商库能查到信息」，不满足即剔除"
        if drop_missing_name else "不设门槛，全部入库（--keep-missing-name）"))

    crawler = CrawlerFactory.get_crawler(platform)
    fresh_leads: list[RawSupplierLead] = await crawler.scrape(keyword=keyword, max_count=max_count)

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
            logger.info(f"\n🧹 [入库前过滤] {len(dropped)} 家没有中文工商名，本次不入库"
                        f"（未登记指纹 ⇒ 下一轮还会重新采到、再试一次；"
                        f"想留着这些行入库请加 --keep-missing-name）")
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
            ensure_chrome_running(port=crawler.cdp_port, platform_name=source_label)

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
            logger.info(f"\n🧹 [入库前过滤] {len(dropped)} 家在工商库中查不到任何信息"
                        f"（注册资本/实缴/参保/经营状态/联系人/电话 全空），本次不入库")
            for rec in dropped:
                ld = RawSupplierLead(**rec["lead"])
                logger.info(f"      ✗ 剔除: {ld.company} | {ld.store_url}"
                            f" —— 判定 {rec.get('enrich_verdict') or '无数据'}")
        if unattempted:
            logger.warning(f"      ⚠️ 另有 {len(unattempted)} 家**未执行**工商补全（熔断跳过或 --no-enrich），"
                           f"已保留入库（它们不算'查不到'）。要清理请重跑补齐后再判定。")
        records = kept

    # 5. 存量报表里「无中文名」的历史行：重爬一次，仍取不到则删除
    drop_urls: set[str] = set()
    if drop_missing_name:
        drop_urls = await _recheck_legacy_missing_name(crawler, output_file)

    # 6. 落盘报表导出
    leads_data = [RawSupplierLead(**rec["lead"]) for rec in records.values()]
    enriched_results = [rec["enrich"] for rec in records.values()]
    eval_results = [
        EvaluatedSupplier(**rec["eval"]) if rec.get("eval") else _default_eval(RawSupplierLead(**rec["lead"]))
        for rec in records.values()
    ]
    export_leads_to_excel(
        leads_data=leads_data,
        enriched_results=enriched_results,
        eval_results=eval_results,
        keyword=keyword,
        output_file=output_file,
        drop_urls=drop_urls,
    )

    # 7. 落盘**成功之后**才写指纹库。
    # 位置刻意放在 export 之后而不是采集阶段：指纹库的语义是「这家已经进了报表」，
    # 早写会让「采到 → 被剔除 → 指纹已写 → 永久消失」成立（实测 3218 条指纹只对应
    # 286 行报表）。放这里还有两个好处：
    #   · 导出抛异常 ⇒ 指纹不写 ⇒ 这批下轮还能重来；
    #   · 中途 Ctrl-C ⇒ 同理，不会留下"没入库却被标记已处理"的孤儿。
    # 注意这里用的是 `leads_data`（= records，已剔除无中文名/工商全空的家），
    # 所以写进去的都是**确实进过报表**的公司。
    written = commit_lead_fingerprints(leads_data)
    logger.info(f"🔑 [指纹库] 已为本次入库的 {len(leads_data)} 家商户登记指纹"
                f"（新增 {written} 条；重复写法不会重复计）")


if __name__ == "__main__":
    asyncio.run(run_pipeline(keyword="chair", max_count=3, enrich_websites=True, enrich_tianyancha=True))