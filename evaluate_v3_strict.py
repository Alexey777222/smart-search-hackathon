"""Строгая оценка v3 по трём исходам (M3: любой товар без предупреждения).

Запуск:  python evaluate_v3_strict.py

Отличие от evaluate_v3.py:
  M3 засчитывается, если ХОТЬ ОДИН товар в топ-5 является неподходящим
  без предупреждения, ДАЖЕ когда другой результат в том же запросе
  правильно попал в M2.

Неподходящий товар (для M3):
  - outcome=none    → любой выданный товар без предупреждения;
  - outcome=unconfirmed:
      * товар не из expected_ids И без предупреждения (чужой товар);
      * товар из expected_ids, но БЕЗ предупреждения (движок не
        отметил, что обязательный атрибут неподтверждён).

Не меняет движки и порог. Сохраняет прежний data/report_v3_review.txt,
пишет новый data/report_v3_review_strict.txt.
"""

import csv
from collections import Counter

import marking_v3 as mk
import search as v1
import search_v3 as v3

DATA_DIR = v1.DATA_DIR
V2F = DATA_DIR / "queries_v2_fixed.csv"
V3Q = DATA_DIR / "queries_v3.csv"
REPORT_PATH = DATA_DIR / "report_v3_review_strict.txt"

ENGINES = {
    "baseline": v1.baseline_search,
    "smart v1": v1.smart_search,
    "smart v3": v3.smart_search_v3,
}


# ── загрузка ───────────────────────────────────────────────────────

def load_union() -> list[str]:
    seen, order = set(), []
    for path in (V2F, V3Q):
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                q = row["query"]
                if q not in seen:
                    seen.add(q)
                    order.append(q)
    return order


def load_v3_only() -> list[str]:
    a = {r["query"] for r in csv.DictReader(V2F.open(encoding="utf-8"))}
    b = [r["query"] for r in csv.DictReader(V3Q.open(encoding="utf-8"))]
    seen, out = set(), []
    for q in b:
        if q not in a and q not in seen:
            seen.add(q)
            out.append(q)
    return out


# ── классификация одного запроса ──────────────────────────────────

def classify(q: str, res: list[dict]) -> dict:
    """Возвращает {'m1_top1','m1_top5','m2','m3','m3_offenses'} для запроса."""
    outcome, acc_str, _ = mk.MARKING[q]
    acc = {a for a in acc_str.split("|") if a}
    res5 = res[:5]

    m1_top1 = m1_top5 = False
    m2 = False
    m3 = False
    offenses = []

    if outcome == "confirmed":
        if res5 and res5[0]["item"]["id"] in acc:
            m1_top1 = True
        if any(r["item"]["id"] in acc for r in res5):
            m1_top5 = True

    elif outcome == "unconfirmed":
        # M2: допустимый товар С предупреждением в топ-5
        m2 = any(r["item"]["id"] in acc and mk.is_flagged_unconfirmed(r)
                 for r in res5)
        # M3: любой товар без предупреждения
        for pos, r in enumerate(res5, 1):
            warned = mk.is_flagged_unconfirmed(r)
            iid = r["item"]["id"]
            if warned:
                continue
            if iid not in acc:
                m3 = True
                offenses.append((pos, iid, "чужой товар без предупреждения"))
            else:
                m3 = True
                offenses.append((pos, iid,
                    "допустимый товар, но без предупреждения "
                    "(движок не отметил неподтверждённый атрибут)"))

    else:  # none
        for pos, r in enumerate(res5, 1):
            if not mk.is_flagged_unconfirmed(r):
                m3 = True
                offenses.append((pos, r["item"]["id"],
                    "товар без предупреждения на запрос без подходящего товара"))

    return {"outcome": outcome, "expected": acc_str,
            "m1_top1": m1_top1, "m1_top5": m1_top5,
            "m2": m2, "m3": m3, "m3_offenses": offenses}


# ── прогон и подсчёт ──────────────────────────────────────────────

def evaluate(queries: list[str], catalog: list[dict]):
    per_engine = {name: {q: classify(q, fn(q, catalog)) for q in queries}
                  for name, fn in ENGINES.items()}
    return per_engine


def summarize(per_engine: dict, queries: list[str]) -> Counter:
    s = Counter()
    for q in queries:
        outcome = mk.MARKING[q][0]
        s[f"{outcome}_total"] += 1
        for name, res in per_engine.items():
            c = res[q]
            if outcome == "confirmed":
                if c["m1_top1"]:
                    s[f"{name}_c_top1"] += 1
                if c["m1_top5"]:
                    s[f"{name}_c_top5"] += 1
            elif outcome == "unconfirmed":
                if c["m2"]:
                    s[f"{name}_u_warned"] += 1
            if c["m3"]:
                s[f"{name}_m3"] += 1
    return s


def pct(n, d):
    return f"{n}/{d} = {100 * n / d:.1f}%" if d else "0/0 = н/д"


# ── печать секций ────────────────────────────────────────────────

