"""一次完整的检查：查新公告 → 查 USCIS 用表 → 生成网页 → 存档。

本地运行：   python run.py
CI 运行：     python run.py --ci        （有新公告或用表变化时写出 data/changes.md 供开 Issue 用）
仅重建网页：  python run.py --rebuild   （不联网，改了 labels.yaml 或模板之后用）
"""

import sys
from datetime import datetime

import requests

import build
from scraper import fetch_bulletin, fetch_uscis_charts

# 通知里的顺序，与网页标签一致（scraper.CHARTS 是 PDF 里的顺序，不能动）
CHARTS = ("family_filing", "family_final", "employment_filing", "employment_final")

try:  # Windows 控制台默认 GBK，会把中文输出成乱码
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

SITE_URL = "https://spinningdiver.github.io/visa-bulletin-tracker/"
NOTIFY_COUNTRIES = ("CN", "ALL")  # Issue 表格里列出的国家；员工通知只写第一个
CHART_CN = {"final": "裁定排期表", "filing": "递交排期表"}


def add_month(ym: str, n: int) -> str:
    y, m = map(int, ym.split("-"))
    y, m = divmod(y * 12 + m - 1 + n, 12)
    return f"{y}-{m + 1:02d}"


# ---------- 通知文案 ----------


def _moves(diff: dict, chart: str, country: str, labels: dict) -> list[str]:
    cats = labels["categories"]
    out = []
    for row, cells in diff[chart].items():
        x = cells.get(country)
        if x and x["move"]["kind"] in ("up", "down"):
            # 新值是 C／U 时，变化文字本身已说明去向，不再接「至 …」
            to = f"至 {x['new']}" if x["new"] not in build.SHOW else ""
            out.append(f"{cats[row]['code']} {x['move']['text']}{to}")
    return out


def _uscis_sentence(ym: str, uscis: dict) -> str:
    d = uscis.get(ym) or {}
    if not ("family" in d and "employment" in d):
        return (
            f"USCIS 尚未公布 {build.month_cn(ym)}在境内递交 I-485 应使用哪张表，"
            "一般在排期发布后一周内公布，届时另行通知。"
        )
    if d["family"] == d["employment"]:
        return f"USCIS 规定 {build.month_cn(ym)}在境内递交 I-485，亲属类和职业类都使用{CHART_CN[d['family']]}。"
    return (
        f"USCIS 规定 {build.month_cn(ym)}在境内递交 I-485，亲属类使用{CHART_CN[d['family']]}，"
        f"职业类使用{CHART_CN[d['employment']]}。"
    )


def staff_notice(new: dict | None, diff: dict | None, uscis: dict, uscis_months: list[str], labels: dict) -> list[str]:
    """给员工的通知，可直接复制转发。结论先行、连贯叙述，不用小标题。"""
    body = ["Dear Team,", ""]

    if new:
        country = NOTIFY_COUNTRIES[0]
        parts = []
        for chart in CHARTS:
            moves = _moves(diff, chart, country, labels)
            name = labels["charts"][chart]
            parts.append(f"{name} {'、'.join(moves)}，其余不变" if moves else f"{name}全部不变")
        body.append(
            f"{build.month_cn(new['month'])}移民排期已公布。"
            f"{labels['countries'][country]}申请人的变化如下：{'；'.join(parts)}。"
        )
        body.append("")
        body.append(_uscis_sentence(new["month"], uscis))
    else:
        for ym in uscis_months:
            body.append(_uscis_sentence(ym, uscis))
        body.append("")
        body.append("指定使用递交排期表的月份，若某类别在裁定排期表上显示 C，或裁定排期表的截止日更晚，当月也可按裁定排期表递交。")

    body += [
        "",
        f"完整排期（含其他国家／地区）：{SITE_URL}",
        f"以国务院公告原文为准：{new['page']}" if new else "以 USCIS 官网为准：https://www.uscis.gov/visabulletininfo",
        "",
        "Best regards,",
        "Jay",
    ]
    return body


