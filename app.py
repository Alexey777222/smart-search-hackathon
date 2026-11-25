"""app.py — сквозной сценарий: пользовательский поиск с записью в журнал.

Запуск:
    python app.py "текст запроса"
    python app.py "медиаплей" --interest "аудио"
    python app.py "медиаплей" --viewed SW-019,SW-018
    python app.py "медиаплей" --profile data/profile_example.json
    python app.py "..." --journal data/demo_journal.jsonl   # временный журнал
    python app.py "таскф" --suggest                         # подсказки по мере ввода

Один ПОИСК = одна запись в журнал.
Подсказки (--suggest) — это НЕ поиск: они НЕ пишут журнал, иначе каждый
введённый символ завышал бы спрос.
Контрольные прогоны (evaluate*.py) используют search_v3 напрямую и НЕ пишут журнал.

Приоритет контекста: --interest/--viewed (CLI) перекрывают файл профиля;
без контекста поиск работает ровно как раньше.
"""

import argparse
import json
import sys
from pathlib import Path

import search_v3
import suggest
from search_log import (record_search, resolve_journal_path,
                        _correction_line, _unconfirmed_line)


def load_profile(path: str) -> dict:
    """Читает тестовый профиль: {"interest": "...", "viewed": ["SW-..."]}."""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Профиль не найден: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return {"interest": data.get("interest", ""),
            "viewed": list(data.get("viewed", [])),
            "source": f"profile:{p.name}"}


def build_context(args) -> tuple[dict | None, str]:
    """Собирает контекст из CLI/профиля. Возвращает (context, описание)."""
    ctx = {"interest": "", "viewed": [], "source": "none"}

    profile_path = getattr(args, "profile", None)
    if profile_path:
        ctx = load_profile(profile_path)

    if getattr(args, "interest", None):
        ctx["interest"] = args.interest
        ctx["source"] = "cli"
    if getattr(args, "viewed", None):
        ids = [v.strip() for v in args.viewed.split(",") if v.strip()]
        if ids:
            ctx["viewed"] = ids
            ctx["source"] = "cli"

    parts = []
    if ctx["interest"]:
        parts.append(f"интерес «{ctx['interest']}»")
    if ctx["viewed"]:
        parts.append("просмотрено " + ", ".join(ctx["viewed"]))
    return (ctx if parts else None), " | ".join(parts)


def run_search(query: str, context_desc: str = "",
               context: dict | None = None,
               journal_path=None,
               metrics=None,
               metrics_path=None) -> tuple[list[dict], dict, dict]:
    """Выполняет поиск, записывает ОДНО событие, возвращает
    (results, analysis, report) report["conflict_excluded"] etc."""
    catalog = search_v3.load_catalog()
    nq = search_v3.normalize(query)

    # Единственный вызов движка (внутри может быть несколько проходов —
    # для журнала это ОДНО событие).
    report: dict = {}
    results = search_v3.smart_search_v3(query, catalog, context, metrics, report)
    analysis = search_v3.analyze_query(query)

    commerce_info: dict = {"enabled": metrics is not None,
                           "source": str(metrics_path or ("default" if metrics else "none"))}
    if metrics:
        commerce_info["items_count"] = len(metrics)

    record_search(
        raw_query=query,
        normalized_query=nq,
        context=context_desc,
        results=results,
        correction=_correction_line(analysis),
        unconfirmed=_unconfirmed_line(results),
        analysis=analysis,
        context_info=(context or {"interest": "", "viewed": [],
                                  "source": "none"}),
        journal_path=journal_path,
        commerce_info=commerce_info,
    )
    return results, analysis, report


