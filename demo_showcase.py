"""demo_showcase.py — сквозная демонстрация «Умный поиск» для заказчика (~3 мин).

Три эпизода:
  1. Опечатка: «TaskFlow Pr0 2024» → умный поиск находит карточку нужного
     года, подсказка показывает исправление «pr0» → «pro».
     Точный (baseline) и умный поиск выводятся рядом — это ОДНО обращение
     покупателя и ОДНА запись в журнал.
  2. Коммерция: «медиаплей» одним вызовом --compare-metrics (до/после
     рядом), затем «медиаплей видео плеер» — покупатель получает видео.
  3. Пустая выдача: «VPN Tunnel Client» → отчёт менеджера с этим запросом
     и числом таких поисков из журнала.

Использует существующие app.py, report.py и учебные данные
data/metrics_commerce_b.csv.

Данные: УЧЕБНЫЕ (СИНТЕТИЧЕСКИЕ). Демо пишет в отдельный журнал
data/showcase_journal.jsonl; основной журнал спроса не трогается.
Каждый запуск пересоздаёт демо-журнал с нуля — цифры воспроизводятся,
события не дублируются. Сравнение двух режимов одного обращения
(--compare-metrics) — ОДНА запись в журнал.

Запуск из корня проекта:
    python demo_showcase.py
"""

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import search_v3 as sv

# Дочерние прогоны с capture_output пишут в ТРУБУ, где stdout Python
# использует системную кодировку (на Windows это cp1251) и падает на
# псевдографике (╔, «→», «✓»). Явно задаём UTF-8 всем дочерним процессам.
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

# Если вывод самого показа перенаправлен в файл/трубу — тот же риск.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
JOURNAL = DATA / "showcase_journal.jsonl"
MAIN_JOURNAL = DATA / "search_journal.jsonl"

# Учебные показатели (проверенный набор из demo_commerce.py):
# SW-018 Video — 30 заказов/30д, 50% маржа; SW-019 Audio — 60 заказов/30д, 50%.
DEMO_METRICS = DATA / "metrics_commerce_b.csv"


def run(cmd: list[str], label: str = "") -> None:
    if label:
        print()
        print("=" * 70)
        print(f"  {label}")
        print("=" * 70)
    root = str(ROOT)
    shown = []
    for c in cmd:
        s = str(c)
        if s == sys.executable:
            s = "python"
        else:
            s = s.replace(root, "").lstrip("\\/")
        shown.append(s)
    print(f"\n$ {' '.join(shown)}")
    subprocess.run(cmd, check=True, text=True, encoding="utf-8",
                   env=CHILD_ENV)


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip())


def banner(text: str) -> None:
    print()
    print("=" * 70)
    print(f"  {text}")
    print("=" * 70)


def count_query_in_journal(norm_query: str) -> int:
    """Число записей журнала с данным нормализованным запросом."""
    if not JOURNAL.exists():
        return 0
    total = 0
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        if e.get("norm") == norm_query:
            total += 1
    return total


def journal_counts() -> list[tuple[str, int]]:
    """[нормализованный запрос, число событий] в демо-журнале."""
    if not JOURNAL.exists():
        return []
    c: Counter = Counter()
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c[json.loads(line).get("norm", "")] += 1
    return list(c.items())


def two_columns(left: tuple[str, list[str]], right: tuple[str, list[str]],
                width: int = 36) -> None:
    """Печатает два столбца рядом (заголовок + строки фактического вывода)."""
    l_title, l_lines = left
    r_title, r_lines = right
    col_l = [l_title] + l_lines
    col_r = [r_title] + r_lines
    for i in range(max(len(col_l), len(col_r))):
        a = col_l[i] if i < len(col_l) else ""
        b = col_r[i] if i < len(col_r) else ""
        print(f"  {a:<{width}}{b}")


