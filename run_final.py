"""Финальный прогон: сравнение baseline/smart v1 vs smart v3 на v2-fixed и v3.

Запуск:  python run_final.py

Не изменяет config.py и config_v3.py.
Сохраняет отчёты в data/report_v2_fixed.txt и data/report_v3.txt.
"""

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import search as s1
import config as cfg1
import search_v3 as s3
import config_v3 as cfg3

DATA_DIR = Path(__file__).resolve().parent / "data"
V2_FIXED = DATA_DIR / "queries_v2_fixed.csv"
V3 = DATA_DIR / "queries_v3.csv"
REPORT_V2 = DATA_DIR / "report_v2_fixed.txt"
REPORT_V3 = DATA_DIR / "report_v3.txt"


# ── утилиты ───────────────────────────────────────────────────────

def pct(n, d):
    return f"{n}/{d} = {100 * n / d:.1f}%" if d else "0/0 (н/д)"


def fmt_results(results):
    if not results:
        return "  ничего не найдено"
    sr = sorted(results, key=lambda r: r["score"] or -1, reverse=True)
    lines = []
    for i, r in enumerate(sr, 1):
        tag = "топ-1" if i == 1 else ("топ-5" if i == 5 else "")
        score = (f" мера сходства: {r['score']:.1f}/100"
                 if r["score"] is not None else "")
        lines.append(f"  {i}. {r['item']['name']} [ID: {r['item']['id']}] "
                      f"{tag}{f'  ({r['method']})' if tag else ''}{score}")
    return "\n".join(lines)


def fmt_one(r):
    score = f" ({r['score']:.1f})" if r["score"] is not None else ""
    return f"{r['item']['name']} [{r['item']['id']}]{score}"


def load_queries(path):
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["expected"] = {e for e in r["expected_ids"].split("|") if e}
        r["frequency"] = int(r["frequency"])
        if "expected_note" in r:
            r["expected_note"] = r["expected_note"] or ""
    return rows


# ── прогон на одном наборе ──────────────────────────────────────

def run_on_set(rows, catalog, mode_v1="smart_search", mode_v3="smart_search_v3"):
    results = {"baseline": {}, "smart v1": {}, "smart v3": {}}
    top1 = {"baseline": 0, "smart v1": 0, "smart v3": 0}
    top5 = {"baseline": 0, "smart v1": 0, "smart v3": 0}
    fp = {"baseline": 0, "smart v1": 0, "smart v3": 0}
    n_with = sum(1 for r in rows if r["expected"])
    n_without = sum(1 for r in rows if not r["expected"])

    for r in rows:
        # baseline
        res_b = s1.baseline_search(r["query"], catalog)
        results["baseline"][r["query"]] = res_b
        if r["expected"]:
            ids = {x["item"]["id"] for x in res_b}
            if ids & r["expected"]:
                top5["baseline"] += 1
                if res_b and res_b[0]["item"]["id"] in r["expected"]:
                    top1["baseline"] += 1
        else:
            if res_b:
                fp["baseline"] += 1

        # v1 smart
        res_v1 = s1.smart_search(r["query"], catalog)
        results["smart v1"][r["query"]] = res_v1
        if r["expected"]:
            ids = {x["item"]["id"] for x in res_v1}
            if ids & r["expected"]:
                top5["smart v1"] += 1
                if res_v1 and res_v1[0]["item"]["id"] in r["expected"]:
                    top1["smart v1"] += 1
        else:
            if res_v1:
                fp["smart v1"] += 1

        # v3 smart
        res_v3 = s3.smart_search_v3(r["query"], catalog)
        results["smart v3"][r["query"]] = res_v3
        if r["expected"]:
            ids = {x["item"]["id"] for x in res_v3}
            if ids & r["expected"]:
                top5["smart v3"] += 1
                if res_v3 and res_v3[0]["item"]["id"] in r["expected"]:
                    top1["smart v3"] += 1
        else:
            if res_v3:
                fp["smart v3"] += 1

    return {
        "results": results, "top1": top1, "top5": top5, "fp": fp,
        "n_with": n_with, "n_without": n_without
    }


# ── построение отчёта ────────────────────────────────────────────

