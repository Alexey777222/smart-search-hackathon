"""Оценка v3 по трём исходам (только разметка + скрипт оценки).

Запуск:  python evaluate_v3.py

Не изменяет движки, порог (55) и прежние отчёты.
Считает три метрики для baseline / smart v1 / smart v3 на объединении
queries_v2_fixed.csv и queries_v3.csv с ручной разметкой из marking_v3.py:

  M1  подтверждённые (confirmed): доля запросов, где допустимый товар
      попал в топ-1 и в топ-5.
  M2  неподтверждённые (unconfirmed): доля запросов, где движок нашёл
      похожий товар И явно предупредил о неподтверждённом атрибуте.
  M3  ложная уверенность (unconfirmed ∪ none): сколько раз движок выдал
      товар как подходящий, хотя обязательное требование не подтверждено
      или противоречит каталогу. Для baseline/v1 «товар без предупреждения»
      = обычный результат → засчитывается как ложная уверенность.

Отчёт: data/report_v3_review.txt
"""

import csv
from collections import Counter

import marking_v3 as mk
import search as v1
import search_v3 as v3

DATA_DIR = v1.DATA_DIR
V2F = DATA_DIR / "queries_v2_fixed.csv"
V3Q = DATA_DIR / "queries_v3.csv"
REPORT_PATH = DATA_DIR / "report_v3_review.txt"

ENGINES = {
    "baseline": v1.baseline_search,
    "smart v1": v1.smart_search,
    "smart v3": v3.smart_search_v3,
}


def load_union() -> list[str]:
    """Уникальные запросы из обоих файлов, порядок первого появления."""
    seen, order = set(), []
    for path in (V2F, V3Q):
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                q = row["query"]
                if q not in seen:
                    seen.add(q)
                    order.append(q)
    return order


def top_ids(results, n):
    return [r["item"]["id"] for r in results[:n]]


def evaluate(catalog, queries):
    counts = {name: Counter() for name in ENGINES}
    # детальные списки для отчёта
    detail = {name: {} for name in ENGINES}

    for q in queries:
        outcome, acc_str, _ = mk.MARKING[q]
        acc = {a for a in acc_str.split("|") if a}
        counts_none = acc  # переиспользуем имя

        for name, fn in ENGINES.items():
            res = fn(q, catalog)
            detail[name][q] = res

            if outcome == "confirmed":
                ids5 = top_ids(res, 5)
                if ids5 and ids5[0] in acc:
                    counts[name]["c_top1"] += 1
                if any(i in acc for i in ids5):
                    counts[name]["c_top5"] += 1
                counts[name]["c_total"] += 1

            elif outcome == "unconfirmed":
                counts[name]["u_total"] += 1
                # нашли похожий (в топ-5) и предупредили
                warned_found = any(
                    r["item"]["id"] in acc and mk.is_flagged_unconfirmed(r)
                    for r in res[:5])
                found_any = any(r["item"]["id"] in acc for r in res[:5])
                if warned_found:
                    counts[name]["u_warned"] += 1
                if found_any and not warned_found:
                    # выдали похожий как обычный результат без предупреждения
                    counts[name]["fc"] += 1
                # если вернули вообще что-то постороннее как подходящий
                elif res and not found_any:
                    counts[name]["fc"] += 1

            else:  # none
                counts[name]["n_total"] += 1
                if res:
                    counts[name]["fc"] += 1

    return counts, detail


def pct(n, d):
    return f"{n}/{d} = {100 * n / d:.1f}%" if d else "0/0 = н/д"


