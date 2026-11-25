"""check_suggest.py — автоматические проверки подсказок покупателя.

Сценарии:
  1. Автодополнение: «пиксельд» → до 3 PixelDesk из каталога.
  2. Исправление опечатки: «TaskFlow Pr0 2024» → typo_fix → SW-001.
  3. Связанные запросы: «медиаплей» → related audio/video.
  4. Пустая выдача: «VPN Tunnel Client» → пусто.
  5. Точное название: «TaskFlow Pro 2024» → пусто (не исправление).
  6. Обратная совместимость: «таскф» → completion (прежнее поведение).

Каждая проверка возвращает bool. В конце — итог.
Ожидаемые ID указываются только здесь, в проверках.
"""

import search_v3 as sv
import suggest

CATALOG = sv.load_catalog()

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))


def kinds(suggestions: list[dict]) -> list[str]:
    return [s["kind"] for s in suggestions]


def ids(suggestions: list[dict]) -> list[str]:
    return [s["id"] for s in suggestions]


def result_ids(results: list[dict]) -> list[str]:
    return [r["item"]["id"] for r in results]


# ── 1. Автодополнение ────────────────────────────────────────────────

s = suggest.get_suggestions("\u043f\u0438\u043a\u0441\u0435\u043b\u044c\u0434", CATALOG)
check("автодополнение «пиксельд» → до 3 completion",
      len(s) <= 3 and all(k == "completion" for k in kinds(s)),
      f"count={len(s)} kinds={kinds(s)} ids={ids(s)}")
check("автодополнение «пиксельд» → PixelDesk товары",
      sorted(ids(s)) == sorted(["SW-005", "SW-006", "SW-007"]),
      f"ids={ids(s)}")

# ── 2. Исправление опечатки ──────────────────────────────────────────

s = suggest.get_suggestions("TaskFlow Pr0 2024", CATALOG)
check("опечатка «Pr0» → typo_fix",
      len(s) >= 1 and kinds(s)[0] == "typo_fix",
      f"kinds={kinds(s)}")
check("опечатка → найден товар SW-001",
      "SW-001" in ids(s), f"ids={ids(s)}")

# Проверим, что год сохранён в поиске.
results = sv.smart_search_v3("TaskFlow Pro 2024", CATALOG)
check("исправленный запрос «TaskFlow Pro 2024» → год 2024 сохранён",
      results and results[0]["item"]["id"] == "SW-001",
      f"top={result_ids(results)}")

# ── 3. Связанные запросы ────────────────────────────────────────────

s = suggest.get_suggestions("\u043c\u0435\u0434\u0438\u0430\u043f\u043b\u0435\u0439", CATALOG)
check("«медиаплей» → related (не typo_fix)",
      len(s) >= 2 and all(k == "related" for k in kinds(s)),
      f"kinds={kinds(s)}")
check("связанные запросы → аудио и видео плееры",
      sorted(ids(s)) == sorted(["SW-018", "SW-019"]),
      f"ids={ids(s)}")

# Проверим, что каждый запрос ведёт к правильному плееру.
r_video = sv.smart_search_v3(s[0]["text"], CATALOG)
r_audio = sv.smart_search_v3(s[1]["text"], CATALOG)
check("related «MediaPlay Video Player» → SW-018",
      r_video and r_video[0]["item"]["id"] == "SW-018",
      f"top={result_ids(r_video)}")
check("related «MediaPlay Audio Player» → SW-019",
      r_audio and r_audio[0]["item"]["id"] == "SW-019",
      f"top={result_ids(r_audio)}")

# ── 4. Пустая выдача ─────────────────────────────────────────────────

s = suggest.get_suggestions("VPN Tunnel Client", CATALOG)
check("«VPN Tunnel Client» → пустой список подсказок",
      s == [], f"suggestions count={len(s)}")

# Проверим, что поиск тоже не возвращает антивирус/файрвол.
r = sv.smart_search_v3("VPN Tunnel Client", CATALOG)
check("поиск «VPN Tunnel Client» → пусто (не антивирус/файрвол)",
      r == [], f"results={result_ids(r)}")

# ── 5. Точное название ───────────────────────────────────────────────