def print_results(results: list[dict], context: dict | None,
                  metrics: dict | None = None,
                  report: dict | None = None) -> None:
    """Вывод результатов с объяснением для пользователя."""
    if not results:
        print("  Ничего не найдено.")
    for pos, r in enumerate(results, 1):
        score = (f"  (сходство: {r['score']:.1f}/100)"
                 if r["score"] is not None else "")
        print(f"  {pos}. {r['item']['name']} [{r['item']['id']}]{score}")
        print(f"     Объяснение: {r['method']}")
        if context and r.get("context_signals"):
            print(f"     Признак контекста: {', '.join(r['context_signals'])}")
        if metrics and r.get("metrics"):
            m = r["metrics"]
            p = m.get("purchases_30d")
            g = m.get("gross_margin_pct")
            print(f"     Показатели (синтетика): "
                  f"покупок 30д {'?' if p is None else f'{p:.0f}'}; "
                  f"маржа {'?' if g is None else f'{g:.0f}%'}")
        if r.get("commerce_note"):
            print(f"     Коммерческая причина: {r['commerce_note']}")

    # Если контекст задан, но ни один товар его не подсветил — объясним.
    if context and results and not any(r.get("context_signals") for r in results):
        ctx_desc = []
        if context.get("interest"):
            ctx_desc.append(f"интерес «{context['interest']}»")
        if context.get("viewed"):
            ctx_desc.append("история просмотров")
        print(f"     Контекст ({', '.join(ctx_desc)}) задан, но "
              f"явно запрошенные продукт/назначение приоритетнее — порядок не изменён.")

    # Товары, исключённые из выдачи по конфликту обязательного атрибута.
    if report and report.get("conflict_excluded"):
        for ex in report["conflict_excluded"]:
            print(f"  ✗ Исключён (конфликт обязательного атрибута, "
                  f"до коммерческого слоя): {ex['name']} [{ex['id']}] — {ex['why']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Умный поиск: пользовательский запрос + запись в журнал")
    parser.add_argument("query", help="текст запроса")
    parser.add_argument("--context", default="",
                        help="произвольная метка контекста для журнала")
    parser.add_argument("--interest", default="",
                        help="категория интереса (media/audio/video/security...)")
    parser.add_argument("--viewed", default="",
                        help="ID просмотренных товаров через запятую")
    parser.add_argument("--profile", default="",
                        help="файл тестового профиля (JSON)")
    parser.add_argument("--journal", default="",
                        help="путь к журналу (по умолчанию data/search_journal.jsonl)")
    parser.add_argument("--history", default="",
                        help="файл журнала для истории подсказок (JSONL)")
    parser.add_argument("--suggest", action="store_true",
                        help="показать подсказки по введённому тексту (не поиск)")
    parser.add_argument("--with-metrics", action="store_true",
                        help="применить коммерческие показатели (ДЕМО-ДАННЫЕ "
                             "СИНТЕТИЧЕСКИЕ) из data/metrics_demo.csv")
    parser.add_argument("--metrics", default="",
                        help="путь к CSV с показателями (включает --with-metrics)")
    parser.add_argument("--compare-metrics", action="store_true",
                        help="показать выдачу без показателей и с показателями "
                             "рядом; в журнал пишет ОДНУ запись (с метриками)")
    args = parser.parse_args(argv)

    # ── режим подсказок: НЕ пишет журнал ─────────────────────────
    if args.suggest:
        catalog = search_v3.load_catalog()
        history = []
        hist_path = None
        if args.history:
            hist_path = Path(args.history)
            if hist_path.exists():
                for line in hist_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        entry = json.loads(line)
                        if entry.get("raw"):
                            history.append(entry["raw"])
        # Если читаем общий журнал — метка источника «Из истории запросов магазина».
        hist_label = ""
        if history:
            hist_label = "Из истории запросов магазина"
        suggestions = suggest.get_suggestions(args.query, catalog,
                                              history=history or None,
                                              history_label=hist_label)
        print(f"Подсказки для: {args.query}")
        if not suggestions:
            print("  нет подходящих подсказок")
        else:
            for i, s in enumerate(suggestions, 1):
                if s["id"]:
                    print(f"  {i}. {s['text']} [{s['id']}]")
                else:
                    print(f"  {i}. {s['text']}")
                print(f"     Причина: {s['reason']}")
                print(f"     Источник: {s['source']}")
        print(f"\n  (подсказки не записываются в журнал — "
              f"иначе каждый введённый символ завысит спрос)")
        if history:
            print(f"  история из {hist_path}: {len(history)} запросов")
        return 0

    # ── обычный поиск + запись в журнал ──────────────────────────
    context, context_desc = build_context(args)
    if args.context:
        context_desc = (f"{context_desc} | {args.context}"
                        if context_desc else args.context)

    journal = resolve_journal_path(args.journal or None)

    # Метрики: по умолчанию НЕ применяются (обычный запуск — как раньше).
    metrics_path = args.metrics or None
    use_metrics = args.with_metrics or bool(args.metrics) or args.compare_metrics
    metrics = search_v3.load_metrics(
        metrics_path or search_v3.METRICS_PATH) if use_metrics else None

    print(f"Запрос: {args.query}")
    if context_desc:
        print(f"Контекст: {context_desc}")
    if metrics:
        print(f"Коммерческие показатели: ДЕМО-ДАННЫЕ СИНТЕТИЧЕСКИЕ "
              f"({metrics_path or search_v3.METRICS_PATH})")

    if args.compare_metrics:
        # Сравнение «рядом»: два прогона движка, ОДИН поиск в журнале.
        catalog = search_v3.load_catalog()
        rep_no: dict = {}
        res_no = search_v3.smart_search_v3(args.query, catalog, context,
                                           None, rep_no)
        rep_m: dict = {}
        res_m = search_v3.smart_search_v3(args.query, catalog, context,
                                          metrics, rep_m)
        print("\n--- без коммерческих показателей (обычный порядок) ---")
        print_results(res_no, context, None, rep_no)
        print("\n--- с коммерческими показателями (ДЕМО-ДАННЫЕ СИНТЕТИЧЕСКИЕ) ---")
        print_results(res_m, context, metrics, rep_m)
        moved = [(p, r["item"]["id"]) for p, r in enumerate(res_m, 1)
                 if r.get("commerce_note")]
        if moved:
            print("\nИтог: коммерческий слой изменил порядок на "
                  f"{len(moved)} позициях (см. причины выше).")
        else:
            print("\nИтог: показатели не изменили порядок "
                  "(нет сопоставимых товаров с различающимися метриками).")
        # В журнал — одна запись (финальный прогон с метриками).
        analysis = search_v3.analyze_query(args.query)
        record_search(
            raw_query=args.query,
            normalized_query=search_v3.normalize(args.query),
            context=context_desc,
            results=res_m,
            correction=_correction_line(analysis),
            unconfirmed=_unconfirmed_line(res_m),
            analysis=analysis,
            context_info=(context or {"interest": "", "viewed": [],
                                      "source": "none"}),
            journal_path=journal,
            commerce_info={"enabled": True, "mode": "compare",
                           "source": str(metrics_path or search_v3.METRICS_PATH),
                           "items_count": len(metrics)},
        )
        print(f"\n✓ Поиск записан в журнал (одна запись): {journal}")
        return 0

    results, _, report = run_search(args.query, context_desc, context,
                                    journal, metrics, metrics_path)
    print_results(results, context, metrics, report)
    print(f"\n✓ Поиск записан в журнал: {journal}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
