#!/usr/bin/env python3
"""check_astra_typo.py — регрессия: опечатка в названии семейства.

Правило: «astra linuc» → «astra linux» должно быть исправлено до ранжирования,
самая релевантная карточка (Astra Linux Special Edition) — первой в прямых
совпадениях. Аналогично для «астра линукс».
"""

from __future__ import annotations

import sys
import io
from pathlib import Path

# Убедимся, что корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import organizer_search as osr
from organizer_index import connect


def main() -> int:
    conn = connect()
    queries = {
        "astra linuc": {
            "tokens": ["astra", "linux"],  # linuc исправлен на linux
            "corr": [("linuc", "linux")],
            "direct_top": "Astra Linux Special Edition",
        },
        "астра линукс": {
            "tokens": ["астр", "linux"],  # линукс → linux
            "corr": [("линукс", "linux")],
            "direct_top": "Astra Linux Special Edition",
        },
        "astra linux": {
            "tokens": ["astra", "linux"],  # без исправления
            "corr": [],
            "direct_top": "Astra Linux Special Edition",
        },
    }
    passed = 0
    failed = 0
    for q, expected in queries.items():
        r = osr.search(conn, q, limit=12)
        ok = True
        # 1) токены после исправления (порядок не важен)
        if set(r["tokens"]) != set(expected["tokens"]):
            print(f"FAIL {q}: tokens {r['tokens']} != {expected['tokens']}")
            ok = False
        # 2) исправления опечаток
        corr_from = [(c["from"], c["to"]) for c in r.get("corrections") or []]
        if corr_from != expected["corr"]:
            print(f"FAIL {q}: corrections {corr_from} != {expected['corr']}")
            ok = False
        # 3) direct секция не пуста
        direct = r["buckets"].get("direct", [])
        if not direct:
            print(f"FAIL {q}: нет прямых совпадений")
            ok = False
        # 4) первый прямой — Astra Linux Special Edition
        if direct and expected["direct_top"] not in direct[0]["name"]:
            print(f"FAIL {q}: первый direct «{direct[0]['name'][:50]}» — не ОС")
            ok = False
        # 5) for_os секция не пуста (товары для ОС отдельно)
        for_os = r["buckets"].get("for_os", [])
        if not for_os:
            print(f"FAIL {q}: нет секции for_os (товары для ОС)")
            ok = False

        if ok:
            print(f"PASS {q}")
            passed += 1
        else:
            failed += 1

    print(f"\nИтог: {passed}/{passed+failed} проверок пройдено")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