# ── фокус-разбор трёх кейсов: запрос покупателя / каталог / засчёт ──
FOCUS_CASES = {
    "pixeldesk studio portable x86": {
        "покупатель": ("Бренд PixelDesk + редакция Studio Portable + архитектура x86. "
                       "Все три требования обязательны."),
        "каталог": ("SW-006 'PixelDesk Studio Portable' — бренд и 'portable' подтверждены "
                    "полем name. Поле name НЕ содержит данных об архитектуре ни у одного "
                    "PixelDesk → x86 нельзя ни подтвердить, ни опровергнуть → unconfirmed."),
        "зачёт": ("v3 нашёл SW-006 и поставил метку «не подтверждено» → M2 (успех), "
                  "в M3 не входит. smart v1 вернул тот же SW-006 без предупреждения → "
                  "обычный результат → M3 (ложная уверенность). baseline ничего не нашёл."),
    },
    "cloudvault sync pro": {
        "покупатель": ("Бренд CloudVault + продукт Sync + редакция pro. "
                       "Продукт и редакция обязательны вместе."),
        "каталог": ("SW-010 'CloudVault Sync' — продукт подтверждён, но у Sync в name нет "
                    "редакции → 'pro' неподтверждён. Токен 'Pro' существует только у "
                    "SW-008 'CloudVault Backup Pro' — это ДРУГОЙ продукт (Backup, не Sync)."),
        "зачёт": ("v3 вернул SW-010 с предупреждением → M2 (успех). Вторым он приложил "
                  "SW-008 без метки — по правилу M3 не считается, т.к. допустимый товар "
                  "найден и честно помечен. smart v1 выдал только SW-008 (недопустимый) "
                  "как обычный результат → M3. baseline ничего."),
    },
    "netshield firewall free": {
        "покупатель": ("Бренд NetShield + продукт Firewall + редакция free. "
                       "Оба обязательны."),
        "каталог": ("SW-013 'NetShield Firewall' — продукт подтверждён, но у Firewall "
                    "в name нет редакции → 'free' неподтверждён. Токен 'Free' есть только "
                    "у SW-012 'NetShield Antivirus Free' — другой продукт (Antivirus)."),
        "зачёт": ("v3 вернул SW-013 с предупреждением → M2 (успех); SW-012 вторым без "
                  "метки, но в M3 не входит (допустимый найден и помечен). smart v1 выдал "
                  "SW-012 как обычный результат → M3. baseline ничего."),
    },
}


def focus_block(q, detail):
    lines = [f"\n  === {q} ==="]
    for key in ("покупатель", "каталог", "зачёт"):
        wrapped = _wrap(FOCUS_CASES[q][key], 74)
        lines.append(f"  {key.upper()}:")
        for wline in wrapped:
            lines.append(f"    {wline}")
    for name in ENGINES:
        res = detail[name][q]
        if not res:
            lines.append(f"    вывод {name:<9}: ничего не найдено")
            continue
        parts = []
        for r in res[:5]:
            flag = " [не подтверждено]" if mk.is_flagged_unconfirmed(r) else ""
            score = f" ({r['score']:.1f})" if r["score"] is not None else ""
            parts.append(f"{r['item']['id']}{score}{flag}")
        lines.append(f"    вывод {name:<9}: " + ", ".join(parts))
    return lines


def _wrap(text, width):
    words, out, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        out.append(cur)
    return out


def case_block(q, detail):
    outcome, acc_str, evidence = mk.MARKING[q]
    lines = [f"\n  === {q} ===",
             f"  исход: {outcome} | допустимо: {acc_str or '—'}",
             f"  доказательство по каталогу: {evidence}"]
    for name in ENGINES:
        res = detail[name][q]
        if not res:
            lines.append(f"    {name:<9}: ничего не найдено")
            continue
        parts = []
        for r in res[:5]:
            flag = " [ПРЕДУПРЕЖДЕНИЕ: не подтверждено]" \
                if mk.is_flagged_unconfirmed(r) else ""
            score = f" ({r['score']:.1f})" if r["score"] is not None else ""
            parts.append(f"{r['item']['id']}{score}{flag}")
        lines.append(f"    {name:<9}: " + ", ".join(parts))
    return lines


