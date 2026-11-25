"""evaluate_organizer.py — прогон 10 запросов организатора и сверка целей.

Excel используется ТОЛЬКО как эталон оценки. В поиск таблица не попадает:
движок видит лишь текст запроса и локальный индекс каталога.

Для каждой строки печатается:
  * ожидаемая цель (URL из Excel) и её тип (товар/семейство/категория);
  * первый результат ЛОКАЛЬНОГО БАЗОВОГО поиска (простой текстовый поиск
    по тому же индексу) — это НЕ действующий поиск сайта Softline;
  * первый результат нашего поиска;
  * причина расхождения;
  * статус подтверждения соответствия ожидаемой цели.

Запуск:
  python evaluate_organizer.py                 # таблица + отчёт
  python evaluate_organizer.py --json          # те же данные в JSON
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

import organizer_search as osr
from organizer_index import OFFER_TYPE_LABELS, connect, normalize_text

import config_v3

ROOT = Path(__file__).resolve().parent
XLSX = ROOT / "data" / "organizer" / "Выборка поисковых запросов.xlsx"
REPORT = ROOT / "data" / "report_organizer.txt"

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ── чтение Excel без внешних пакетов ──────────────────────────────

def read_xlsx_rows(path: Path = XLSX) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))

    rows: list[dict[str, str]] = []
    for row in root.iter(f"{NS}row"):
        cells: dict[str, str] = {}
        for c in row.findall(f"{NS}c"):
            ref = c.get("r") or ""
            col = re.match(r"[A-Z]+", ref)
            if not col:
                continue
            kind = c.get("t")
            value = c.find(f"{NS}v")
            if value is not None and value.text is not None:
                if kind == "s":
                    cells[col.group(0)] = shared[int(value.text)]
                else:
                    # Числовая ячейка: 365 читается как запрос «365».
                    cells[col.group(0)] = value.text
            elif kind == "inlineStr":
                inline = c.find(f"{NS}is")
                if inline is not None:
                    cells[col.group(0)] = "".join(
                        t.text or "" for t in inline.iter(f"{NS}t"))
        if cells:
            rows.append(cells)
    return rows


def load_queries(path: Path = XLSX) -> list[dict]:
    """Возвращает [{'query': str, 'expected_url': str}] без строки заголовка."""
    out: list[dict] = []
    for cells in read_xlsx_rows(path)[1:]:
        query = (cells.get("A") or "").strip()
        url = (cells.get("B") or "").strip()
        if not query:
            continue
        out.append({"query": query, "expected_url": url})
    return out


# ── разбор ожидаемой цели ─────────────────────────────────────────

# Служебные части URL, не несущие названия продукта.
_URL_STOP = {"filter", "category", "c", "ru", "store", "softline"}

# Вендор (первый сегмент пути) → слова, которые должны быть у результата.
_VENDOR_HINTS = {
    "astralinux": ("астра", "astra"),
    "microsoft": ("microsoft", "майкрософт"),
    "kaspersky": ("kaspersky", "касперск"),
    "kripto-pro": ("криптопро", "крипто-про", "cryptopro"),
    "anydesk": ("anydesk",),
    "cyberprotect": ("киберпротект", "кибер бэкап", "cyberprotect"),
    "npo-echelon": ("эшелон", "echelon"),
    "webinar": ("мтс", "mts", "webinar", "линк"),
    "positive-technologies": ("positive technologies", "maxpatrol", "pt "),
}

# Слова, которые встречаются почти в каждой карточке и не различают семейство.
_FAMILY_STOP = {
    "dlya", "для", "i", "и", "the", "for", "and", "na", "на", "po", "по",
    "licenziya", "licenses", "edition", "special", "family", "d", "o",
}


def parse_target(url: str) -> dict:
    if not url:
        return {"url": "", "kind": "не указана", "vendor_slug": "",
                "vendor_hints": (), "family_tokens": set(),
                "filter_category": "", "is_offer_page": False}
    path = unquote(url.split("store.softline.ru", 1)[-1]).strip("/")
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    parts = [p for p in path.split("/") if p]
    vendor_slug = parts[0] if parts else ""
    rest = parts[1:]
    family_tokens = {t for t in re.split(r"[-_\s]+", " ".join(rest).lower())
                     if len(t) >= 3 and t not in _FAMILY_STOP}

    filter_category = ""
    if query:
        match = re.search(r"category\]\[\]=(\d+)", unquote(query))
        if match:
            filter_category = match.group(1)

    if filter_category or "filter" in query or not rest:
        kind = "категория/бренд"
    elif len(family_tokens) >= 2:
        kind = "страница семейства"
    else:
        kind = "страница товара"

    return {
        "url": url,
        "kind": kind,
        "vendor_slug": vendor_slug,
        "vendor_hints": _VENDOR_HINTS.get(vendor_slug, (vendor_slug,)),
        "family_tokens": family_tokens,
        "filter_category": filter_category,
        "is_offer_page": kind == "страница товара",
    }


_HINT_CACHE: dict[str, re.Pattern] = {}


def _matches(haystack: str, hint: str) -> bool:
    """Совпадение с начала слова: «kaspersk» ловит «Касперского»,
    но «криптопро» не ловит «криптопровайдера» внутри другого слова."""
    rx = _HINT_CACHE.get(hint)
    if rx is None:
        rx = re.compile(r"(?<![0-9a-zа-яё])" + re.escape(hint))
        _HINT_CACHE[hint] = rx
    return bool(rx.search(haystack))


def _any_match(haystacks: list[str], needles) -> list[str]:
    hits = []
    for needle in needles:
        if any(_matches(hay, needle) for hay in haystacks if hay):
            hits.append(needle)
    return hits


def confirm(entry: dict | None, target: dict) -> dict:
    """Проверяет, подтверждается ли ожидаемая цель первым результатом.

    Сверка идёт по вендору (поле vendor + слаг URL) и названию/URL карточки:
    страницы семейства и категории в индексе не хранятся отдельно, поэтому
    сам URL цели не воспроизводится. Для сравнения латинских слагов с
    кириллицей используется транслитерация.
    """
    if not entry:
        return {"status": "нет результата", "vendor_ok": False,
                "family_hits": [], "note": "поиск ничего не вернул"}

    name = normalize_text(entry.get("name") or "")
    url = normalize_text(entry.get("url") or "")
    vendor = normalize_text(entry.get("vendor") or "")
    vendor_fields = [vendor, url]
    name_fields = [name, url, vendor]
    for fields in (vendor_fields, name_fields):
        fields.extend(config_v3.transliterate(f) for f in list(fields))

    hits = _any_match(name_fields, target["family_tokens"])
    vendor_ok = bool(_any_match(vendor_fields, target["vendor_hints"]))

    # Цель — категория/бренд: отдельных карточек таких страниц в индексе нет.
    if target["kind"] == "категория/бренд":
        if target["filter_category"] \
                and str(entry.get("category_id") or "") == target["filter_category"]:
            return {"status": "подтверждено (категория + вендор)" if vendor_ok
                            else "подтверждено (категория)",
                    "vendor_ok": vendor_ok, "family_hits": hits,
                    "note": (f"категория карточки {entry['category_id']} совпала "
                             f"с фильтром из URL цели; сама страница фильтра "
                             f"в индексе не хранится")}
        if vendor_ok:
            return {"status": "подтверждено (вендор; цель — категория/бренд)",
                    "vendor_ok": True, "family_hits": hits,
                    "note": ("страница категории/бренда в индексе не хранится; "
                             "подтверждён вендор, карточка ведёт на его товар")}
        return {"status": "НЕ подтверждено", "vendor_ok": False,
                "family_hits": hits,
                "note": "вендор и категория цели не совпали с первым результатом"}

    if vendor_ok and hits:
        status = "подтверждено (вендор + семейство)"
    elif vendor_ok:
        status = "частично (вендор совпал, семейство другое)"
    elif hits:
        status = "частично (семейство совпало, вендор другой)"
    else:
        status = "НЕ подтверждено"

    note = ""
    if status.startswith("подтверждено"):
        note = ("URL страницы семейства в индексе не хранится; "
                "соответствие подтверждено по вендору и названию карточки")
    elif status.startswith("частично"):
        note = ("страницу цели подтвердить не удалось: "
                + ("карточка другого вендора" if hits else "то же семейство не найдено"))
    return {"status": status, "vendor_ok": vendor_ok,
            "family_hits": hits, "note": note}


# ── причина расхождения базового и нашего поиска ──────────────────

def divergence_reason(baseline: dict | None, ours: dict | None) -> str:
    if not baseline and not ours:
        return "оба поиска ничего не нашли"
    if not baseline:
        return ("локальный базовый поиск не нашёл предложения, где все слова "
                "запроса (от 3 символов) встречаются подстрокой")
    if not ours:
        return "наш поиск ничего не нашёл, базовый нашёл подстрочное совпадение"
    if baseline.get("group_id") and ours.get("group_id") \
            and baseline["group_id"] == ours["group_id"]:
        return "совпадение: оба поиска выбрали одну группу"
    if baseline.get("id") == ours.get("id"):
        return "совпадение: оба поиска выбрали одно предложение"

    reasons = []
    b_name = normalize_text(baseline.get("name") or "")
    q_norm = normalize_text(ours.get("_query") or "")
    if q_norm and q_norm in b_name:
        reasons.append("базовый нашёл только подстроку без учёта словоформ")
    if baseline.get("group_id") and ours.get("group_id") \
            and baseline["group_id"] != ours["group_id"]:
        reasons.append("базовый не группирует варианты лицензий и берёт "
                       "первое совпадение по ID/длине названия")
    details = ours.get("details") or {}
    if details.get("prefix_coverage", 0) >= 0.99 and details.get("coverage", 0) >= 0.99:
        reasons.append("наш поиск учёл покрытие токенов и начало названия")
    if details.get("version_match"):
        reasons.append("наш поиск проверил версию из запроса")
    if not reasons:
        reasons.append("разный порядок кандидатов: базовый сортирует по "
                       "наличию/длине названия, наш — по релевантности")
    return "; ".join(reasons)


# ── пригодность первого предложения для покупки ───────────────────
#
# Второй вопрос отчёта: не «нашлось ли семейство», а «можно ли купить
# именно это предложение». Ответ строится по типу предложения из фида
# (поле «Тип лицензии» + название), а не по конкретным ID.

_SUITABLE_TYPES = {"base", "subscription"}


def suitability(entry: dict | None) -> dict:
    if not entry:
        return {"ok": False, "verdict": "нет предложения", "type": "—",
                "note": "поиск ничего не вернул"}
    otype = entry.get("offer_type") or "unknown"
    label = OFFER_TYPE_LABELS.get(otype, otype)
    if otype in _SUITABLE_TYPES:
        verdict, ok = "да, можно покупать с нуля", True
    elif otype == "unknown":
        verdict, ok = "неясно: тип в фиде не указан", None
    elif otype == "renewal":
        verdict, ok = "только если лицензия уже куплена", False
    elif otype == "support":
        verdict, ok = "нет: это сопровождение, а не лицензия", False
    elif otype == "crossgrade":
        verdict, ok = "нет: cross-grade/переход с другой редакции", False
    elif otype == "addon":
        verdict, ok = "нет: это дополнение к существующей лицензии", False
    elif otype == "upgrade":
        verdict, ok = "нет: это обновление уже купленной версии", False
    else:
        verdict, ok = f"нет: {label}", False

    notes = []
    if not entry.get("available"):
        notes.append("нет в наличии")
    price = entry.get("price")
    if not price:
        notes.append("цена не указана")
    return {"ok": ok, "verdict": verdict, "type": label,
            "note": "; ".join(notes)}


# ── прогон ────────────────────────────────────────────────────────

def run(queries: list[dict]) -> dict:
    conn = connect()
    rows = []
    for item in queries:
        q = item["query"]
        target = parse_target(item["expected_url"])
        base = osr.baseline_text_search(conn, q, 1)
        base_top = base[0] if base else None

        result = osr.search(conn, q, limit=12)
        ours_top = result["groups"][0] if result["groups"] else None
        ours_entry = None
        if ours_top:
            ours_entry = dict(ours_top["best"])
            ours_entry["group_id"] = ours_top["group_id"]
            ours_entry["_query"] = q
            ours_entry["details"] = ours_top["details"]
            ours_entry["score"] = ours_top["score"]
            ours_entry["variant_count"] = ours_top["variant_count"]
            ours_entry["offer_type"] = ours_top["offer_type"]
            ours_entry["type_label"] = ours_top["type_label"]
            ours_entry["reason"] = ours_top["reason"]

        rows.append({
            "query": q,
            "expected_url": item["expected_url"],
            "target": target,
            "baseline_top": base_top,
            "baseline_confirm": confirm(base_top, target),
            "ours_top": ours_entry,
            "ours_confirm": confirm(ours_entry, target),
            "ours_suitable": suitability(ours_entry),
            "reason": divergence_reason(base_top, ours_entry),
            "candidates": result["candidates"],
            "total": result["total"],
            "shortfall": result["shortfall"],
        })

    # Пары «общий запрос / прямой запрос про тип предложения».
    pairs = [
        ("anydesk", "anydesk дополнительные устройства"),
        ("кибер бэкап", "сопровождение Кибер Бэкап"),
    ]
    pair_rows = []
    for general, specific in pairs:
        g = osr.search(conn, general, limit=1)
        s = osr.search(conn, specific, limit=1)
        pair_rows.append({
            "general_query": general,
            "general": g["groups"][0] if g["groups"] else None,
            "specific_query": specific,
            "specific": s["groups"][0] if s["groups"] else None,
        })

    # Где выдача меньше 4 товаров.
    shortfall = []
    for q in [r["query"] for r in rows] + [
            "anydesk дополнительные устройства", "сопровождение Кибер Бэкап"]:
        res = osr.search(conn, q, limit=12)
        shortfall.append({"query": q, "total": res["total"],
                          "shortfall": res["shortfall"]})

    # Где именно базовый поиск терял совпадение (исправлено).
    diagnostics = [osr.baseline_diagnose(conn, q)
                   for q in ("мтс", "кибер бэкап")]

    # Отдельная диагностика запроса «сервер» (не входит в 10 строк Excel).
    server = osr.search(conn, "сервер", limit=5)
    return {"rows": rows, "server": server, "pairs": pair_rows,
            "shortfall": shortfall, "diagnostics": diagnostics}


def _short(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def print_report(data: dict) -> str:
    L: list[str] = []
    rows = data["rows"]

    L.append("=" * 110)
    L.append("ПРОГОН 10 ЗАПРОСОВ ОРГАНИЗАТОРА")
    L.append("=" * 110)
    L.append("Локальный базовый поиск — простой текстовый поиск по тому же")
    L.append("индексу (все слова от 2 символов подстрокой в названии/вендоре).")
    L.append("Это НЕ действующий поиск сайта Softline: живой поиск магазина")
    L.append("не измерялся, сравнение только локальное.")
    L.append("Внимание: базовый поиск исправлен — SQLite lower() не понижал")
    L.append("регистр кириллицы, из-за чего «мтс» и «кибер бэкап» давали 0")
    L.append("совпадений. До исправления было 4/10, после — см. итог ниже.")
    L.append("")
    L.append(f"Строк в Excel: {len(rows)}")
    L.append("")

    for i, row in enumerate(rows, 1):
        t = row["target"]
        b = row["baseline_top"]
        o = row["ours_top"]
        L.append("-" * 110)
        L.append(f"{i}. Запрос: «{row['query']}»")
        L.append(f"   Ожидаемая цель: {t['url'] or '—'}")
        L.append(f"   Тип цели: {t['kind']} | вендор: {t['vendor_slug'] or '—'}")
        if b:
            L.append(f"   Локальный базовый поиск (1-й): {_short(b['name'], 78)} "
                     f"[{b['id']}]")
            L.append(f"      URL: {b['url']}")
            L.append(f"      Сверка: {row['baseline_confirm']['status']}")
        else:
            L.append("   Локальный базовый поиск (1-й): ничего не найдено")
            L.append(f"      Сверка: {row['baseline_confirm']['status']}")
        if o:
            L.append(f"   Наш поиск (1-й): {_short(o['name'], 78)} [{o['id']}] "
                     f"(балл {o['score']:.1f}, вариантов {o['variant_count']})")
            L.append(f"      URL: {o['url']}")
            L.append(f"      Тип предложения: {row['ours_suitable']['type']}")
            L.append(f"      Сверка семейства/категории: "
                     f"{row['ours_confirm']['status']}")
            suit = row["ours_suitable"]
            L.append(f"      Пригодность для покупки: {suit['verdict']}"
                     + (f" ({suit['note']})" if suit["note"] else ""))
        else:
            L.append("   Наш поиск (1-й): ничего не найдено")
            L.append(f"      Сверка семейства/категории: "
                     f"{row['ours_confirm']['status']}")
            L.append("      Пригодность для покупки: нет предложения")
        L.append(f"   Причина расхождения: {row['reason']}")
        note = row["ours_confirm"]["note"]
        if note:
            L.append(f"   Примечание: {note}")

    L.append("-" * 110)
    L.append("")
    L.append("СВОДНАЯ ТАБЛИЦА")
    L.append("")
    header = (f"{'#':>2} | {'запрос':<30} | {'базовый (1-й)':<34} | "
              f"{'наш (1-й)':<34} | сверка нашего")
    L.append(header)
    L.append("-" * len(header))
    for i, row in enumerate(rows, 1):
        b = row["baseline_top"]
        o = row["ours_top"]
        b_txt = _short(f"{b['name']} [{b['id']}]", 34) if b else "—"
        o_txt = _short(f"{o['name']} [{o['id']}]", 34) if o else "—"
        L.append(f"{i:>2} | {_short(row['query'], 30):<30} | {b_txt:<34} | "
                 f"{o_txt:<34} | {row['ours_confirm']['status']}")
    L.append("")

    # Счётчики.
    confirmed = sum(1 for r in rows
                    if r["ours_confirm"]["status"].startswith("подтверждено"))
    partial = sum(1 for r in rows
                  if r["ours_confirm"]["status"].startswith("частично"))
    failed = sum(1 for r in rows
                 if r["ours_confirm"]["status"].startswith("НЕ"))
    b_ok = sum(1 for r in rows
               if r["baseline_confirm"]["status"].startswith("подтверждено"))
    L.append(f"Итог нашего поиска: подтверждено {confirmed}/{len(rows)}, "
             f"частично {partial}, не подтверждено {failed}.")
    L.append(f"Итог локального базового: подтверждено {b_ok}/{len(rows)}.")
    L.append("")

    # Два разных вопроса: найдено ли семейство и можно ли купить первое.
    L.append("=" * 110)
    L.append("ДВА РАЗНЫХ ВОПРОСА К ПЕРВОМУ РЕЗУЛЬТАТУ")
    L.append("=" * 110)
    L.append(f"{'#':>2} | {'запрос':<30} | {'семейство/категория':<34} | "
             f"можно ли купить")
    L.append("-" * 100)
    buy_ok = 0
    for i, row in enumerate(rows, 1):
        suit = row["ours_suitable"]
        if suit["ok"]:
            buy_ok += 1
        L.append(f"{i:>2} | {_short(row['query'], 30):<30} | "
                 f"{row['ours_confirm']['status']:<34} | {suit['verdict']}")
    L.append("")
    L.append(f"Первое предложение пригодно для покупки с нуля: "
             f"{buy_ok}/{len(rows)}. Остальные — продление/сопровождение/"
             f"cross-grade/дополнение либо тип не указан в фиде.")
    L.append("")

    # Пары: общий запрос и прямой запрос про тип предложения.
    L.append("=" * 110)
    L.append("ПАРЫ ЗАПРОСОВ: ОБЩИЙ И ПРЯМОЙ ПРО ТИП ПРЕДЛОЖЕНИЯ")
    L.append("=" * 110)
    for pair in data["pairs"]:
        for label, key in (("общий", "general"), ("прямой", "specific")):
            g = pair[key]
            q = pair["general_query"] if key == "general" else pair["specific_query"]
            L.append(f"«{q}» ({label}):")
            if not g:
                L.append("   ничего не найдено")
                continue
            L.append(f"   1-й: {_short(g['name'], 70)} [{g['best']['id']}]")
            L.append(f"        тип: {g['type_label']} | группа: {g['bucket']} | "
                     f"цена: {g['best']['price']} {g['best']['currency'] or ''}")
            L.append(f"        почему: {g['reason']}")
        L.append("")

    # Требование «минимум 4 товара».
    L.append("=" * 110)
    L.append("ТРЕБОВАНИЕ «МИНИМУМ 4 ТОВАРА В ОТВЕТЕ»")
    L.append("=" * 110)
    bad = [s for s in data["shortfall"] if s["shortfall"]]
    for s in data["shortfall"]:
        mark = "НЕ ВЫПОЛНЕНО" if s["shortfall"] else "ок"
        L.append(f"  {s['query']:<36} найдено {s['total']:>2} — {mark}")
    if bad:
        L.append("")
        L.append("Невыполненные случаи отмечены выше; выдача не добирается "
                 "дублями лицензий одной линейки.")
    L.append("")

    # Где терялся базовый поиск.
    L.append("=" * 110)
    L.append("ПОЧЕМУ БАЗОВЫЙ ПОИСК ПРОМАХИВАЛСЯ (пошагово)")
    L.append("=" * 110)
    for diag in data["diagnostics"]:
        L.append(f"«{diag['query']}»: слова {diag['words']}"
                 + (f", отброшены {diag['dropped']}" if diag["dropped"] else ""))
        for s in diag["steps"]:
            L.append(f"   «{s['word']}»: в каталоге {s['present']}, "
                     f"lower() видел {s['sqlite_lower_like']}"
                     + (f" → потеряно {s['lost_by_sqlite_lower']}"
                        if s["lost_by_sqlite_lower"] else ""))
        L.append(f"   → {diag['conclusion']}")
        L.append("")

    # Запрос «сервер».
    srv = data["server"]
    L.append("=" * 110)
    L.append("ДИАГНОСТИКА: запрос «сервер» — различение классов")
    L.append("=" * 110)
    if srv.get("sections"):
        for kind, groups in srv["sections"].items():
            L.append(f"\n{osr._KIND_LABELS.get(kind, kind)} ({len(groups)}):")
            for g in groups:
                L.append(f"  {g['score']:6.1f} | {_short(g['name'], 72)}")
                L.append(f"         {g['best']['url']}")
                L.append(f"         {g['best']['category_path']}")
    else:
        L.append("  классы не разделились")
    L.append("")
    L.append("Сервер различается по категории и названию: оборудование —")
    L.append("«Серверы и опции», комплектующие — «Комплектующие», ПО —")
    L.append("категории программ и лицензий. Внутри класса порядок по баллу.")

    report = "\n".join(L)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Прогон 10 запросов организатора и сверка с целями Excel")
    parser.add_argument("--xlsx", default=str(XLSX))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    queries = load_queries(Path(args.xlsx))
    if not queries:
        print("В Excel не найдено ни одной строки с запросом")
        return 1

    data = run(queries)
    if args.json:
        serializable = []
        for row in data["rows"]:
            serializable.append({
                "query": row["query"],
                "expected_url": row["expected_url"],
                "target_kind": row["target"]["kind"],
                "baseline": row["baseline_top"],
                "baseline_status": row["baseline_confirm"]["status"],
                "ours": {k: v for k, v in (row["ours_top"] or {}).items()
                         if k not in ("params_json", "description", "details")},
                "ours_status": row["ours_confirm"]["status"],
                "reason": row["reason"],
            })
        print(json.dumps({"rows": serializable}, ensure_ascii=False, indent=2))
        return 0

    report = print_report(data)
    print(report)
    REPORT.write_text(report, encoding="utf-8")
    print(f"\nОтчёт сохранён: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