def episode_1(py: str) -> None:
    banner("ЭПИЗОД 1 из 3: ПОКУПАТЕЛЬ ОШИБСЯ В НАЗВАНИИ")
    query = "TaskFlow Pr0 2024"
    print("  Покупатель ввёл: TaskFlow Pr0 2024")
    print("  (опечатка: вместо буквы «o» в слове Pro нажата цифра «0»)")

    # 1.1  Точный и умный поиск рядом — ОДНО обращение покупателя.
    # Счёт в памяти (тот же движок), в журнал не пишется; реальны
    # именно эти строки: baseline = точное совпадение названия,
    # smart v3 = поиск, который ниже выполняет app.py.
    catalog = sv.load_catalog()
    rep: dict = {}
    smart = sv.smart_search_v3(query, catalog, report=rep)
    exact = sv.baseline_search(query, catalog)

    print()
    print("=" * 70)
    print("  1.1  Точный поиск (baseline) и умный поиск — рядом")
    print("=" * 70)
    left = ("точный поиск (baseline):",
            [f"  {i}. {r['item']['name']} [{r['item']['id']}]"
             for i, r in enumerate(exact, 1)] or ["  ничего не найдено"])
    right = ("умный поиск (smart v3):",
             [f"  {i}. {r['item']['name']} [{r['item']['id']}] "
              f"({r['score']:.1f})" for i, r in enumerate(smart[:5], 1)]
             or ["  ничего не найдено"])
    two_columns(left, right)
    for ex in rep.get("conflict_excluded", []):
        print(f"     ✗ Исключён: {ex['name']} [{ex['id']}] — {ex['why']}")
    print()
    print("  → Точный поиск по строке «Pr0» не нашёл ничего.")
    print("  → Умный поиск распознал бренд taskflow, исправил однозначную")
    print("    опечатку «pr0» → «pro» и выдал карточку нужного года:")
    print("    SW-001 TaskFlow Pro 2024 — первая позиция.")
    print("  → Исправленная редакция работает как ограничение вместе с годом:")
    print("    TaskFlow Home 2024 и TaskFlow Pro 2023 исключены.")

    # 1.2  Реальный поиск через приложение — одна запись в журнал.
    run([py, "app.py", query, "--journal", str(JOURNAL)],
        "1.2  Реальный прогон через app.py (та же выдача, одна запись)")
    print()
    print("  → Это ОДНО обращение покупателя = ОДНА запись в журнал.")

    # 1.3  Подсказка при вводе — не поиск, журнал не трогает.
    run([py, "app.py", query, "--suggest"],
        "1.3  Подсказка при вводе: исправление опечатки")
    print()
    print("  → Подсказка: «TaskFlow Pro 2024» [SW-001].")
    print("  → Причина: «pr0» → «pro». Явно указанный год 2024 сохранён.")
    print("  → Подсказка НЕ записывается в журнал — это не поиск.")


def episode_2(py: str) -> None:
    banner("ЭПИЗОД 2 из 3: КОММЕРЧЕСКИЕ ПОКАЗАТЕЛИ МЕНЯЮТ ПОРЯДОК")
    print("  Покупатель ввёл: медиаплей")
    print("  Учебные данные (число заказов за 30 дней / валовая маржа):")
    print("    SW-018 MediaPlay Video Player — 30 заказов / 50%")
    print("    SW-019 MediaPlay Audio Player — 60 заказов / 50%")

    run([py, "app.py", "медиаплей", "--compare-metrics",
         "--metrics", str(DEMO_METRICS), "--journal", str(JOURNAL)],
        "2.1  Тот же запрос: без коммерции и с коммерцией рядом")
    print()
    print("  → В блоке «без показателей»: сходство обоих 70.0/100,")
    print("    порядок Video, Audio.")
    print("  → В блоке «с показателями»: 60 заказов против 30 при равном")
    print("    сходстве 70.0 — аудиоплеер поднялся на первую позицию.")
    print("  → Причина видна из чисел: «популярнее: 60 vs 30 покупок")
    print("    за 30 дней (маржа 50% vs 50% — не решает)».")
    print("  → Одно обращение покупателя = одна запись в журнал,")
    print("    хотя на экране сравнение двух режимов.")
    # 2.2  Покупатель уточняет запрос.
    run([py, "app.py", "медиаплей видео плеер", "--metrics",
         str(DEMO_METRICS), "--journal", str(JOURNAL)],
        "2.2  Покупатель уточняет: «медиаплей видео плеер»")

    print()
    print("  → Покупатель получил: MediaPlay Video Player [SW-018].")
    print("  → Явно запрошенный видео-плеер сильнее показателей:")
    print("    аудиоплеер с 60 заказами даже не попал в выдачу.")


