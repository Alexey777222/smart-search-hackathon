"""demo_commerce.py — демонстрация влияния популярности и маржинальности.

Используется СУЩЕСТВУЮЩИЙ механизм: search_v3.load_metrics + коммерческий
слой _commercial_pass, точка входа — обычный app.py с флагом --metrics
(или --compare-metrics для «до/после»). Никаких правил под конкретные ID:
показатели передаются как данные (CSV), движок применяет общее правило —
перестановка только внутри групп сопоставимых по релевантности товаров.

Единицы измерения учебных значений:
  purchases_30d     — число заказов (покупок) за последние 30 дней, шт.;
  gross_margin_pct  — валовая маржа, %.

Запуск:
    python demo_commerce.py

Демо пишется в ОТДЕЛЬНЫЙ журнал data/demo_commerce.jsonl — основной
журнал спроса (data/search_journal.jsonl) не затрагивается.
"""

import subprocess
import sys
from pathlib import Path

import search_v3
from search_log import resolve_journal_path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
JOURNAL = DATA / "demo_commerce.jsonl"

CSV_HEADER = (
    "# УЧЕБНЫЕ ДАННЫЕ (СИНТЕТИЧЕСКИЕ) — не реальные продажи.\n"
    "# Единицы: purchases_30d — число заказов за 30 дней (шт.);\n"
    "#          gross_margin_pct — валовая маржа (%).\n"
    "id,purchases_30d,gross_margin_pct\n"
)

# Три набора показателей для плееров MediaPlay.
# Прогон 1: показатели одинаковы — порядок остаётся исходным.
CSV_A = CSV_HEADER + "SW-018,30,50\nSW-019,30,50\n"
# Прогон 2: маржа равна, популярность ВТОРОГО товара (SW-019) выше.
CSV_B = CSV_HEADER + "SW-018,30,50\nSW-019,60,50\n"
# Прогон 3: популярность равна, маржа ВТОРОГО товара (SW-019) выше.
CSV_C = CSV_HEADER + "SW-018,30,50\nSW-019,30,75\n"
# Граница 1: у аудиоплеера максимальные показатели обоих видов.
CSV_MAX_AUDIO = CSV_HEADER + "SW-018,1,1\nSW-019,999,99\n"


def write_csv(name: str, content: str) -> Path:
    p = DATA / name
    p.write_text(content, encoding="utf-8")
    return p


def run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True, text=True, encoding="utf-8")


def journal_lines() -> int:
    if not JOURNAL.exists():
        return 0
    return sum(1 for line in JOURNAL.read_text(encoding="utf-8").splitlines()
               if line.strip())


def main() -> None:
    py = sys.executable

    csv_a = write_csv("metrics_commerce_a.csv", CSV_A)
    csv_b = write_csv("metrics_commerce_b.csv", CSV_B)
    csv_c = write_csv("metrics_commerce_c.csv", CSV_C)
    csv_max = write_csv("metrics_commerce_audio_max.csv", CSV_MAX_AUDIO)

    if JOURNAL.exists():
        JOURNAL.unlink()

    print("=" * 70)
    print("ПРОГОН 1: показатели равны (30 заказов / 50% маржи у обоих)")
    print("  Ожидаем: порядок без изменений, перестановки нет")
    print("=" * 70)
    run([py, "app.py", "медиаплей", "--metrics", str(csv_a),
         "--journal", str(JOURNAL)])

    print()
    print("=" * 70)
    print("ПРОГОН 2: маржа равна, популярность второго товара выше")
    print("  SW-019: 60 заказов vs 30 (маржа 50% у обоих)")
    print("=" * 70)
    run([py, "app.py", "медиаплей", "--compare-metrics", "--metrics", str(csv_b),
         "--journal", str(JOURNAL)])

    print()
    print("=" * 70)
    print("ПРОГОН 3: популярность равна (30), маржа второго товара выше")
    print("  SW-019: 75% vs 50% (заказы 30 у обоих)")
    print("=" * 70)
    run([py, "app.py", "медиаплей", "--compare-metrics", "--metrics", str(csv_c),
         "--journal", str(JOURNAL)])

    print()
    print("=" * 70)
    print("ГРАНИЦА 1: явный видеозапрос против максимальных показателей")
    print("  аудио SW-019: 999 заказов / 99% маржи")
    print("  Ожидаем: первым остаётся ЗАПРОШЕННЫЙ видеоплеер")
    print("=" * 70)
    run([py, "app.py", "медиаплей видео плеер", "--compare-metrics",
         "--metrics", str(csv_max), "--journal", str(JOURNAL)])

    print()
    print("=" * 70)
    print("ГРАНИЦА 2: VPN-запрос при отсутствии VPN-продуктов")
    print("  Ожидаем: пустая выдача даже с коммерческими показателями")
    print("=" * 70)
    run([py, "app.py", "VPN Tunnel Client", "--metrics", str(csv_max),
         "--journal", str(JOURNAL)])

    print()
    print("=" * 70)
    print("ГРАНИЦА 3: запуск без коммерческих полей (обычный поиск)")
    print("  Ожидаем: прежний порядок результатов")
    print("=" * 70)
    run([py, "app.py", "медиаплей", "--journal", str(JOURNAL)])

    print()
    print("=" * 70)
    print("ИТОГ")
    print("=" * 70)
    print(f"  Демо-журнал: {JOURNAL}")
    print(f"  Записей: {journal_lines()} (по одной на поиск)")
    default_journal = resolve_journal_path(None)
    print(f"  Основной журнал спроса не тронут: {default_journal}")


if __name__ == "__main__":
    main()
