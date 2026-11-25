"""report.py — отчёт менеджера по журналу пользовательских поисков.

Запуск:
    python report.py
    python report.py --journal data/demo_journal.jsonl

Разделы:
  0. Счётчики показов карточек по всем выведенным позициям;
  1. Частые запросы без подходящего результата → «проверить наличие товара»;
  2. Рекомендации по карточкам → «дополнить существующую карточку»;
  3. Фактические исправления запроса → «проверить качество исправлений»;
  4. Похожие товары без переписывания запроса → задание НЕ создаётся.

Правило раздела 2 (без привязки к конкретным ID):
  задание на доработку карточки получает только та позиция, для которой
  ПОДТВЕРЖДЕНО запрошенное название/линейка (все продуктовые слова запроса
  есть в названии товара) И известен конкретный пробел — значение атрибута,
  которое каталог не смог подтвердить. Товар, показанный «вторым вариантом»
  и не подтверждающий запрошенную линейку, задание не создаёт.

Старые строки JSONL (без полей transforms/gaps) читаются: для них пробелы
и исправления пересчитываются текущим движком.
"""

import argparse
import sys
from collections import Counter, defaultdict

import search_v3
from search_log import read_journal, read_orders, resolve_journal_path

CATALOG = search_v3.load_catalog()


# ── извлечение данных из строки журнала (новый и старый формат) ───

def entry_analysis(entry: dict) -> dict:
    """Структурные изменения запроса: из записи или пересчётом движка."""
    tr = entry.get("transforms")
    if tr:
        return tr
    return search_v3.analyze_query(entry.get("raw", ""))


def entry_positions(entry: dict) -> list[dict]:
    """Позиции выдачи с gaps и name_confirmed (пересчёт для старых строк)."""
    results = entry.get("results", [])
    if not results:
        return []
    if all("gaps" in r for r in results):
        return results
    # Старый формат: пересчитываем текущим движком по исходному запросу.
    fresh = search_v3.smart_search_v3(entry.get("raw", ""), CATALOG)
    by_id = {r["item"]["id"]: r for r in fresh}
    out = []
    for r in results:
        f = by_id.get(r["id"])
        out.append({
            "position": r["position"],
            "id": r["id"],
            "name": r["name"],
            "gaps": (f.get("gaps", []) if f else []),
            "name_confirmed": (f.get("name_confirmed", True) if f
                               else r.get("name_confirmed", True)),
        })
    return out


def real_fixes(analysis: dict) -> list[dict]:
    """Только фактические переписывания текста: опечатки, сокращения, термины.

    Распознанный бренд (в т.ч. «бренд: pixeldesk») и замены вида
    firewall → firewall сюда НЕ попадают.
    """
    out = []
    for t in analysis.get("typo_fixes", []) or []:
        out.append({**t, "type": "опечатка"})
    for t in analysis.get("abbreviations", []) or []:
        out.append({**t, "type": "сокращение"})
    for t in analysis.get("other_transforms", []) or []:
        if t["dst"] and t["dst"] != t["src"]:
            out.append({**t, "type": "термин" if t["kind"] == "term" else "транслит"})
    return out


# ── сборка отчёта ─────────────────────────────────────────────────

