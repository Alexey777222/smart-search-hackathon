"""Настройка порога RapidFuzz на ОТДЕЛЬНОМ тюнинг-наборе.

Запуск:  python tune.py            — перебор порогов, лучший печатается
         python tune.py --apply    — записать лучший порог в config.py

Контрольный набор data/queries.csv здесь НЕ используется.
"""

import argparse
import csv
import re
from pathlib import Path

import config
import search

TUNING_PATH = search.DATA_DIR / "tuning_queries.csv"
CONFIG_PATH = Path(__file__).resolve().parent / "config.py"


def load_tuning() -> list[dict]:
    with TUNING_PATH.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["expected"] = {e for e in row["expected_ids"].split("|") if e}
    return rows


def accuracy(rows: list[dict], catalog: list[dict]) -> tuple[int, int]:
    """Доля верных исходов: хит в топ-5 либо корректное «ничего не найдено»."""
    correct = 0
    for row in rows:
        ids = {r["item"]["id"] for r in search.smart_search(row["query"], catalog)}
        if row["expected"]:
            if ids & row["expected"]:
                correct += 1
        elif not ids:
            correct += 1
    return correct, len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="записать лучший порог в config.py")
    args = parser.parse_args()

    rows = load_tuning()
    catalog = search.load_catalog()

    print(f"Тюнинг-набор: {len(rows)} примеров ({TUNING_PATH.name})\n")
    print(f"{'порог':>6} | {'доля верных исходов'}")
    best_thr, best_pair = None, (0, 0)
    seen = []
    for thr in range(40, 96, 5):
        config.THRESHOLD = thr
        correct, total = accuracy(rows, catalog)
        seen.append((thr, correct, total))
        print(f"{thr:>6} | {correct}/{total} = {100 * correct / total:.1f}%")
        if (correct, thr) >= (best_pair[0], best_thr or 0):
            best_pair, best_thr = (correct, total), thr

    print(f"\nЛучший порог: {best_thr} ({best_pair[0]}/{best_pair[1]})")

    if args.apply:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        new = re.sub(r"THRESHOLD = \d+", f"THRESHOLD = {best_thr}", text)
        CONFIG_PATH.write_text(new, encoding="utf-8")
        print(f"Порог {best_thr} записан в config.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
