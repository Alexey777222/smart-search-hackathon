"""create_demo_sessions.py — генерация учебных демо-данных.

Создаёт 4 завершённые сессии поисков + файл заказов.
Каждый поиск выполняется через движок search_v3, чтобы пустая или
непустая выдача определялась фактическим результатом каталога.

Сценарии:
  А: успешный поиск → заказ
  Б: пустая выдача → завершение сессии
  В: пустая выдача → повторный успешный поиск → заказ
  Г: успешный поиск → завершение сессии без заказа

Запуск:
    python create_demo_sessions.py

Результаты:
    data/demo_sessions.jsonl  — журнал поисков с session_id
    data/orders_demo.jsonl    — журнал заказов с session_id
"""

import json
from pathlib import Path

import search_v3
import config_v3 as cfg
from search_log import record_search, record_order

# Путь к демо-журналам
SESSIONS_PATH = Path(__file__).resolve().parent / "data" / "demo_sessions.jsonl"
ORDERS_PATH = Path(__file__).resolve().parent / "data" / "orders_demo.jsonl"

CATALOG = search_v3.load_catalog()

# ── Сценарии: (session_id, [(raw_query, ctx, has_order, item_id)]) ──
# has_order / item_id — только для последнего поиска сессии.
SCENARIOS = [
    # А: успешный поиск → заказ
    ("A", [("CloudVault Backup Pro", "классический поиск", True, "SW-008")]),
    # Б: пустая выдача → завершение сессии
    ("B", [("абвгдежзик", "тест пустой выдачи", False, None)]),
    # В: пустая выдача → повтор → заказ
    (
        "V",
        [
            ("абвгдежзик", "тест пустой выдачи", False, None),
            ("TaskFlow Pro 2024", "уточнённый запрос", True, "SW-001"),
        ],
    ),
    # Г: успешный поиск → без заказа
    ("G", [("PixelDesk Studio Portable", "просмотр каталога", False, None)]),
]

# Базовое время: 2025-01-15T10:00:00Z, шаг 30 секунд между поисками
_BASE_TS = "2025-01-15T10:{:02d}:{:02d}Z"


def _next_ts(session_idx: int, search_idx: int) -> str:
    """Формирует метку времени: база + сессия * 10 мин + поиск * 30 сек."""
    total_seconds = session_idx * 600 + search_idx * 30
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return _BASE_TS.format(minutes, seconds)


def _order_ts(session_idx: int, search_idx: int) -> str:
    """Метка времени заказа — через 5 минут после последнего поиска сессии."""
    total_seconds = session_idx * 600 + search_idx * 30 + 300
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return _BASE_TS.format(minutes, seconds)


def main() -> None:
    # Удаляем старые демо-файлы, чтобы обеспечить чистоту теста.
    for p in (SESSIONS_PATH, ORDERS_PATH):
        if p.exists():
            p.unlink()

    catalog = CATALOG
    session_count = 0
    for sid, steps in SCENARIOS:
        for step_idx, (query, ctx, has_order, item_id) in enumerate(steps):
            ts = _next_ts(session_count, step_idx)
            results = search_v3.smart_search_v3(query, catalog)
            analysis = search_v3.analyze_query(query)

            record_search(
                raw_query=query,
                normalized_query=search_v3.normalize(query),
                context=ctx,
                results=results,
                analysis=analysis,
                journal_path=SESSIONS_PATH,
                session_id=sid,
                ts=ts,
            )

            print(f"  [{sid}] #{step_idx+1}  {query!r}  → "
                  f"{len(results)} результатов  ts={ts}")

            if has_order and item_id:
                item = next((i for i in catalog if i["id"] == item_id), {})
                o_ts = _order_ts(session_count, step_idx)
                record_order(
                    session_id=sid,
                    item_id=item_id,
                    item_name=item.get("name", ""),
                    journal_path=str(ORDERS_PATH),
                    ts=o_ts,
                )
                print(f"         → заказ {item_id} {item.get('name', '')}  ts={o_ts}")

        session_count += 1

    print(f"\n✓ Записей поисков: {SESSIONS_PATH}")
    print(f"✓ Записей заказов: {ORDERS_PATH}")


if __name__ == "__main__":
    main()
