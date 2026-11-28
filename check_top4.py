"""check_top4.py — автоматическая проверка требований топ-4 и допродаж.

Запуск:
  python check_top4.py                 # печатает PASS/FAIL
  python check_top4.py --json          # JSON-отчёт
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

import organizer_search as osr
from organizer_index import connect, normalize_text
from selection import (build_top4, extract_requirements, is_os_offer,
                       verify_requirements)


def _pass(label: str, ok: bool) -> str:
    return "[PASS]" if ok else "[FAIL]"


def run(conn: sqlite3.Connection) -> dict:
    checks: list[dict] = []
    results: list[dict] = []

    # ── запросы из требований заказчика ──────────────────────────
    queries = {
        "astra linux": {
            "expect": {
                "count": 4, "first_family": "astra",
                "first_available": True, "first_price": True,
            },
        },
        "astra linuc": {
            "expect": {
                "count": 4, "first_family": "astra",
                "first_available": True, "first_price": True,
                "corrections": True,
            },
        },
        "астра линукс": {
            "expect": {
                # 3, а не 4: четвёртой позицией было средство восстановления
                # Granulex (не операционная система) — теперь оно отбраковано
                # как другой функциональный класс.
                "count_ge": 3, "first_family": "astra",
                "first_available": True, "first_price": True,
            },
        },
        "Найди Linux": {
            "expect": {
                "count": 4,
                "first_os": True,
                "first_available": True,
                "first_price": True,
                "effective": "linux",
            },
        },
        "linux": {
            "expect": {
                "count": 4,
                "first_os": True,
                "first_available": True,
                "first_price": True,
            },
        },
        "сервер": {
            "expect": {
                "count": 4,
                "distinct_families": 4,
                "all_available": True,
                "all_price": True,
            },
        },
        "ПО для сервера": {
            "expect": {
                "count": 4,
                "software_only": True,
                "distinct_vendors": 4,
            },
        },
        "adobe photoshop 2025": {
            "expect": {
                "count_gt": 0,       # не пустой топ-4
                "all_analog": True,   # нет точного — аналог
                "version_in_reason": "2025",
            },
        },
        "кибер бэкап без windows": {
            "expect": {
                "not_wants_os": True,
                "no_windows_only": True,
                "count_ge": 1,
            },
        },
        "кибер бэкап не windows": {
            "expect": {
                "not_wants_os": True,
                "no_windows_only": True,
                "count_ge": 1,
            },
        },
        "astra linux 1.7 x86-64": {
            "expect": {
                "first_family": "astra",
                "reason_contains": "x86-64",
            },
        },
    }

    for query, spec in queries.items():
        result = osr.search(conn, query, limit=12)
        reqs = extract_requirements(
            result.get("normalized") or result.get("effective_query") or query)
        top = build_top4(result, reqs)
        items = top["items"]

        checks_item: list[dict] = []
        # — count
        exp = spec["expect"]
        if "count" in exp:
            ok = top["count"] == exp["count"]
            checks_item.append({"check": f"count == {exp['count']}",
                                "ok": ok,
                                "got": top["count"]})
        if "count_gt" in exp:
            ok = top["count"] > exp["count_gt"]
            checks_item.append({"check": f"count > {exp['count_gt']}",
                                "ok": ok, "got": top["count"]})
        if "count_ge" in exp:
            ok = top["count"] >= exp["count_ge"]
            checks_item.append({"check": f"count >= {exp['count_ge']}",
                                "ok": ok, "got": top["count"]})

        if items:
            first = items[0]
            if "first_family" in exp:
                hay = (normalize_text(first["name"]) + " "
                       + normalize_text(first.get("vendor") or ""))
                ok = exp["first_family"] in hay
                checks_item.append({"check": f"first family '{exp['first_family']}'",
                                    "ok": ok, "got": first["name"][:50]})
            if "first_available" in exp:
                ok = first["available"]
                checks_item.append({"check": "first available",
                                    "ok": ok, "got": first["available_text"]})
            if "first_price" in exp:
                ok = (first["price"] or 0) > 0
                checks_item.append({"check": "first has price",
                                    "ok": ok, "got": first["price_text"]})
            if "first_os" in exp:
                # Проверяем по категории/названию оригинального SKU
                best = _find_best_group(conn, first.get("full_name") or first["name"])
                ok = is_os_offer(best) if best else False
                checks_item.append({"check": "first is OS",
                                    "ok": ok, "got": best.get("name") or first["name"][:50]})
            if "effective" in exp:
                ok = (result.get("effective_query") or "").lower() == exp["effective"]
                checks_item.append({"check": "effective query",
                                    "ok": ok, "got": result.get("effective_query")})
            if "reason_contains" in exp:
                ok = exp["reason_contains"] in (first.get("reason") or "")
                checks_item.append({"check": f"reason contains '{exp['reason_contains']}'",
                                    "ok": ok, "got": first.get("reason", "")[:60]})

        if "corrections" in exp:
            ok = bool(result.get("corrections"))
            checks_item.append({"check": "has corrections",
                                "ok": ok, "got": result.get("corrections")})

        if "distinct_families" in exp:
            fams = {it.get("family_id") for it in items}
            ok = len(fams) == exp["distinct_families"]
            checks_item.append({"check": f"{exp['distinct_families']} distinct families",
                                "ok": ok, "got": len(fams)})

        if "distinct_vendors" in exp:
            vnds = {normalize_text(it.get("vendor") or "") for it in items}
            ok = len(vnds) == exp["distinct_vendors"]
            checks_item.append({"check": f"{exp['distinct_vendors']} distinct vendors",
                                "ok": ok, "got": len(vnds)})

        if "software_only" in exp:
            ok = bool(result.get("software_only"))
            checks_item.append({"check": "software_only", "ok": ok})

        if "all_available" in exp:
            ok = all(it["available"] for it in items)
            checks_item.append({"check": "all available",
                                "ok": ok, "got": top["count"]})

        if "all_price" in exp:
            ok = all((it["price"] or 0) > 0 for it in items)
            checks_item.append({"check": "all have price",
                                "ok": ok, "got": top["count"]})

        if "all_analog" in exp:
            ok = all(it["role"] == "функциональный аналог" for it in items)
            checks_item.append({"check": "all analog", "ok": ok})

        if "version_in_reason" in exp:
            ok = all(exp["version_in_reason"] in (it.get("reason") or "")
                     or it.get("problems") for it in items)
            checks_item.append({"check": "version in reason", "ok": ok})

        if "not_wants_os" in exp:
            ok = not reqs.get("wants_os")
            checks_item.append({"check": "not wants_os", "ok": ok,
                                "got": reqs.get("wants_os")})

        if "no_windows_only" in exp:
            ok = all(_platform_ok(it) for it in items)
            checks_item.append({"check": "no windows-only", "ok": ok})

        results.append({
            "query": query,
            "count": top["count"],
            "shortfall": top["shortfall"],
            "checks": checks_item,
            "passed": all(c["ok"] for c in checks_item),
            "items_summary": [
                {"position": i, "name": it["name"][:50],
                 "role": it["role"], "price": it["price_text"],
                 "available": it["available_text"]}
                for i, it in enumerate(items, 1)
            ],
        })

    # ── проверки допродаж ─────────────────────────────────────────
    reqs_astra = extract_requirements("astra linux")
    upsell = []
    # совместимый
    compat = _first(conn, "name LIKE ? AND available=1",
                    ("%КриптоПро CSP для Astra Linux%",))
    upsell.append({
        "case": "совместимый (КриптоПро CSP для Astra Linux)",
        "found": compat is not None,
        "verified": verify_requirements(compat, reqs_astra)["verified"] if compat else None,
        "expected": True,
        "problems": verify_requirements(compat, reqs_astra)["problems"] if compat else [],
    })
    # несовместимый (только Windows)
    incompat = _first(conn, "lower(platform)='windows' AND name NOT LIKE ?",
                      ("%Astra%",))
    upsell.append({
        "case": "несовместимый (только Windows)",
        "found": incompat is not None,
        "verified": verify_requirements(incompat, reqs_astra)["verified"] if incompat else None,
        "expected": False,
        "problems": verify_requirements(incompat, reqs_astra)["problems"] if incompat else [],
    })
    # неизвестная характеристика (пустая платформа)
    unknown = _first(conn, "(platform IS NULL OR platform='') AND name NOT LIKE ?",
                     ("%Astra%",))
    upsell.append({
        "case": "неизвестная характеристика (нет данных платформы)",
        "found": unknown is not None,
        "verified": verify_requirements(unknown, reqs_astra)["verified"] if unknown else None,
        "expected": False,
        "problems": verify_requirements(unknown, reqs_astra)["problems"] if unknown else [],
    })

    upsell_ok = sum(1 for u in upsell if u.get("verified") == u["expected"])

    return {
        "results": results,
        "upsell": upsell,
        "passed": sum(1 for r in results if r["passed"]),
        "total": len(results),
        "upsell_passed": upsell_ok,
        "upsell_total": len(upsell),
    }


def _first(conn, where: str, params: tuple):
    """Первый offer, удовлетворяющий условию (для проверки допродаж)."""
    row = conn.execute(f"SELECT * FROM offers WHERE {where} LIMIT 1",
                       params).fetchone()
    return dict(row) if row else None


def _find_best_group(conn, name_pattern: str):
    """Найти лучший SKU по названию (для проверки is_os_offer)."""
    row = conn.execute(
        "SELECT * FROM offers WHERE name LIKE ? LIMIT 1",
        (f"%{name_pattern}%",)).fetchone()
    return dict(row) if row else None


def _platform_ok(item: dict) -> bool:
    """Нет ли в топ-4 SKU, который только Windows."""
    plat = normalize_text(item.get("platform") or "")
    if "windows" not in plat:
        return True
    # Если есть ещё и Linux/астра/другие ОС — не только Windows.
    return any(x in plat for x in ("linux", "астра", "astralinux",
                                    "redos", "альт", "рос", "unix", "macos"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка топ-4 и допродаж")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    conn = connect()
    data = run(conn)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    ok = 0
    for r in data["results"]:
        mark = "[PASS]" if r["passed"] else "[FAIL]"
        print(f"{mark} «{r['query']}» → {r['count']}/4")
        for c in r["checks"]:
            s = "ok" if c["ok"] else "НЕТ"
            print(f"       [{s}] {c['check']} (got: {c.get('got', '—')})")
        ok += 1 if r["passed"] else 0

    print(f"\nТоп-4 проверок пройдено: {ok}/{data['total']}")

    print(f"\nПроверки допродаж:")
    for u in data["upsell"]:
        mark = "[PASS]" if u.get("verified") == u["expected"] else "[FAIL]"
        print(f"  {mark} {u['case']} → "
              f"{'подтв.' if u.get('verified') else 'не подтв.'} "
              f"(ожидалось: {'подтв.' if u['expected'] else 'не подтв.'})")
        if u["problems"]:
            print(f"       причина: {'; '.join(u['problems'])}")
    print(f"\nДопродаж пройдено: {data['upsell_passed']}/{data['upsell_total']}")

    return 0 if ok == data["total"] and data["upsell_passed"] == data["upsell_total"] else 1


if __name__ == "__main__":
    sys.exit(main())
