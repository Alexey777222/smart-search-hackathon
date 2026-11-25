"""demo_scenario.py — сценарий защиты, замеры и расчёт стоимости.

Что делает:
  1. прогоняет сценарий «Нужен сервер» → «…Astra Linux 1.7, нужно резервное
     копирование» и печатает ответы, карточки, основания;
  2. проверяет, что вторая сессия получает чистый контекст;
  3. замеряет задержку обычного поиска (локально, без внешних вызовов);
  4. показывает задержку, число вызовов модели и токены ИИ-диалога;
  5. считает стоимость обычного поиска и ИИ-диалога и прогноз на 500
     поисков в день.

Без YANDEX_API_KEY работает имитация провайдера: токены = 0, стоимость
диалога = 0. Живые цифры появятся после подключения ключа — скрипт
печатает те же поля и для живого режима.

Запуск:
  python demo_scenario.py
  python demo_scenario.py --live     # с ключом из окружения
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

from ai_consultant import (Catalog, Consultant, MAX_TOKENS,
                           PRICE_PER_1K_TOKENS_RUB, SYSTEM_PROMPT, TOOLS)
from organizer_index import connect

SEARCH_QUERIES = [
    "astra linux", "кибер бэкап", "anydesk", "сервер", "мтс",
    "max patrol", "крипто про csp 5.0", "сканер-вс", "365", "anydesk дополнительные устройства",
]

SCENARIO = [
    "Нужен сервер",
    "Сервер уже есть, на нём Astra Linux 1.7. Нужно резервное копирование",
]


def _fmt_rub(value: float) -> str:
    return f"{value:,.2f} ₽".replace(",", " ")


def measure_search(conn, repeats: int = 3) -> dict:
    """Задержка обычного поиска: только локальный индекс, без сети."""
    timings = []
    for _ in range(repeats):
        for query in SEARCH_QUERIES:
            started = time.perf_counter()
            from organizer_search import search
            search(conn, query, limit=12)
            timings.append((time.perf_counter() - started) * 1000)
    return {
        "queries": len(SEARCH_QUERIES) * repeats,
        "avg_ms": round(statistics.mean(timings), 1),
        "p95_ms": round(sorted(timings)[int(len(timings) * 0.95) - 1], 1),
        "max_ms": round(max(timings), 1),
    }


def run_scenario(consultant: Consultant) -> list[dict]:
    session = consultant.new_session()
    turns = []
    for message in SCENARIO:
        reply = consultant.ask(session.id, message)
        turns.append({"message": message, **reply})
    return turns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сценарий защиты и замеры")
    parser.add_argument("--live", action="store_true",
                        help="использовать ключ из окружения (если он есть)")
    args = parser.parse_args(argv)

    catalog = Catalog()
    consultant = Consultant(catalog)
    if args.live:
        consultant.provider = __import__("ai_consultant").YandexProvider()
        if not consultant.provider.ready:
            print("Ключ не найден: работаю в режиме имитации провайдера.")

    live = consultant.provider.name == "yandexgpt"
    print("=" * 100)
    print("СЦЕНАРИЙ ЗАЩИТЫ")
    print("=" * 100)
    print(f"Провайдер ИИ: {consultant.provider.name}"
          + ("" if live else " (имитация: ключа нет, живой вызов ещё предстоит проверить)"))
    print()

    turns = run_scenario(consultant)
    for i, turn in enumerate(turns, 1):
        print(f"--- Реплика {i} ---")
        print(f"Пользователь: {turn['message']}")
        print(f"Консультант: {turn['answer']}")
        print(f"Карточек: {len(turn['cards'])}")
        for card in turn["cards"][:4]:
            print(f"  · [{card['id']}] {card['name'][:70]}")
            print(f"    тип: {card['type_label']} | {card['price_text']} | "
                  f"{card['available_text']}")
            print(f"    почему: {card['reason']}")
        print(f"Контекст сессии: {turn['context']}")
        print(f"Вызовов модели: {turn['calls']} | задержка: {turn['latency_ms']} мс | "
              f"токены (вход/выход): {turn['usage']['prompt']}/"
              f"{turn['usage']['completion']} | расчётно: {_fmt_rub(turn['cost_rub'])}")
        print()

    # Чистый контекст второй сессии.
    second = consultant.new_session()
    print("--- Проверка чистого контекста второй сессии ---")
    print(f"Контекст новой сессии до вопроса: {second.context_summary()}")
    fresh = consultant.ask(second.id, "Нужно резервное копирование")
    print(f"После вопроса без упоминания ОС: {fresh['context']['requirements']}")
    print(f"Помнит ли ОС из первой сессии: "
          f"{'да (ошибка)' if fresh['context']['requirements'].get('os') else 'нет (правильно)'}")
    print()

    # Замеры обычного поиска.
    conn = connect()
    search_stats = measure_search(conn)
    print("=" * 100)
    print("ЗАМЕРЫ: ОБЫЧНЫЙ ПОИСК (локальный индекс, без внешних вызовов)")
    print("=" * 100)
    print(f"Запросов: {search_stats['queries']} | среднее: {search_stats['avg_ms']} мс | "
          f"p95: {search_stats['p95_ms']} мс | максимум: {search_stats['max_ms']} мс")
    print()

    # Токены и стоимость.
    total_prompt = sum(t["usage"]["prompt"] for t in turns)
    total_completion = sum(t["usage"]["completion"] for t in turns)
    total_calls = sum(t["calls"] for t in turns)
    total_cost = sum(t["cost_rub"] for t in turns)
    print("=" * 100)
    print("СТОИМОСТЬ: ОБЫЧНЫЙ ПОИСК И ИИ-ДИАЛОГ")
    print("=" * 100)
    print(f"Обычный поиск: внешних вызовов 0 → 0 ₽ за запрос "
          f"(только процессор сервера, ~{search_stats['avg_ms']} мс).")
    print(f"ИИ-диалог (2 реплики): вызовов модели {total_calls}, "
          f"токены {total_prompt}/{total_completion}, "
          f"расчётно {_fmt_rub(total_cost)}.")
    if not live:
        print("Токены = 0, потому что провайдер имитирован. После подключения")
        print("ключа этот же скрипт покажет фактические значения usage из ответа API.")
    print()

    # Прогноз на 500 поисков в день.
    print("=" * 100)
    print("ПРОГНОЗ ДЛЯ ШАБЛОНА ОРГАНИЗАТОРА: ~500 ПОИСКОВ В ДЕНЬ")
    print("=" * 100)
    searches_per_day = 500
    ai_share = 0.2  # допущение: 20% поисков переходят в ИИ-диалог
    ai_dialogs = int(searches_per_day * ai_share)
    turns_per_dialog = 2
    # Оценка токенов на реплику, если живых данных нет.
    prompt_per_call = 900 if not live else max(1, total_prompt // max(total_calls, 1))
    completion_per_call = 250 if not live else max(1, total_completion // max(total_calls, 1))
    calls_per_dialog = 2  # допущение: 1 вызов с функцией + 1 финальный ответ
    month_days = 30
    ai_cost_day = (ai_dialogs * turns_per_dialog * calls_per_dialog
                   * (prompt_per_call + completion_per_call) / 1000
                   * PRICE_PER_1K_TOKENS_RUB)
    print("Допущения (расчётные, не из договора):")
    print(f"  · {searches_per_day} поисков/день, {int(ai_share*100)}% переходят в ИИ-диалог;")
    print(f"  · в диалоге {turns_per_dialog} реплики, на реплику {calls_per_dialog} вызова модели;")
    print(f"  · токены на вызов: вход ~{prompt_per_call}, выход ~{completion_per_call};")
    print(f"  · тариф {PRICE_PER_1K_TOKENS_RUB} ₽ за 1000 токенов (синхронный вызов);")
    print(f"  · системный промпт {len(SYSTEM_PROMPT)} символов, функций: {len(TOOLS)};")
    print(f"  · лимит ответа: {MAX_TOKENS} токенов.")
    print()
    print(f"Обычный поиск: {searches_per_day} запросов/день → 0 ₽ (локально).")
    print(f"ИИ-диалоги: {ai_dialogs} диалогов/день → "
          f"{_fmt_rub(ai_cost_day)}/день, {_fmt_rub(ai_cost_day * month_days)}/месяц.")
    print(f"Итого при таком профиле: {_fmt_rub(ai_cost_day * month_days)}/месяц "
          f"на ИИ + стоимость VDS.")
    print()
    print("Важно: цифры по токенам — расчётные. Фактические значения появятся")
    print("после живого вызова с ключом; формат отчёта уже готов.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