def write_changes_md(new: dict | None, prev: dict | None, uscis: dict, uscis_months: list[str]) -> None:
    """CI 用它作为 Issue 的标题与正文。第一行是标题。"""
    labels = build.load_labels()
    diff = build.compare(prev, new) if new else None

    if new:
        title = f"{build.month_cn(new['month'])}移民排期已公布"
    else:
        title = f"USCIS 公布 {'、'.join(build.month_cn(m) for m in uscis_months)} I-485 用表"

    lines = [title, ""]
    if new:
        lines += [f"与 {build.month_cn(prev['month'])}相比：", ""]
        for chart in CHARTS:
            lines += [f"**{labels['charts'][chart]}**", ""]
            lines += ["| 类别 | " + " | ".join(labels["countries"][c] for c in NOTIFY_COUNTRIES) + " |"]
            lines += ["|---" * (len(NOTIFY_COUNTRIES) + 1) + "|"]
            for row, cells in diff[chart].items():
                tds = []
                for c in NOTIFY_COUNTRIES:
                    x = cells[c]
                    moved = x["move"]["kind"] in ("up", "down")
                    tds.append(f"{build.show(x['new'])}（{x['move']['text']}）" if moved else build.show(x["new"]))
                lines.append(f"| {labels['categories'][row]['code']} | " + " | ".join(tds) + " |")
            lines.append("")

    lines += ["---", "", "### 转发给员工的通知（点右上角图标复制）", "", "```text"]
    lines += staff_notice(new, diff, uscis, uscis_months, labels)
    lines += ["```", "", f"网页已自动更新：{SITE_URL}"]
    if new:
        lines.append(f"公告原文：{new['page']}　PDF：{new['pdf']}")

    (build.DATA / "changes.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


# ---------- 主流程 ----------


def rebuild_only() -> int:
    bulletins = build.load_bulletins()
    if not bulletins:
        print("还没有任何公告数据，请先运行 python run.py")
        return 1
    checked = build.load_json("last_checked.json", {}).get("checked_at", "")
    label = checked[:16].replace("T", " ") + " ET" if checked else "—"
    build.render(bulletins, build.load_json("uscis_charts.json", {}), label)
    print(f"已用现有数据重新生成网页（最新一期 {bulletins[-1]['month']}，未联网）")
    return 0


def main() -> int:
    if "--rebuild" in sys.argv:
        return rebuild_only()

    ci = "--ci" in sys.argv
    now = datetime.now(build.EASTERN)
    this_month = now.strftime("%Y-%m")
    session = requests.Session()

    # 1. 新公告：从已有的最新一期往后逐月试，直到 404（尚未发布）。
    #    首次运行从上个月开始回填，保证网页一上来就有两期可比。
    bulletins = build.load_bulletins()
    first_run = not bulletins
    ym = add_month(bulletins[-1]["month"], 1) if bulletins else add_month(this_month, -1)
    new_bulletins = []
    while ym <= add_month(this_month, 1):
        print(f"查询 {ym} 公告 … ", end="", flush=True)
        b = fetch_bulletin(ym, session)  # 解析失败直接抛异常，让 CI 亮红，不发布错数据
        if b is None:
            print("尚未发布")
            break
        b["fetched_at"] = now.isoformat(timespec="seconds")
        build.save_bulletin(b)
        new_bulletins.append(b)
        print("已获取")
        ym = add_month(ym, 1)
    bulletins = build.load_bulletins()

    # 2. USCIS 指定用表。抓取失败不影响排期本身，沿用上次结果
    uscis = build.load_json("uscis_charts.json", {})
    uscis_changed = []
    try:
        fetched = fetch_uscis_charts(session)
        for m, d in sorted(fetched.items()):
            if uscis.get(m) != d:
                if not first_run and "family" in d and "employment" in d:
                    uscis_changed.append(m)
                uscis[m] = d
        build.save_json("uscis_charts.json", uscis)
        for m, d in sorted(fetched.items()):
            print(f"USCIS 用表 {m}：亲属 {d.get('family', '未公布')}，职业 {d.get('employment', '未公布')}")
    except Exception as exc:
        print(f"!! USCIS 用表抓取失败（沿用上次结果）：{type(exc).__name__}: {exc}"[:300])

    # 3. 网页与存档
    build.render(bulletins, uscis, now.strftime("%Y-%m-%d %H:%M ET"))
    build.save_json(
        "last_checked.json",
        {
            # 每天都写，即使无变化 —— 保证仓库天天有提交，
            # 否则 GitHub 会在 60 天无活动后自动停用定时工作流
            "checked_at": now.isoformat(timespec="seconds"),
            "latest": bulletins[-1]["month"] if bulletins else None,
            "new": [b["month"] for b in new_bulletins],
        },
    )

    # 4. 通知：新公告一期一条；只有用表变化时单独一条。首次回填不通知
    notify_new = None if first_run or not new_bulletins else new_bulletins[-1]
    if notify_new or uscis_changed:
        prev = next((b for b in reversed(bulletins) if b["month"] < notify_new["month"]), None) if notify_new else None
        history = build.load_json("history.json", [])
        history.insert(
            0,
            {"date": now.strftime("%Y-%m-%d"), "bulletin": notify_new and notify_new["month"], "uscis": uscis_changed},
        )
        build.save_json("history.json", history)
        if ci:
            write_changes_md(notify_new, prev, uscis, [] if notify_new else uscis_changed)
        print(f"\n>> 有更新：{'新公告 ' + notify_new['month'] if notify_new else ''} {'USCIS 用表 ' + ','.join(uscis_changed) if uscis_changed else ''}")
    else:
        print("\n无更新。")

    # 5. 兜底告警：本月公告按惯例上月中旬就该出了。到了本月还没有，
    #    多半是 PDF 地址规律变了，此时退出码非零让 CI 亮红、GitHub 发失败邮件
    if not bulletins or bulletins[-1]["month"] < this_month:
        print(f"\n!! 已到 {this_month}，仍未取到本月公告。请检查 PDF 地址规律是否变化：{build.BULLETINS}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
