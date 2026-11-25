"""Второй контрольный прогон (queries_v2.csv) с сохранением настроек.

Запуск:  python run_eval_v2.py

Фиксирует порог THRESHOLD=55 и THRESHOLD_OK=True.
Сохраняет полный отчёт в файл data/report_v2.txt.
Не изменяет config.py и не перезаписывает файлы.
"""

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
import search

V2_PATH = search.DATA_DIR / "queries_v2.csv"
REPORT_PATH = search.DATA_DIR / "report_v2.txt"
EXPECTED_THRESHOLD = 55


# ── фиксация порога ──────────────────────────────────────────────
if config.THRESHOLD != EXPECTED_THRESHOLD:
    print(f"⚠  Текущий порог {config.THRESHOLD} != ожидаемый {EXPECTED_THRESHOLD}")
    print(f"   Останавливаю прогон, чтобы не исказить результат.")
    raise SystemExit(1)


# ── утилиты ──────────────────────────────────────────────────────
def pct(n, d):
    return f"{n}/{d} = {100 * n / d:.1f}%" if d else "0/0 (н/д)"


def fmt_results(results):
    """Сортировка по убыванию балла, вывод «топ-N»."""
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


# ── загрузка ─────────────────────────────────────────────────────
catalog = search.load_catalog()
with V2_PATH.open(encoding="utf-8", newline="") as fh:
    rows = list(csv.DictReader(fh))
for r in rows:
    r["expected"] = {e for e in r["expected_ids"].split("|") if e}
    r["frequency"] = int(r["frequency"])

# ── метрики ──────────────────────────────────────────────────────
n_with = sum(1 for r in rows if r["expected"])
n_without = sum(1 for r in rows if not r["expected"])
top1 = {m: 0 for m in ("baseline", "smart")}
top5 = {m: 0 for m in ("baseline", "smart")}
fp = {m: 0 for m in ("baseline", "smart")}

# ── прогон ───────────────────────────────────────────────────────
run = {m: {} for m in ("baseline", "smart")}  # query -> результаты
for r in rows:
    for mode in ("baseline", "smart"):
        fn = search.baseline_search if mode == "baseline" else search.smart_search
        res = fn(r["query"], catalog)
        run[mode][r["query"]] = res

        if r["expected"]:
            ids = {x["item"]["id"] for x in res}
            if ids & r["expected"]:
                top5[mode] += 1
                if res and res[0]["item"]["id"] in r["expected"]:
                    top1[mode] += 1
        else:
            if res:
                fp[mode] += 1

# ── отчёт ────────────────────────────────────────────────────────
lines = []
lines.append("=" * 78)
lines.append("ВТОРОЙ КОНТРОЛЬНЫЙ ПРОГОН  (queries_v2.csv)")
lines.append("=" * 78)
lines.append(f"Дата: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
lines.append(f"Каталог: {len(catalog)} позиций")
lines.append(f"Запросов: {len(rows)} (с товаром: {n_with}, без товара: {n_without})")
lines.append(f"Порог RapidFuzz (фиксированный): {config.THRESHOLD}")
lines.append(f"Словарь сокращений: {len(config.ABBREVIATIONS)} записей")
lines.append("")

lines.append("-" * 78)
lines.append("ИТОГОВЫЕ МЕТРИКИ")
lines.append("-" * 78)
lines.append("")
lines.append(f"{'':>30} | {'топ-1':>14} | {'топ-5':>14}")
for m in ("baseline", "smart"):
    lines.append(f"  {m:>28} | {pct(top1[m], n_with):>14} | {pct(top5[m], n_with):>14}")

lines.append("")
lines.append(f"{'':>30} | {'ложные выдачи':>14}")
for m in ("baseline", "smart"):
    lines.append(f"  {m:>28} | {pct(fp[m], n_without):>14}")

lines.append("")
lines.append("-" * 78)
lines.append("ПОДРОБНЫЙ ОТЧЁТ ПО КАЖДОМУ ЗАПРОСУ")
lines.append("-" * 78)

for r in rows:
    lines.append("")
    lines.append(f"  Запрос: {r['query']}")
    lines.append(f"  Ожидаемые ID: {r['expected_ids'] or '(нет товара)'}")
    lines.append(f"  Частота: {r['frequency']}  Контекст: {r['context']}  Тип: {r['case_type']}")
    for mode in ("baseline", "smart"):
        res = run[mode][r["query"]]
        lines.append(f"    {mode}:")
        lines.append(fmt_results(res))

lines.append("")
lines.append("-" * 78)
lines.append("ОШИБКИ smart-режима (только с товаром)")
lines.append("-" * 78)
errors = 0
for r in rows:
    if not r["expected"]:
        continue
    res = run["smart"][r["query"]]
    ids = {x["item"]["id"] for x in res}
    if not (ids & r["expected"]):
        errors += 1
        lines.append("")
        lines.append(f"  [Ошибка] Запрос: {r['query']}")
        lines.append(f"  Ожидалось:  {r['expected_ids']}")
        res_sorted = sorted(res, key=lambda x: x["score"] or 0, reverse=True)
        if res_sorted:
            ids_out = [fmt_one(x) for x in res_sorted]
            lines.append(f"  Получено:   {', '.join(ids_out)}")
        else:
            lines.append(f"  Получено:   ничего не найдено")
if errors == 0:
    lines.append("  ошибок нет")

lines.append("")
lines.append("-" * 78)
lines.append("ЛОЖНЫЕ ВЫДАЧИ (запросы без товара) — smart")
lines.append("-" * 78)
fp_count = 0
for r in rows:
    if r["expected"]:
        continue
    res = run["smart"][r["query"]]
    if res:
        fp_count += 1
        lines.append("")
        lines.append(f"  [Ложная выдача] Запрос: {r['query']}")
        ids_out = [fmt_one(x) for x in res]
        lines.append(f"  Получено:     {', '.join(ids_out)}")
if fp_count == 0:
    lines.append("  ложных выдач нет")

lines.append("")
lines.append("=" * 78)
lines.append("КОНЕЦ ОТЧЁТА")
lines.append("=" * 78)

report = "\n".join(lines)

# Сохранение
REPORT_PATH.write_text(report, encoding="utf-8")
print(report)
print(f"\nОтчёт сохранён: {REPORT_PATH}")
