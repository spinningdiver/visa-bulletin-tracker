"""抓取并解析国务院 Visa Bulletin 与 USCIS 的「本月用哪张表」。

数据来源（2026-09 实测）：
  · 公告网页 travel.state.gov/.../visa-bulletin-for-october-2026.html 前面挡着
    Cloudflare 人机验证，脚本请求一律 403。
  · 同一份公告的官方 PDF 放在 content/dam 下，不经验证，文件名规律固定：
        visabulletin_October2026.pdf
    这就是本项目的数据源。公告发布前该地址返回 404，据此判断新一期是否已出。

PDF 结构（2026-08/09/10 三期一致）：
  四张排期表依次为 亲属·裁定、亲属·递交、职业·裁定、职业·递交。每张都是 6 列
  （类别 + 5 个国家/地区），首行是表头（"Family-Sponsored"/"Employment-Based"）。
  职业表太长时会被分页截断，下一页的续表没有表头 —— 无表头的 6 列表格
  一律并入上一张。多样性移民的地区表只有 3 列，自然被排除。
"""

import calendar
import io
import re
from datetime import date

import pdfplumber
import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
PDF_URL = "https://travel.state.gov/content/dam/visas/Bulletins/visabulletin_{name}{year}.pdf"
# 网页版给人看；财年从 10 月开始，所以 10–12 月的公告放在下一年的目录里
PAGE_URL = (
    "https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin/"
    "{fy}/visa-bulletin-for-{slug}-{year}.html"
)
USCIS_URL = "https://www.uscis.gov/visabulletininfo"

CHARTS = ("family_final", "family_filing", "employment_final", "employment_filing")

COUNTRY_COLS = {  # 表头关键词 → 代码
    "all chargeability": "ALL",
    "china": "CN",
    "india": "IN",
    "mexico": "MX",
    "philippines": "PH",
}

FAMILY_ROWS = ("F1", "F2A", "F2B", "F3", "F4")


def _employment_key(label: str) -> str | None:
    """职业类的行名五花八门（含换行、括号注释），按关键词归一。"""
    s = re.sub(r"\s+", " ", label).lower()
    if "religious" in s:
        return "SR"
    if "other workers" in s:
        return "EW"
    if s.startswith("5th"):
        if "unreserved" in s:
            return "EB5U"
        if "rural" in s:
            return "EB5R"
        if "unemployment" in s:
            return "EB5H"
        if "infrastructure" in s:
            return "EB5I"
        return None
    for n in ("1st", "2nd", "3rd", "4th"):
        if s.startswith(n):
            return "EB" + n[0]
    return None


EMPLOYMENT_ROWS = ("EB1", "EB2", "EB3", "EW", "EB4", "SR", "EB5U", "EB5R", "EB5H", "EB5I")

VALUE_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})$")
MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}


def parse_value(v: str, today: date | None = None) -> str:
    """22JAN20 → 2020-01-22；C（无排期）与 U（暂停）原样保留。"""
    v = (v or "").strip().upper()
    if v in ("C", "U"):
        return v
    m = VALUE_RE.match(v)
    if not m or m.group(2) not in MONTHS:
        raise ValueError(f"无法识别的排期值：{v!r}")
    dd, mon, yy = m.groups()
    # 两位年份：不晚于明年的算 20xx，其余是 19xx（个别类别历史上排到过 90 年代）
    cutoff = (today or date.today()).year % 100 + 1
    year = 2000 + int(yy) if int(yy) <= cutoff else 1900 + int(yy)
    return date(year, MONTHS[mon], int(dd)).isoformat()


def _header_cols(row: list[str]) -> list[str]:
    cols = []
    for cell in row[1:]:
        text = re.sub(r"\s+", " ", cell or "").lower()
        code = next((c for k, c in COUNTRY_COLS.items() if k in text), None)
        if code is None:
            raise ValueError(f"表头出现未知国家列：{cell!r}")
        cols.append(code)
    return cols


def parse_pdf(data: bytes) -> dict:
    """PDF 字节 → {chart: {row: {country: value}}}。结构不符预期时抛异常，
    宁可这一期不更新也不要把错位的数据发布出去。"""
    raw_charts = []  # [(kind, cols, rows)]
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table or len(table[0]) != 6:
                    continue
                first = re.sub(r"\s+", "", table[0][0] or "").lower()
                if first.startswith(("family", "employment")):
                    kind = "family" if first.startswith("family") else "employment"
                    raw_charts.append((kind, _header_cols(table[0]), list(table[1:])))
                elif raw_charts:
                    raw_charts[-1][2].extend(table)

    kinds = [k for k, _, _ in raw_charts]
    if kinds != ["family", "family", "employment", "employment"]:
        raise ValueError(f"排期表数量或顺序不符预期：{kinds}")

    out = {}
    for name, (kind, cols, rows) in zip(CHARTS, raw_charts):
        chart = {}
        for row in rows:
            label = (row[0] or "").strip()
            key = label.upper() if kind == "family" else _employment_key(label)
            if key is None:
                raise ValueError(f"{name} 出现未知类别：{label!r}")
            chart[key] = {c: parse_value(v) for c, v in zip(cols, row[1:])}

        expected = FAMILY_ROWS if kind == "family" else EMPLOYMENT_ROWS
        # 递交表历年都比裁定表少几行（如宗教工作者），少行允许，多行或缺主类别不行
        missing = [r for r in expected[:5] if r not in chart]
        extra = [r for r in chart if r not in expected]
        if missing or extra:
            raise ValueError(f"{name} 类别不符：缺 {missing}，多 {extra}")
        out[name] = {r: chart[r] for r in expected if r in chart}
    return out


def month_urls(ym: str) -> tuple[str, str]:
    year, month = map(int, ym.split("-"))
    name = calendar.month_name[month]
    fy = year + 1 if month >= 10 else year
    return (
        PDF_URL.format(name=name, year=year),
        PAGE_URL.format(fy=fy, slug=name.lower(), year=year),
    )


def fetch_bulletin(ym: str, session: requests.Session) -> dict | None:
    """取某月公告。尚未发布返回 None；其他失败抛异常。"""
    pdf_url, page_url = month_urls(ym)
    resp = session.get(pdf_url, headers={"User-Agent": UA}, timeout=60)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    if not resp.content.startswith(b"%PDF"):
        raise ValueError(f"{pdf_url} 返回的不是 PDF")
    return {"month": ym, "pdf": pdf_url, "page": page_url, "charts": parse_pdf(resp.content)}


USCIS_RE = re.compile(
    r"For all (family-sponsored|employment-based) preference categories, you must use the "
    r"(Dates for Filing|Final Action Dates) chart in the Department of State Visa Bulletin "
    r"for (\w+) (\d{4})",
    re.I,
)


def fetch_uscis_charts(session: requests.Session) -> dict:
    """USCIS 每月指定 I-485 用哪张表 → {"2026-10": {"family": "filing", ...}}。

    页面同时列出「本月」和「下月」两段，同一句式，一个正则全部取到。
    下月的指定通常在公告发布后一周内才出现，没出现时就缺这一项。
    """
    resp = session.get(USCIS_URL, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    text = re.sub(r"\s+", " ", BeautifulSoup(resp.text, "html.parser").get_text(" "))
    months = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}

    out: dict = {}
    for kind, chart, mon, year in USCIS_RE.findall(text):
        if mon.lower() not in months:
            continue
        ym = f"{year}-{months[mon.lower()]:02d}"
        key = "family" if kind.lower().startswith("family") else "employment"
        out.setdefault(ym, {})[key] = "filing" if "filing" in chart.lower() else "final"
    return out
