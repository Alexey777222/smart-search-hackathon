"""Итоговая оценка обоих режимов на контрольном наборе.

Запуск:  python evaluate.py

Метрики:
  - доля запросов с подходящим товаром, где хотя бы один правильный
    товар попал в топ-5 (с числителем и знаменателем);
  - для запросов без подходящего товара — число ложных выдач
    и общее число таких запросов;
  - самые частые запросы без результата (smart), по frequency.
"""

import csv
from collections import Counter

import search

QUERIES_PATH = search.DATA_DIR / "queries.csv"


def load_queries() -> list[dict]:
    with QUERIES_PATH.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["expected"] = {e for e in row["expected_ids"].split("|") if e}
        row["frequency"] = int(row["frequency"])
    return rows


def pct(num: int, den: int) -> str:
    return f"{num}/{den} = {100 * num / den:.1f}%" if den else "0/0 = н/д"


def main() -> int:
    catalog = search.load_catalog()
    rows = load_queries()

    stats = {mode: Counter() for mode in ("baseline", "smart")}
    missed = {mode: [] for mode in ("baseline", "smart")}
    falses = {mode: [] for mode in ("baseline", "smart")}
    no_result = []  # запросы smart без какого-либо результата

    for row in rows:
        for mode in ("baseline", "smart"):
            fn = (search.baseline_search if mode == "baseline"
                  else search.smart_search)
            ids = {r["item"]["id"] for r in fn(row["query"], catalog)}
            s = stats[mode]
            if row["expected"]:
                s["with_product"] += 1
                if ids & row["expected"]:
                    s["top5_hit"] += 1
                else:
                    missed[mode].append(row["query"])
            else:
                s["no_product"] += 1
                if ids:
                    s["false"] += 1
                    falses[mode].append(row["query"])
        if not no_result_ids(row, catalog):
            no_result.append(row)

    print(f"Контрольный прогон: {len(rows)} запросов "
          f"({QUERIES_PATH.name}), каталог {len(catalog)} позиций\n")
    print("=== Доля запросов с правильным товаром в топ-5 ===")
    for mode in ("baseline", "smart"):
        s = stats[mode]
        print(f"  {mode:>8}: {pct(s['top5_hit'], s['with_product'])}")

    print("\n=== Ложные выдачи на запросах без подходящего товара ===")
    for mode in ("baseline", "smart"):
        s = stats[mode]
        print(f"  {mode:>8}: ложных {pct(s['false'], s['no_product'])}")
        for q in falses[mode]:
            print(f"      -> {q}")

    print("\n=== Пропущенные запросы (smart, правильный товар не в топ-5) ===")
    if missed["smart"]:
        for q in missed["smart"]:
            print(f"      -> {q}")
    else:
        print("  нет")

    print("\n=== Самые частые запросы без результата (smart) ===")
    no_result.sort(key=lambda r: r["frequency"], reverse=True)
    if no_result:
        print(f"  {'частота':>8} | {'запрос':<38} | контекст / тип")
        for r in no_result[:10]:
            print(f"  {r['frequency']:>8} | {r['query']:<38} | "
                  f"{r['context']} / {r['case_type']}")
    else:
        print("  нет")
    return 0


def no_result_ids(row: dict, catalog: list[dict]) -> bool:
    """True, если smart вернул хотя бы один результат."""
    return bool(search.smart_search(row["query"], catalog))


if __name__ == "__main__":
    raise SystemExit(main())