s = suggest.get_suggestions("TaskFlow Pro 2024", CATALOG)
check("точное название «TaskFlow Pro 2024» → пусто (не исправление)",
      s == [], f"suggestions={kinds(s)}")

# ── 6. Обратная совместимость ────────────────────────────────────────

s = suggest.get_suggestions("\u0442\u0430\u0441\u043a\u0444", CATALOG)
check("обратная совместимость «таскф» → completion (прежнее поведение)",
      len(s) >= 1 and all(k == "completion" for k in kinds(s)),
      f"kinds={kinds(s)} count={len(s)}")

# ── 7. Пустой ввод ──────────────────────────────────────────────────

s = suggest.get_suggestions("", CATALOG)
check("пустой ввод → пустой список", s == [])

# ── 8. Дедупликация: история + каталог, лимит 3 ────────────────────

# История содержит "taskflow pro" и "TaskFlow Pro 2024" (из журнала).
# Каталог содержит "TaskFlow Pro 2024" (SW-001), "TaskFlow Pro 2023" (SW-002),
# "TaskFlow Home 2024" (SW-003), "TaskFlow Enterprise" (SW-004).
# Все они подходят к вводу "task".
hist = ["taskflow pro", "TaskFlow Pro 2024", "MediaPlay Video Player"]
s = suggest.get_suggestions("task", CATALOG, history=hist,
                            history_label="Из истории запросов магазина")
norm_texts = [sv.normalize(x["text"]) for x in s]

check("объединённая выдача: максимум 3 строки",
      len(s) <= 3, f"count={len(s)} texts={[x['text'] for x in s]}")

check("дубль «taskflow pro 2024» встречается один раз",
      norm_texts.count("taskflow pro 2024") <= 1,
      f"norm_texts={norm_texts}")

# Каталог побеждает историю: у «taskflow pro 2024» должен быть ID.
dup_item = next((x for x in s if sv.normalize(x["text"]) == "taskflow pro 2024"), None)
check("при дубле сохранён ID из каталога (SW-001)",
      dup_item is not None and dup_item["id"] == "SW-001",
      f"dup_item={dup_item}")

# ── 9. Разные годы не склеиваются ──────────────────────────────────

s = suggest.get_suggestions("taskflow pro", CATALOG,
                            history=["taskflow pro 2023"])
norm_texts = [sv.normalize(x["text"]) for x in s]
# 2023 (из истории) и 2024 (из каталога) — разные варианты
check("годы 2023 и 2024 остаются разными вариантами",
      any("2023" in t for t in norm_texts) and any("2024" in t for t in norm_texts),
      f"norm_texts={norm_texts}")

# ── 10. Метка истории ──────────────────────────────────────────────

s = suggest.get_suggestions("task", CATALOG, history=["taskflow pro"],
                            history_label="Из истории запросов магазина")
hist_items = [x for x in s if x["kind"] == "history"]
check("метка истории: «Из истории запросов магазина»",
      hist_items and all(x["reason"] == "Из истории запросов магазина"
                         for x in hist_items),
      f"hist_items reasons={[x['reason'] for x in hist_items]}")

# ── 11. Подсказки не пишут журнал ──────────────────────────────────

import json
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    journal_path = Path(tmp) / "test_journal.jsonl"
    # Запишем один поиск
    import search_log
    search_log.record_search(
        raw_query="taskflow pro",
        normalized_query="taskflow pro",
        results=[],
        journal_path=journal_path,
    )
    count_before = len(search_log.read_journal(journal_path))
    # Покажем подсказки (не пишут журнал)
    _ = suggest.get_suggestions("task", CATALOG,
                                history=["taskflow pro 2024"],
                                history_label="Из истории запросов магазина")
    _ = suggest.get_suggestions("пиксельд", CATALOG)
    _ = suggest.get_suggestions("медиаплей", CATALOG)
    count_after = len(search_log.read_journal(journal_path))
    check("показ подсказок не увеличивает число записей в журнале",
          count_after == count_before,
          f"before={count_before}, after={count_after}")

# ── Итог ─────────────────────────────────────────────────────────────

print("=" * 60)
print("АВТОМАТИЧЕСКИЕ ПРОВЕРКИ ПОДСКАЗОК")
print("=" * 60)
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