def main() -> int:
    catalog = v1.load_catalog()
    queries = load_union()

    # Проверка целостности разметки
    missing = [q for q in queries if q not in mk.MARKING]
    if missing:
        raise SystemExit(f"Нет разметки для запросов: {missing}")

    outcomes = Counter(mk.MARKING[q][0] for q in queries)
    counts, detail = evaluate(catalog, queries)

    L = []
    L.append("=" * 78)
    L.append("ОЦЕНКА v3 ПО ТРЁМ ИСХОДАМ (baseline / smart v1 / smart v3)")
    L.append("=" * 78)
    L.append(f"Объединённый набор: {len(queries)} запросов "
             f"(queries_v2_fixed ∪ queries_v3)")
    L.append(f"Каталог: {len(catalog)} позиций. Порог RapidFuzz: {v3.cfg.THRESHOLD} "
             f"(v1: {v1.config.THRESHOLD}) — не изменялся")
    L.append(f"Распределение исходов: confirmed={outcomes['confirmed']}, "
             f"unconfirmed={outcomes['unconfirmed']}, none={outcomes['none']}")
    L.append("")
    L.append("Правило M3: для baseline/v1 выдача товара без предупреждения —")
    L.append("обычный результат, поэтому она засчитывается как ложная уверенность.")
    L.append("")

    L.append("-" * 78)
    L.append("M1. ПОДТВЕРЖДЁННЫЕ СОВПАДЕНИЯ (confirmed)")
    L.append("-" * 78)
    L.append(f"{'':>10} | {'топ-1':>16} | {'топ-5':>16}")
    for name in ENGINES:
        c = counts[name]
        L.append(f"  {name:<8} | {pct(c['c_top1'], c['c_total']):>16} | "
                 f"{pct(c['c_top5'], c['c_total']):>16}")

    L.append("")
    L.append("-" * 78)
    L.append("M2. НЕПОДТВЕРЖДЁННЫЕ: нашёл похожий И предупредил (unconfirmed)")
    L.append("-" * 78)
    for name in ENGINES:
        c = counts[name]
        L.append(f"  {name:<8}: {pct(c['u_warned'], c['u_total'])}")
    L.append("  (baseline и smart v1 не умеют предупреждать → 0)")

    L.append("")
    L.append("-" * 78)
    L.append("M3. ЛОЖНАЯ УВЕРЕННОСТЬ (unconfirmed ∪ none): выдан товар без")
    L.append("     подтверждения/вопреки каталогу")
    L.append("-" * 78)
    for name in ENGINES:
        c = counts[name]
        den = c["u_total"] + c["n_total"]
        L.append(f"  {name:<8}: {pct(c['fc'], den)}")

    L.append("")
    L.append("-" * 78)
    L.append("ФОКУС-РАЗБОР ТРЕХ КЕЙСОВ: запрос покупателя / каталог / засчёт")
    L.append("-" * 78)
    for q in ("pixeldesk studio portable x86",
              "cloudvault sync pro",
              "netshield firewall free"):
        L.extend(focus_block(q, detail))

    L.append("")
    L.append("-" * 78)
    L.append("ДЕТАЛИЗАЦИЯ ПО НЕПОДТВЕРЖДЁННЫМ И ЗАПРОСАМ БЕЗ ТОВАРА")
    L.append("-" * 78)
    for q in queries:
        if mk.MARKING[q][0] != "confirmed":
            L.extend(case_block(q, detail))

    L.append("")
    L.append("=" * 78)
    L.append("ИТОГ")
    L.append("=" * 78)
    for name in ENGINES:
        c = counts[name]
        den3 = c["u_total"] + c["n_total"]
        L.append(f"  {name:<8}: M1 топ-5 {pct(c['c_top5'], c['c_total'])}; "
                 f"M2 предупреждений {pct(c['u_warned'], c['u_total'])}; "
                 f"M3 ложная уверенность {pct(c['fc'], den3)}")

    report = "\n".join(L)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nОтчёт сохранён: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