def print_metric_table(L, per_engine, queries, tag):
    s = summarize(per_engine, queries)
    n_conf = sum(1 for q in queries if mk.MARKING[q][0] == "confirmed")
    n_unc = sum(1 for q in queries if mk.MARKING[q][0] == "unconfirmed")
    n_non = sum(1 for q in queries if mk.MARKING[q][0] == "none")

    L.append(f"  [distribution] confirmed={n_conf}, "
             f"unconfirmed={n_unc}, none={n_non}")
    L.append("")
    L.append(f"  M1 подтверждённые (confirmed), знаменатель = {n_conf}")
    L.append(f"  {'engine':<10} | {'топ-1':>16} | {'топ-5':>16}")
    for name in ENGINES:
        L.append(f"  {name:<10} | {pct(s[f'{name}_c_top1'], n_conf):>16} | "
                 f"{pct(s[f'{name}_c_top5'], n_conf):>16}")
    L.append("")
    L.append(f"  M2 нашёл похожий и предупредил (unconfirmed), "
             f"знаменатель = {n_unc}")
    for name in ENGINES:
        L.append(f"  {name:<10}: {pct(s[f'{name}_u_warned'], n_unc)}")
    L.append("")
    den3 = n_unc + n_non
    L.append(f"  M3 ложная уверенность (unconfirmed ∪ none), "
             f"знаменатель = {den3}")
    for name in ENGINES:
        L.append(f"  {name:<10}: {pct(s[f'{name}_m3'], den3)}")
    return s


def print_unconfirmed_none_detail(L, per_engine, queries):
    L.append("")
    L.append("-" * 78)
    L.append("ДЕТАЛИЗАЦИЯ ПО 15 ЗАПРОСАМ unconfirmed + none (union)")
    L.append("-" * 78)
    for q in queries:
        outcome, acc_str, evidence = mk.MARKING[q]
        if outcome == "confirmed":
            continue
        L.append("")
        L.append(f"  === {q} ===")
        L.append(f"  исход: {outcome} | допустимо: {acc_str or '—'}")
        L.append(f"  доказательство: {evidence}")
        for name in ENGINES:
            c = per_engine[name][q]
            res = ENGINES[name](q, _CATALOG)[:5]
            if not res:
                L.append(f"    {name:<9}: ничего не найдено")
                continue
            parts = []
            for pos, r in enumerate(res, 1):
                warned = mk.is_flagged_unconfirmed(r)
                tag = " [не подтверждено]" if warned else ""
                score = f" ({r['score']:.1f})" if r["score"] is not None else ""
                mark = "M3✗" if pos in [p for p, _, _ in c["m3_offenses"]] else ""
                parts.append(f"  {pos}. {r['item']['id']}{score}{tag} {mark}")
            status = []
            if c["m2"]:
                status.append("M2✓")
            if c["m3"]:
                status.append("M3✗")
            if not status:
                status.append("—")
            L.append(f"    {name:<9} [{', '.join(status)}]:")
            for p in parts:
                L.append(f"      {p}")
            for pos, iid, why in c["m3_offenses"]:
                L.append(f"        → позиция {pos}: {iid} — {why}")


def print_v3_only_section(L, per_engine, queries_v3_only, catalog):
    L.append("")
    L.append("=" * 78)
    L.append("ОТДЕЛЬНЫЙ СРЕЗ: 15 запросов, уникальных для queries_v3.csv")
    L.append("=" * 78)
    L.append(f"Запросы: {len(queries_v3_only)}")
    L.append("")
    for q in queries_v3_only:
        outcome, acc_str, _ = mk.MARKING[q]
        L.append(f"  [{outcome:>11}] {q}  (допустимо: {acc_str or '—'})")
    L.append("")
    print_metric_table(L, per_engine, queries_v3_only, "v3-only")

    L.append("")
    L.append("Ошибки M3 в этом срезе (позиция + ID):")
    any_err = False
    for q in queries_v3_only:
        for name in ENGINES:
            c = per_engine[name][q]
            for pos, iid, why in c["m3_offenses"]:
                any_err = True
                L.append(f"  {name:<9} | {q} → позиция {pos}: {iid} — {why}")
    if not any_err:
        L.append("  нет")


# ── главный ──────────────────────────────────────────────────────

_CATALOG = v1.load_catalog()


def main() -> int:
    queries = load_union()
    queries_v3_only = load_v3_only()

    per_engine = evaluate(queries, _CATALOG)

    L = []
    L.append("=" * 78)
    L.append("ОЦЕНКА v3 (СТРОГИЙ M3): baseline / smart v1 / smart v3")
    L.append("=" * 78)
    L.append(f"Объединённый набор: {len(queries)} запросов "
             f"(queries_v2_fixed ∪ queries_v3)")
    L.append(f"Каталог: {len(_CATALOG)} позиций. Порог: v1={v1.config.THRESHOLD}, "
             f"v3={v3.cfg.THRESHOLD} — не изменялся")
    L.append("")
    L.append("Правило строгого M3: если ХОТЬ ОДИН товар в топ-5")
    L.append("неподходящий без предупреждения — запрос засчитывается в M3,")
    L.append("даже когда другой товар в том же запросе попал в M2.")
    L.append("")
    L.append("-" * 78)
    L.append("МЕТРИКИ НА ОБЪЕДИНЁННОМ НАБОРЕ")
    L.append("-" * 78)
    print_metric_table(L, per_engine, queries, "union")

    print_unconfirmed_none_detail(L, per_engine, queries)
    print_v3_only_section(L, per_engine, queries_v3_only, _CATALOG)

    report = "\n".join(L)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nОтчёт сохранён: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