def build_report(title, data, catalog, rows, cfg_name, cfg_val):
    lines = []
    lines.append("=" * 78)
    lines.append(title)
    lines.append("=" * 78)
    lines.append(f"Дата: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    lines.append(f"Каталог: {len(catalog)} позиций")
    lines.append(f"Запросов: {len(rows)} (с товаром: {data['n_with']}, без товара: {data['n_without']})")
    lines.append(f"Порог RapidFuzz: {cfg_val}")
    lines.append("")

    lines.append("-" * 78)
    lines.append("ИТОГОВЫЕ МЕТРИКИ")
    lines.append("-" * 78)
    lines.append("")
    lines.append(f"{'':>30} | {'топ-1':>14} | {'топ-5':>14}")
    for m in ("baseline", "smart v1", "smart v3"):
        lines.append(f"  {m:>28} | {pct(data['top1'][m], data['n_with']):>14} | "
                      f"{pct(data['top5'][m], data['n_with']):>14}")
    lines.append("")
    lines.append(f"{'':>30} | {'ложные выдачи':>14}")
    for m in ("baseline", "smart v1", "smart v3"):
        lines.append(f"  {m:>28} | {pct(data['fp'][m], data['n_without']):>14}")

    lines.append("")
    lines.append("-" * 78)
    lines.append("ПОДРОБНЫЙ ОТЧЁТ ПО КАЖДОМУ ЗАПРОСУ")
    lines.append("-" * 78)

    for r in rows:
        lines.append("")
        lines.append(f"  Запрос: {r['query']}")
        lines.append(f"  Ожидаемые ID: {r['expected_ids'] or '(нет товара)'}")
        if r.get("expected_note"):
            lines.append(f"  Примечание: {r['expected_note']}")
        lines.append(f"  Частота: {r['frequency']}  Контекст: {r['context']}  Тип: {r['case_type']}")
        for mname, mkey in [("baseline", "baseline"), ("smart v1", "smart v1"), ("smart v3", "smart v3")]:
            res = data["results"][mkey][r["query"]]
            lines.append(f"    {mname}:")
            lines.append(fmt_results(res))

    lines.append("")
    lines.append("-" * 78)
    lines.append("ОШИБКИ smart v3 (только с товаром)")
    lines.append("-" * 78)
    errors = 0
    for r in rows:
        if not r["expected"]:
            continue
        res = data["results"]["smart v3"][r["query"]]
        ids = {x["item"]["id"] for x in res}
        if not (ids & r["expected"]):
            errors += 1
            lines.append("")
            lines.append(f"  [Ошибка] Запрос: {r['query']}")
            lines.append(f"  Ожидалось:  {r['expected_ids']}")
            res_sorted = sorted(res, key=lambda x: x["score"] or 0, reverse=True)
            if res_sorted:
                lines.append(f"  Получено:   {', '.join(fmt_one(x) for x in res_sorted)}")
            else:
                lines.append(f"  Получено:   ничего не найдено")
    if errors == 0:
        lines.append("  ошибок нет")

    lines.append("")
    lines.append("-" * 78)
    lines.append("ЛОЖНЫЕ ВЫДАЧИ (smart v3, запросы без товара)")
    lines.append("-" * 78)
    fp_count = 0
    for r in rows:
        if r["expected"]:
            continue
        res = data["results"]["smart v3"][r["query"]]
        if res:
            fp_count += 1
            lines.append("")
            lines.append(f"  [Ложная выдача] Запрос: {r['query']}")
            lines.append(f"  Получено:     {', '.join(fmt_one(x) for x in res)}")
    if fp_count == 0:
        lines.append("  ложных выдач нет")

    lines.append("")
    lines.append("=" * 78)
    lines.append("КОНЕЦ ОТЧЁТА")
    lines.append("=" * 78)

    return "\n".join(lines)


# ── сравнение v1 vs v3 ──────────────────────────────────────────

def compare_v1_v3(data):
    rows_with = [r for r in data["_rows"] if r["expected"]]
    lines = []
    lines.append("")
    lines.append("=" * 78)
    lines.append("СРАВНЕНИЕ: smart v1 vs smart v3")
    lines.append("=" * 78)
    lines.append("")
    lines.append(f"{'':>35} | {'v1':>14} | {'v3':>14} | {'изменение'}")
    for m in ("топ-1", "топ-5", "ложные"):
        if m == "ложные":
            v1_n, v3_n = data["fp"]["smart v1"], data["fp"]["smart v3"]
            den = data["n_without"]
        elif m == "топ-1":
            v1_n, v3_n = data["top1"]["smart v1"], data["top1"]["smart v3"]
            den = data["n_with"]
        else:
            v1_n, v3_n = data["top5"]["smart v1"], data["top5"]["smart v3"]
            den = data["n_with"]
        lines.append(f"  {m:>33} | {pct(v1_n, den):>14} | {pct(v3_n, den):>14} |")

    lines.append("")
    lines.append("Запросы, где v3 стало ЛУЧШЕ (v1 не нашёл, v3 нашёл — с товаром):")
    improved = []
    for r in rows_with:
        res_v1 = data["results"]["smart v1"][r["query"]]
        res_v3 = data["results"]["smart v3"][r["query"]]
        ids_v1 = {x["item"]["id"] for x in res_v1}
        ids_v3 = {x["item"]["id"] for x in res_v3}
        if not (ids_v1 & r["expected"]) and (ids_v3 & r["expected"]):
            improved.append(r)
    if improved:
        for r in improved:
            lines.append(f"  + {r['query']} → {r['expected_ids']}")
    else:
        lines.append("  нет")

    lines.append("")
    lines.append("Запросы, где v3 стало ХУЖЕ (v1 нашёл, v3 не нашёл — с товаром):")
    regressed = []
    for r in rows_with:
        res_v1 = data["results"]["smart v1"][r["query"]]
        res_v3 = data["results"]["smart v3"][r["query"]]
        ids_v1 = {x["item"]["id"] for x in res_v1}
        ids_v3 = {x["item"]["id"] for x in res_v3}
        if (ids_v1 & r["expected"]) and not (ids_v3 & r["expected"]):
            regressed.append(r)
    if regressed:
        for r in regressed:
            lines.append(f"  - {r['query']} → {r['expected_ids']}")
    else:
        lines.append("  нет")

    lines.append("")
    lines.append("Запросы с ложными выдачами, где v3 исправил (v1 нашёл, v3 — нет):")
    fixed_fp = []
    for r in [row for row in data["_rows"] if not row["expected"]]:
        res_v1 = data["results"]["smart v1"][r["query"]]
        res_v3 = data["results"]["smart v3"][r["query"]]
        if res_v1 and not res_v3:
            fixed_fp.append(r)
    if fixed_fp:
        for r in fixed_fp:
            lines.append(f"  ✓ {r['query']}")
    else:
        lines.append("  нет")

    lines.append("")
    lines.append("Запросы с ложными выдачами, где v3 создал (v1 не нашёл, v3 нашёл):")
    new_fp = []
    for r in [row for row in data["_rows"] if not row["expected"]]:
        res_v1 = data["results"]["smart v1"][r["query"]]
        res_v3 = data["results"]["smart v3"][r["query"]]
        if not res_v1 and res_v3:
            new_fp.append(r)
    if new_fp:
        for r in new_fp:
            res_v3_sorted = sorted(data["results"]["smart v3"][r["query"]],
                                    key=lambda x: x["score"] or 0, reverse=True)
            lines.append(f"  ✗ {r['query']} → {', '.join(fmt_one(x) for x in res_v3_sorted)}")
    else:
        lines.append("  нет")

    return "\n".join(lines)


# ── основной поток ───────────────────────────────────────────────

catalog = s1.load_catalog()

# ── прогон на v2-fixed ──────────────────────────────────────────
rows_v2 = load_queries(V2_FIXED)
data_v2 = run_on_set(rows_v2, catalog)
data_v2["_rows"] = rows_v2

report_v2 = build_report(
    "ВТОРОЙ КОНТРОЛЬНЫЙ ПРОГОН (v2-fixed) — baseline vs smart v1 vs smart v3",
    data_v2, catalog, rows_v2,
    cfg_name="config.py", cfg_val=cfg1.THRESHOLD
)
REPORT_V2.write_text(report_v2, encoding="utf-8")
print(report_v2)
print(f"\nОтчёт v2-fixed сохранён: {REPORT_V2}")

# ── прогон на v3 ────────────────────────────────────────────────
rows_v3 = load_queries(V3)
data_v3 = run_on_set(rows_v3, catalog)
data_v3["_rows"] = rows_v3

report_v3 = build_report(
    "ФИНАЛЬНЫЙ КОНТРОЛЬНЫЙ ПРОГОН (v3) — baseline vs smart v1 vs smart v3",
    data_v3, catalog, rows_v3,
    cfg_name="config_v3.py", cfg_val=cfg3.THRESHOLD
)
REPORT_V3.write_text(report_v3, encoding="utf-8")
print(report_v3)
print(f"\nОтчёт v3 сохранён: {REPORT_V3}")

# ── сравнение v1 vs v3 на v2-fixed ──────────────────────────────
comparison = compare_v1_v3(data_v2)
print(comparison)
COMPARISON_PATH = DATA_DIR / "comparison_v1_vs_v3.txt"
COMPARISON_PATH.write_text(comparison, encoding="utf-8")
print(f"\nСравнение сохранено: {COMPARISON_PATH}")
print(f"\nВсего запросов в v2-fixed: {len(rows_v2)}")
print(f"Всего запросов в v3: {len(rows_v3)}")
print(f"\nПорог RapidFuzz: v1={cfg1.THRESHOLD}, v3={cfg3.THRESHOLD} (не менялся)")
print(f"Словарь v1: {len(cfg1.ABBREVIATIONS)} записей")
print(f"Словарь v3: {len(cfg3.ABBREVIATIONS)} записей + {len(cfg3.COMMON_TERMS)} терминов + {len(cfg3.BRAND_ALIASES)} брендов")