def episode_3(py: str) -> None:
    banner("ЭПИЗОД 3 из 3: ПУСТАЯ ВЫДАЧА СТАНОВИТСЯ ЗАДАЧЕЙ МЕНЕДЖЕРА")
    print("  Покупатель ввёл: VPN Tunnel Client")
    print("  VPN-продуктов в каталоге нет.")

    run([py, "app.py", "VPN Tunnel Client", "--journal", str(JOURNAL)],
        "3.1  Реальный поиск через приложение")
    print()
    print("  → Ничего не найдено. Покупателю НЕ показали антивирус")
    print("    или файрвол «вместо VPN» — пустая выдача осталась пустой.")

    run([py, "report.py", "--journal", str(JOURNAL),
         "--orders", str(DATA / "showcase_orders.jsonl")],
        "3.2  Отчёт менеджера по тому же демо-журналу")
    print()
    n = count_query_in_journal("vpn tunnel client")
    print(f"  → Строка отчёта читается как готовая задача менеджеру:")
    print(f"    запрос «VPN Tunnel Client», частота: {n} поиск,")
    print("    действие: проверить наличие в ассортименте.")


# Ожидание ОТ ЗАПУСКА (не движок): демо выполняет ровно эти четыре
# обращения покупателя, каждое — один раз.
EXPECTED_EVENTS = ["taskflow pr0 2024", "медиаплей",
                   "медиаплей видео плеер", "vpn tunnel client"]


def check_journal(counts: list[tuple[str, int]]) -> None:
    """Печатает сверку состава демо-журнала с ожидаемыми событиями."""
    total = sum(cnt for _, cnt in counts)
    print("  Сверка журнала с ожидаемыми событиями показа:")
    print(f"    всего событий: {total} (ожидалось {len(EXPECTED_EVENTS)}): "
          + ("OK" if total == len(EXPECTED_EVENTS) else "ОШИБКА"))
    print(f"    набор запросов: "
          + ("OK" if [q for q, _ in counts] == EXPECTED_EVENTS else "ОШИБКА"))
    print(f"    каждое событие по одному разу: "
          + ("OK" if all(cnt == 1 for _, cnt in counts) else "ОШИБКА"))


def main(replay: bool = True) -> None:
    py = sys.executable

    print()
    print("╔" + "═" * 68 + "╗")
    print("║" + "УМНЫЙ ПОИСК — СКВОЗНАЯ ДЕМОНСТРАЦИЯ (УЧЕБНЫЕ ДАННЫЕ)".center(68) + "║")
    print("╚" + "═" * 68 + "╝")
    print("  Все показатели — синтетические, не реальные продажи.")

    # Демо-журнал пересоздаётся с нуля: второй показ даёт те же цифры.
    if JOURNAL.exists():
        JOURNAL.unlink()
    main_before = count_lines(MAIN_JOURNAL)

    episode_1(py)
    episode_2(py)
    episode_3(py)

    banner("ИТОГ")
    n = count_lines(JOURNAL)
    counts1 = journal_counts()
    main_after = count_lines(MAIN_JOURNAL)
    print(f"  Демо-журнал показа: {JOURNAL.name} — событий: {n}")
    print("    (подсказки — не события; сравнение до/после — одно событие;")
    print("     одно обращение покупателя = одна запись)")
    print("  Состав и частоты (нормализованный запрос → число поисков):")
    for norm, cnt in counts1:
        print(f"    {cnt:>2} × {norm}")
    check_journal(counts1)
    print(f"  Основной журнал спроса: {MAIN_JOURNAL.name} — записей: "
          f"{main_after} (до показа: {main_before})")

    if replay:
        banner("ПОВТОРНЫЙ ЗАПУСК — ПРОВЕРКА ВОСПРОИЗВОДИМОСТИ")
        print("  Второй показ демо (тот же сценарий заново, вывод скрыт)...")
        child = subprocess.run([py, "demo_showcase.py", "--no-replay"],
                               capture_output=True, text=True,
                               encoding="utf-8", env=CHILD_ENV)
        if child.returncode != 0:
            # Без this вывод ошибки был бы невидим (capture_output=True).
            print(f"  ОШИБКА: дочерний показ завершился с кодом "
                  f"{child.returncode}")
            if child.stdout:
                print("  --- stdout дочернего процесса ---")
                print(child.stdout)
            if child.stderr:
                print("  --- stderr дочернего процесса ---")
                print(child.stderr)
            raise SystemExit(child.returncode)
        counts2 = journal_counts()
        main_final = count_lines(MAIN_JOURNAL)
        print(f"  Событий после второго показа: "
              f"{sum(cnt for _, cnt in counts2)} "
              f"(первый показ: {sum(cnt for _, cnt in counts1)})")
        print("  Состав и частоты журнала совпали: "
              + ("OK" if counts1 == counts2 else "ОШИБКА"))
        check_journal(counts2)
        print("  Основной журнал спроса не изменился: "
              + ("OK" if main_final == main_before else "ОШИБКА"))


if __name__ == "__main__":
    main(replay="--no-replay" not in sys.argv)
