"""公告数据的存取、逐月比对与网页生成。

  load_bulletins() —— data/bulletins/ 下每期一个 JSON，按月份排序
  compare()        —— 相邻两期逐格比对，算出前进／倒退天数
  render()         —— 数据注入 docs/template.html，写出 docs/index.html
"""

import json
from datetime import date, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "data"
BULLETINS = DATA / "bulletins"
SITE = ROOT / "docs"
EASTERN = timezone(timedelta(hours=-4))  # 夏令时；仅用于显示

SHOW = {"C": "C", "U": "U"}  # 照公告原样写，C = Current，U = Unauthorized


def load_labels() -> dict:
    return yaml.safe_load((ROOT / "labels.yaml").read_text(encoding="utf-8"))


def load_bulletins() -> list[dict]:
    if not BULLETINS.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(BULLETINS.glob("*.json"))]


def save_bulletin(b: dict) -> None:
    BULLETINS.mkdir(parents=True, exist_ok=True)
    (BULLETINS / f"{b['month']}.json").write_text(
        json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
    )


def load_json(name: str, default):
    path = DATA / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(name: str, obj) -> None:
    DATA.mkdir(exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")


def show(v: str | None) -> str:
    return SHOW.get(v, v) if v else "—"


def movement(old: str | None, new: str) -> dict:
    """一格的变化。kind 取 up / down / same / none，网页据此着色。

    C（Current）视为比任何日期都靠前；U（Unauthorized）视为比任何日期都靠后。
    """
    if old is None:
        return {"kind": "none", "text": "—"}
    if old == new:
        return {"kind": "same", "text": "不变"}
    if new == "U":
        return {"kind": "down", "text": "暂停签发"}
    if old == "U":
        return {"kind": "up", "text": "恢复签发"}
    if new == "C":
        return {"kind": "up", "text": "前进至 C"}
    if old == "C":
        return {"kind": "down", "text": "由 C 倒退"}
    days = (date.fromisoformat(new) - date.fromisoformat(old)).days
    return {"kind": "up", "text": f"前进{days}天"} if days > 0 else {"kind": "down", "text": f"倒退{-days}天"}


# EB-5 三项预留（乡村／高失业区／基础设施）在网页和通知里合并成一行
EB5_SET_ASIDE = {"EB5R": "乡村", "EB5H": "高失业", "EB5I": "基建"}


def _merge_set_aside(rows: dict) -> dict:
    """三项取值相同（历来如此，多为 C）时就显示这个值；一旦分化，
    逐项写明，不让合并掩盖差异。"""
    keys = [k for k in EB5_SET_ASIDE if k in rows]
    if not keys:
        return rows
    merged = {}
    for c in rows[keys[0]]:
        parts = [rows[k][c] for k in keys]
        olds = {p["old"] for p in parts}
        news = {p["new"] for p in parts}
        moves = {p["move"]["text"] for p in parts}
        if len(olds) == 1 and len(news) == 1:
            merged[c] = parts[0]
            continue
        label = lambda k, v: f"{EB5_SET_ASIDE[k]} {show(v)}"
        changed = [(k, p["move"]) for k, p in zip(keys, parts) if p["move"]["kind"] in ("up", "down")]
        kinds = {m["kind"] for _, m in changed}
        merged[c] = {
            "old": "／".join(label(k, p["old"]) for k, p in zip(keys, parts)),
            "new": "／".join(label(k, p["new"]) for k, p in zip(keys, parts)),
            "move": {"kind": "same", "text": "不变"} if not changed else {
                "kind": kinds.pop() if len(kinds) == 1 else "down",
                "text": "、".join(f"{EB5_SET_ASIDE[k]}{m['text']}" for k, m in changed)
                if len(moves) > 1 else changed[0][1]["text"],
            },
        }
    out = {}
    for k, v in rows.items():
        if k == keys[0]:
            out["EB5S"] = merged
        elif k not in EB5_SET_ASIDE:
            out[k] = v
    return out


def compare(prev: dict | None, cur: dict) -> dict:
    """{chart: {row: {country: {"old", "new", "move"}}}}"""
    out = {}
    for chart, rows in cur["charts"].items():
        old_rows = (prev or {}).get("charts", {}).get(chart, {})
        out[chart] = _merge_set_aside({
            row: {
                c: {"old": old_rows.get(row, {}).get(c), "new": v, "move": movement(old_rows.get(row, {}).get(c), v)}
                for c, v in cells.items()
            }
            for row, cells in rows.items()
        })
    return out


def month_cn(ym: str) -> str:
    y, m = ym.split("-")
    return f"{y} 年 {int(m)} 月"


def render(bulletins: list[dict], uscis: dict, checked_label: str) -> None:
    labels = load_labels()
    cur = bulletins[-1]
    prev = bulletins[-2] if len(bulletins) > 1 else None
    table = compare(prev, cur)

    page = {
        "cur": {"month": cur["month"], "pdf": cur["pdf"], "page": cur["page"]},
        "prev": {"month": prev["month"]} if prev else None,
        "uscis": {m: uscis.get(m) for m in (cur["month"], prev and prev["month"]) if m},
        "labels": labels,
        # 值与变化文字都在这里算好，网页只管展示，通知邮件用同一套文字
        "charts": {
            chart: [
                {
                    "key": row,
                    "cells": {
                        c: {"old": show(x["old"]), "new": show(x["new"]), **x["move"]}
                        for c, x in cells.items()
                    },
                }
                for row, cells in rows.items()
            ]
            for chart, rows in table.items()
        },
    }

    html = (
        (SITE / "template.html")
        .read_text(encoding="utf-8")
        .replace("{{DATA_JSON}}", json.dumps(page, ensure_ascii=False, indent=1))
        .replace("{{CHECKED}}", checked_label)
    )
    (SITE / "index.html").write_text(html, encoding="utf-8", newline="\n")
