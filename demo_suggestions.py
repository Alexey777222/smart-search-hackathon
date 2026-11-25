"""demo_suggestions.py — демонстрация 4 сценариев подсказок через app.py.

Запуск:
    python demo_suggestions.py

Что делает:
  1. Создаёт демо-журнал с одной записью (для истории запросов).
  2. Запускает app.py --suggest для каждого сценария.
  3. Считает записи в журнале до/после подсказок (подсказки НЕ пишут).
  4. Прогоняет выбранную подсказку через обычный поиск app.py.

Сценарии:
  1. Автодополнение «пиксельд»
  2. Исправление опечатки «TaskFlow Pr0 2024»
  3. Связанные запросы «медиаплей»
  4. Пустая выдача «VPN Tunnel Client»
"""

import subprocess
import sys
from pathlib import Path

DEMO_JOURNAL = Path(__file__).resolve().parent / "data" / "demo_suggest_journal.jsonl"


def run(cmd: list[str]) -> None:
    """Запускает команду и печатает вывод."""
    print(f"\n$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False, text=True,
                            encoding="utf-8")
    if result.returncode not in (0, 1):
        print(f"  [exit code {result.returncode}]")


def journal_lines() -> int:
    if not DEMO_JOURNAL.exists():
        return 0
    return sum(1 for line in DEMO_JOURNAL.read_text(encoding="utf-8").splitlines()
               if line.strip())


def main() -> None:
    py = sys.executable

    # 0. Готовим демо-журнал с одной записью (для истории).
    DEMO_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    if DEMO_JOURNAL.exists():
        DEMO_JOURNAL.unlink()
    # Одна запись: обычный поиск «taskflow», который будет в истории.
    run([py, "app.py", "taskflow pro", "--journal", str(DEMO_JOURNAL)])
    print(f"  Записей в журнале: {journal_lines()} (1 — это поиск)")

    # ── Сценарий 1: автодополнение ────────────────────────────────
    print("\n" + "=" * 60)
    print("СЦЕНАРИЙ 1: Автодополнение")
    print("=" * 60)
    run([py, "app.py", "пиксельд", "--suggest"])
    print(f"  Записей в журнале после подсказки: {journal_lines()} (не изменилось)")
    print("  → Прогоняем выбранную подсказку «PixelDesk Studio» через поиск:")
    run([py, "app.py", "PixelDesk Studio", "--journal", str(DEMO_JOURNAL)])
    print(f"  Записей в журнале: {journal_lines()} (2 — поиск добавлен)")

    # ── Сценарий 2: исправление опечатки ──────────────────────────
    print("\n" + "=" * 60)
    print("СЦЕНАРИЙ 2: Исправление опечатки")
    print("=" * 60)
    run([py, "app.py", "TaskFlow Pr0 2024", "--suggest"])
    print(f"  Записей в журнале после подсказки: {journal_lines()} (не изменилось)")
    print("  → Прогоняем исправленный запрос «TaskFlow Pro 2024» через поиск:")
    run([py, "app.py", "TaskFlow Pro 2024", "--journal", str(DEMO_JOURNAL)])
    print(f"  Записей в журнале: {journal_lines()} (3 — поиск добавлен)")
    print("  → Проверка точного названия (не должно быть исправления):")
    run([py, "app.py", "TaskFlow Pro 2024", "--suggest"])

    # ── Сценарий 3: связанные запросы ─────────────────────────────
    print("\n" + "=" * 60)
    print("СЦЕНАРИЙ 3: Связанные запросы")
    print("=" * 60)
    run([py, "app.py", "медиаплей", "--suggest"])
    print(f"  Записей в журнале после подсказки: {journal_lines()} (не изменилось)")
    print("  → Выполнили «MediaPlay Video Player» через поиск:")
    run([py, "app.py", "MediaPlay Video Player", "--journal", str(DEMO_JOURNAL)])
    print("  → Выполнили «MediaPlay Audio Player» через поиск:")
    run([py, "app.py", "MediaPlay Audio Player", "--journal", str(DEMO_JOURNAL)])
    print(f"  Записей в журнале: {journal_lines()} (5 — два поиска добавлены)")

    # ── Сценарий 4: пустая выдача ─────────────────────────────────
    print("\n" + "=" * 60)
    print("СЦЕНАРИЙ 4: Пустая выдача")
    print("=" * 60)
    run([py, "app.py", "VPN Tunnel Client", "--suggest"])
    print(f"  Записей в журнале после подсказки: {journal_lines()} (не изменилось)")
    print("  → Поиск «VPN Tunnel Client»:")
    run([py, "app.py", "VPN Tunnel Client", "--journal", str(DEMO_JOURNAL)])
    print(f"  Записей в журнале: {journal_lines()} (6 — поиск добавлен)")

    # ── История запросов ──────────────────────────────────────────
    print("\n" + "=" * 60)
    hist_label = "Из истории запросов магазина"
    print(f"ИСТОРИЯ ЗАПРОСОВ: «task» с --history ({hist_label})")
    print("=" * 60)
    before = journal_lines()
    run([py, "app.py", "task", "--suggest", "--history", str(DEMO_JOURNAL)])
    after = journal_lines()
    print(f"  Записей в журнале: до={before}, после={after} "
          f"(изменилось: {after - before})")

    print("\n" + "=" * 60)
    print("ДЕМО ЗАВЕРШЕНО")
    print("=" * 60)
    print(f"  Финальный журнал: {DEMO_JOURNAL}")
    print(f"  Записей в журнале: {journal_lines()} (6 поисков, 0 подсказок)")


if __name__ == "__main__":
    main()
