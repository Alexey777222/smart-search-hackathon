"""check_regress.py — поиск регрессий M1 по confirmed-запросам разметки."""

import marking_v3 as mk
import search_v3 as v3

CATALOG = v3.load_catalog()

broken = []
for q, (outcome, acc_str, _ev) in mk.MARKING.items():
    if outcome != "confirmed":
        continue
    acc = {a for a in acc_str.split("|") if a}
    res = v3.smart_search_v3(q, CATALOG)[:5]
    top1 = res[0]["item"]["id"] if res else None
    top5 = {r["item"]["id"] for r in res}
    if top1 not in acc:
        broken.append((q, acc, top1, sorted(top5)))

print(f"confirmed-запросов: "
      f"{sum(1 for v in mk.MARKING.values() if v[0] == 'confirmed')}")
print(f"сломанных топ-1: {len(broken)}\n")
for q, acc, top1, top5 in broken:
    print(f"  запрос   : {q!r}")
    print(f"  ожидалось: {sorted(acc)}  получено топ-1: {top1}  топ-5: {top5}")
    an = v3.analyze_query(q)
    print(f"  brand    : {an['brand']!r} ({an['brand_source']})")
    print(f"  requested: {an['requested']}")
    print(f"  transformed: {an['transformed']!r}")
    for r in v3.smart_search_v3(q, CATALOG):
        print(f"    {r['item']['id']} {r['item']['name']:<32} "
              f"score={r['score']} name_ok={r['name_confirmed']} "
              f"explicit_ok={r['explicit_ok']}")
        print(f"      {r['method']}")
    print()