def build_report(entries: list[dict], orders: list[dict] | None = None) -> str:
    if orders is None:
        orders = []
    L = []
    L.append("=" * 70)
    L.append("ОТЧЁТ МЕНЕДЖЕРА ПО ЖУРНАЛУ ПОИСКОВ")
    L.append("=" * 70)
    L.append(f"Записей в журнале: {len(entries)}")
    L.append("")

    # ── 0. Показы карточек по всем выведенным позициям ────────────
    card_counters: Counter = Counter()
    card_names: dict[str, str] = {}
    for e in entries:
        for r in e.get("results", []):
            card_counters[r["id"]] += 1
            card_names[r["id"]] = r["name"]

    L.append("─" * 70)
    L.append("СЧЁТЧИКИ ПОКАЗОВ КАРТОЧЕК (по всем выведенным позициям)")
    L.append("─" * 70)
    if card_counters:
        for cid, count in card_counters.most_common():
            L.append(f"  {cid}  {card_names.get(cid, ''):<32} ×{count}")
    else:
        L.append("  (нет попаданий)")
    L.append("")

    # ── 1. Запросы без подходящего результата ─────────────────────
    no_result: Counter = Counter()
    no_result_example: dict[str, str] = {}
    no_result_ctx: dict[str, str] = {}
    for e in entries:
        if not e.get("results"):
            nq = e["norm"]
            no_result[nq] += 1
            if nq not in no_result_example:
                no_result_example[nq] = e["raw"]
                no_result_ctx[nq] = e.get("ctx", "")

    L.append("─" * 70)
    L.append("1. ЧАСТЫЕ ЗАПРОСЫ БЕЗ ПОДХОДЯЩЕГО РЕЗУЛЬТАТА")
    L.append("   Действие: проверить наличие товара в ассортименте")
    L.append("─" * 70)
    if no_result:
        L.append(f"  {'Поисков':<9}{'Пример запроса':<38}{'Контекст':<16}Действие")
        for nq, cnt in no_result.most_common(10):
            raw = no_result_example[nq]
            ctx = no_result_ctx.get(nq, "") or "—"
            # Действие называет сам запрос: строка отчёта читается как
            # готовая задача менеджеру (запрос + частота + действие).
            action = f"проверить наличие «{raw}» в ассортименте"
            L.append(f"  {cnt:<9}{raw[:36]:<38}{ctx[:14]:<16}{action}")
    else:
        L.append("  (нет записей)")
    L.append("")

    # ── 2. Рекомендации по карточкам ──────────────────────────────
    # Ключ (id, kind, value) — задание только для подтверждённой линейки.
    tasks: dict[tuple, dict] = defaultdict(
        lambda: {"count": 0, "example": "", "name": "", "field": ""})
    for e in entries:
        for r in entry_positions(e):
            if not r.get("name_confirmed", True):
                # Показан как похожий вариант, но запрошенную линейку
                # не подтверждает — задание на доработку НЕ создаём.
                continue
            for gap in r.get("gaps", []):
                key = (r["id"], gap["kind"], gap["value"])
                t = tasks[key]
                t["count"] += 1
                t["name"] = r["name"]
                t["field"] = gap["field"]
                if not t["example"]:
                    t["example"] = e["raw"]

    L.append("─" * 70)
    L.append("2. КАРТОЧКИ С НЕДОСТАЮЩИМИ ХАРАКТЕРИСТИКАМИ")
    L.append("   Действие: дополнить существующую карточку")
    L.append("─" * 70)
    if tasks:
        for (cid, kind, value), t in sorted(tasks.items()):
            verb = search_v3.cfg.ATTRIBUTE_CHECK_VERB[kind].format(value=value)
            L.append(f"  Карточка: {cid} {t['name']}")
            L.append(f"  Поисков с этим пробелом: {t['count']}")
            L.append(f"  Пример запроса: \"{t['example']}\"")
            L.append(f"  Действие: {verb} у {t['name']} ({cid}) "
                     f"и заполнить поле «{t['field']}»")
            L.append("")
    else:
        L.append("  (нет записей)")
    L.append("")

    # ── 3. Фактические исправления запроса ────────────────────────
    fix_rows: dict[str, dict] = {}
    fix_count: Counter = Counter()
    for e in entries:
        if not e.get("results"):
            continue
        analysis = entry_analysis(e)
        fixes = real_fixes(analysis)
        if not fixes:
            continue
        nq = e["norm"]
        fix_count[nq] += 1
        if nq not in fix_rows:
            fix_rows[nq] = {
                "raw": e["raw"],
                "fixes": fixes,
                "brand": analysis.get("brand"),
                "brand_source": analysis.get("brand_source"),
            }

    L.append("─" * 70)
    L.append("3. ТОВАР НАЙДЕН ПОСЛЕ ИСПРАВЛЕНИЯ ЗАПРОСА")
    L.append("   Действие: проверить качество исправлений")
    L.append("─" * 70)
    if fix_rows:
        L.append(f"  {'Поисков':<9}{'Пример запроса':<34}Исправлено")
        for nq, cnt in fix_count.most_common(10):
            info = fix_rows[nq]
            desc = "; ".join(f"{f['type']} «{f['src']}» → «{f['dst']}»"
                             for f in info["fixes"])
            L.append(f"  {cnt:<9}{info['raw'][:32]:<34}{desc}")
    else:
        L.append("  (нет фактических исправлений запроса)")
    L.append("")

    # ── 4. Похожий товар без переписывания запроса ────────────────
    similar_only: Counter = Counter()
    similar_example: dict[str, dict] = {}
    for e in entries:
        if not e.get("results"):
            continue
        analysis = entry_analysis(e)
        if real_fixes(analysis):
            continue
        nq = e["norm"]
        similar_only[nq] += 1
        if nq not in similar_example:
            similar_example[nq] = {
                "raw": e["raw"],
                "brand": analysis.get("brand"),
                "brand_source": analysis.get("brand_source"),
            }

    L.append("─" * 70)
    L.append("4. ТОВАР НАЙДЕН КАК ПОХОЖИЙ, ТЕКСТ ЗАПРОСА НЕ ПЕРЕПИСЫВАЛСЯ")
    L.append("   Действие: исправлений нет — задание на проверку не создаётся")
    L.append("─" * 70)
    if similar_only:
        L.append(f"  {'Поисков':<9}{'Пример запроса':<34}Как найдено")
        for nq, cnt in similar_only.most_common(10):
            info = similar_example[nq]
            if info["brand"]:
                how = f"бренд распознан ({info['brand_source']}): {info['brand']}"
            else:
                how = "нечёткое совпадение по названию"
            L.append(f"  {cnt:<9}{info['raw'][:32]:<34}{how}")
    else:
        L.append("  (нет записей)")
    L.append("")

    # ── 5. От поиска до заказа ───────────────────────────────────
    L.append("─" * 70)
    L.append("5. ОТ ПОИСКА ДО ЗАКАЗА")
    L.append("   Источник: Учебные данные")
    L.append("─" * 70)

    # Разделяем записи журнала на «с session_id» и «без».
    entries_with_sid = [e for e in entries if e.get("session_id")]
    entries_without_sid = [e for e in entries if not e.get("session_id")]

    # Все поиски идут в статистику поисков (разделы 1-4 выше).
    total_searches = len(entries)
    empty_searches = sum(1 for e in entries if not e.get("results"))

    # Показатель 1: доля поисков с пустой выдачей.
    L.append("")
    L.append(f"  Доля поисков с пустой выдачей:")
    if total_searches:
        pct = 100.0 * empty_searches / total_searches
        L.append(f"    {empty_searches}/{total_searches} = {pct:.1f}%")
    else:
        L.append(f"    недостаточно данных")

    # Сессии для анализа по session_id.
    sessions: dict[str, list[dict]] = defaultdict(list)
    for e in entries_with_sid:
        sessions[e["session_id"]].append(e)

    # Заказы: группируем по session_id.
    orders_by_session: dict[str, list[dict]] = defaultdict(list)
    for o in orders:
        sid = o.get("session_id", "")
        if sid:
            orders_by_session[sid].append(o)

    # Показатель 2: доля сессий, закончившихся на пустой выдаче,
    # среди сессий, где была хотя бы одна пустая выдача.
    sessions_with_empty = 0
    sessions_ended_empty = 0
    for sid, sess_entries in sessions.items():
        has_empty = any(not e.get("results") for e in sess_entries)
        if has_empty:
            sessions_with_empty += 1
            # Последняя запись сессии (сортировка по ts)
            last = max(sess_entries, key=lambda e: e.get("ts", ""))
            if not last.get("results"):
                sessions_ended_empty += 1

    L.append("")
    L.append(f"  Доля сессий, закончившихся на пустой выдаче")
    L.append(f"  (среди сессий, где была пустая выдача):")
    if sessions_with_empty:
        pct = 100.0 * sessions_ended_empty / sessions_with_empty
        L.append(f"    {sessions_ended_empty}/{sessions_with_empty} = {pct:.1f}%")
    else:
        L.append(f"    недостаточно данных")

    # Показатель 3: конверсия поисковых сессий в заказ.
    # Сессия засчитывается как сконвертированная, если в ней есть
    # заказ ПОСЛЕ поиска. Несколько заказов в одной сессии = 1.
    sessions_with_search = len(sessions)
    sessions_converted = 0
    for sid in sessions:
        if sid in orders_by_session:
            # Проверяем, что хотя бы один заказ произошёл после
            # хотя бы одного поиска в этой сессии.
            last_search_ts = max(e.get("ts", "") for e in sessions[sid])
            for order in orders_by_session[sid]:
                if order.get("ts", "") >= last_search_ts:
                    sessions_converted += 1
                    break  # не увеличиваем при нескольких заказах

    L.append("")
    L.append(f"  Конверсия поисковых сессий в заказ:")
    if sessions_with_search:
        pct = 100.0 * sessions_converted / sessions_with_search
        L.append(f"    {sessions_converted}/{sessions_with_search} = {pct:.1f}%")
    else:
        L.append(f"    недостаточно данных")

    # Доп. информация: сколько записей без session_id.
    if entries_without_sid:
        L.append("")
        L.append(f"  Примечание: {len(entries_without_sid)} записей журнала")
        L.append(f"  без session_id — учтены в статистике поисков,")
        L.append(f"  но не включены в расчёты по сессиям.")

    # Детализация сессий (для учебного прогона).
    if sessions:
        L.append("")
        L.append("  Детализация сессий:")
        for sid in sorted(sessions.keys()):
            sess_entries = sessions[sid]
            has_order = sid in orders_by_session
            last = max(sess_entries, key=lambda e: e.get("ts", ""))
            last_empty = not last.get("results")
            searches_count = len(sess_entries)
            empty_count = sum(1 for e in sess_entries if not e.get("results"))
            order_mark = "✓ заказ" if has_order else "— без заказа"
            end_mark = "пустая выдача" if last_empty else "успешная выдача"
            L.append(f"    {sid}: {searches_count} поисков"
                     f" (пустых: {empty_count}), конец: {end_mark}, {order_mark}")

    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Отчёт менеджера по журналу пользовательских поисков")
    parser.add_argument("--journal", default="",
                        help="путь к журналу (по умолчанию data/search_journal.jsonl)")
    parser.add_argument("--orders", default="",
                        help="путь к журналу заказов (по умолчанию data/orders_demo.jsonl)")
    args = parser.parse_args(argv)

    journal = resolve_journal_path(args.journal or None)
    entries = read_journal(journal)
    orders = read_orders(args.orders or None)
    print(f"Журнал: {journal}")
    if orders:
        print(f"Заказы: {args.orders or 'data/orders_demo.jsonl'}")
    print()
    print(build_report(entries, orders))
    return 0


if __name__ == "__main__":
    sys.exit(main())
