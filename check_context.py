"""check_context.py — проверка контекстного ранжирования (не часть демо).

Два блока:
  1. Иллюстрация: один запрос с разными контекстами (порядок меняется).
  2. Автоматические проверки: контекст меняет порядок ПОДХОДЯЩИХ товаров,
     но НЕ возвращает исключённый товар, НЕ снимает предупреждение
     о неподтверждённой характеристике и НЕ превращает пустую выдачу
     в случайную рекомендацию. Ожидаемые ID — только здесь, в проверках,
     не в логике поиска.
"""

import search_v3 as v3

CATALOG = v3.load_catalog()
Q = "медиаплей"

variants = [
    ("без контекста", None),
    ("интерес: аудио", {"interest": "аудио"}),
    ("интерес: видео", {"interest": "видео"}),
    ("просмотрен SW-019 (аудиоплеер)", {"viewed": ["SW-019"]}),
    ("просмотрен SW-018 (видеоплеер)", {"viewed": ["SW-018"]}),
]

for label, ctx in variants:
    print(f"\n=== {Q} | {label} ===")
    for pos, r in enumerate(v3.smart_search_v3(Q, CATALOG, ctx), 1):
        print(f"  {pos}. {r['item']['id']:<7} {r['item']['name']:<26} "
              f"score={r['score']:.1f}")
        print(f"     {r['method']}")

print("\n\n=== Явный запрос побеждает контекст ===")
for label, ctx in [("аудиоконтекст", {"interest": "аудио"}),
                   ("аудиоконтекст+просмотр аудио", {"interest": "аудио",
                                                     "viewed": ["SW-019"]})]:
    print(f"\n--- медиаплей видео плеер | {label} ---")
    for pos, r in enumerate(v3.smart_search_v3("медиаплей видео плеер",
                                               CATALOG, ctx), 1):
        print(f"  {pos}. {r['item']['id']} {r['item']['name']}")
        print(f"     {r['method']}")

# ── автоматические проверки ────────────────────────────────────────

def ids(results: list[dict]) -> list[str]:
    return [r["item"]["id"] for r in results]


CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))


# 1. Неоднозначный запрос: два плеера равны без контекста.
base = v3.smart_search_v3(Q, CATALOG, None)
check("без контекста оба плеера равны (70.0)",
      ids(base) == ["SW-018", "SW-019"]
      and abs(base[0]["score"] - base[1]["score"]) < 1e-9,
      f"топ: {ids(base)}")

# 2. Аудиоконтекст поднимает аудиоплеер, видеоконтекст — видеоплеер.
audio_ctx = v3.smart_search_v3(Q, CATALOG, {"viewed": ["SW-019"]})
check("просмотр аудиоплеера → SW-019 первый",
      ids(audio_ctx)[0] == "SW-019", f"топ: {ids(audio_ctx)}")
video_ctx = v3.smart_search_v3(Q, CATALOG, {"interest": "видео"})
check("интерес к видео → SW-018 первый",
      ids(video_ctx)[0] == "SW-018", f"топ: {ids(video_ctx)}")

# 3. Обратный случай: явный видеозапрос против аудиоконтекста.
explicit = v3.smart_search_v3("медиаплей видео плеер", CATALOG,
                              {"interest": "аудио", "viewed": ["SW-019"]})
check("явный «видео плеер» + аудиоконтекст → SW-018 первый",
      ids(explicit)[0] == "SW-018", f"топ: {ids(explicit)}")

# 4. Контекст НЕ возвращает товар, исключённый по конфликту
#    обязательного атрибута (Home ≠ Professional).
no_ctx = v3.smart_search_v3("taskflow home", CATALOG, None)
with_ctx = v3.smart_search_v3("taskflow home", CATALOG,
                              {"interest": "office", "viewed": ["SW-001"]})
check("исключённый Pro не вернулся из-за контекста",
      "SW-001" not in ids(with_ctx) and ids(with_ctx) == ids(no_ctx),
      f"без контекста: {ids(no_ctx)}, с контекстом: {ids(with_ctx)}")

# 5. Контекст НЕ снимает предупреждение о неподтверждённой характеристике.
warn_no = v3.smart_search_v3("pixeldesk studio portable x86", CATALOG, None)
warn_ctx = v3.smart_search_v3("pixeldesk studio portable x86", CATALOG,
                              {"interest": "graphics", "viewed": ["SW-006"]})
gap_no = {(r["item"]["id"], tuple(g["kind"] for g in r.get("gaps", [])))
          for r in warn_no}
gap_ctx = {(r["item"]["id"], tuple(g["kind"] for g in r.get("gaps", [])))
           for r in warn_ctx}
check("предупреждение об архитектуре x86 сохранено с контекстом",
      any(i == "SW-006" and "architecture" in kinds
          for i, kinds in gap_no) and gap_no == gap_ctx,
      f"пробелы без контекста: {sorted(gap_no)}, с контекстом: {sorted(gap_ctx)}")

# 6. Контекст НЕ превращает пустую выдачу в случайную рекомендацию.
empty_ctx = v3.smart_search_v3("абвгдежзик", CATALOG,
                               {"interest": "аудио", "viewed": ["SW-019"]})
check("пустая выдача осталась пустой с контекстом",
      empty_ctx == [], f"результат: {ids(empty_ctx)}")

print("\n\n=== АВТОМАТИЧЕСКИЕ ПРОВЕРКИ КОНТЕКСТА ===")
failed = 0
for name, ok, detail in CHECKS:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}")
    if detail:
        print(f"         {detail}")
    if not ok:
        failed += 1

print(f"\nИтог: {len(CHECKS) - failed}/{len(CHECKS)} проверок пройдено")
if failed:
    raise SystemExit(1)
