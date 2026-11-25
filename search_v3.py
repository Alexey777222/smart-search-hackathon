"""search_v3.py — новый движок поиска с обработкой смешанных запросов.

Включает:
  - транслитерацию кириллица → латиница;
  - словарь общих терминов (бэкап → backup и т.д.);
  - алиасы брендов (пиксельдеск → pixeldesk);
  - отдельную проверку года, редакции, архитектуры;
  - пометку «не подтверждено» для атрибутов, которых нет в каталоге.

Не изменяет config.py — использует config_v3.py.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

from rapidfuzz import fuzz, process

import config_v3 as cfg

DATA_DIR = Path(__file__).resolve().parent / "data"
CATALOG_PATH = DATA_DIR / "catalog.csv"
METRICS_PATH = DATA_DIR / "metrics_demo.csv"

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_CYRILLIC_RE = re.compile(r"[а-яё]")


# ── базовые утилиты ───────────────────────────────────────────────

def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def load_catalog(path: Path = CATALOG_PATH) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_metrics(path: Path = METRICS_PATH) -> dict[str, dict] | None:
    """Читает демонстрационные коммерческие показатели по ID товара.

    Файл ДАННЫЕ-СИНТЕТИЧЕСКИЕ (учебное демо, не реальные продажи).
    Формат: id,purchases_30d,gross_margin_pct[,data_source].
    Строки-комментарии (#) и пустые пропускаются. Пустое значение =
    неизвестное (None). Если файла нет — None: прежнее поведение движка.
    """
    path = Path(path)
    if not path.exists():
        return None
    metrics: dict[str, dict] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [line for line in fh if line.strip()
                and not line.lstrip().startswith("#")]
    for row in csv.DictReader(rows):
        item: dict = {}
        for col in ("purchases_30d", "gross_margin_pct"):
            raw = (row.get(col) or "").strip()
            item[col] = None if not raw else float(raw)
        metrics[row["id"].strip()] = item
    return metrics


def _tokens(text: str) -> list[str]:
    return normalize(text).split()


def _has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text))


# ── общий механизм «цифра вместо буквы» ───────────────────────────
# Тот же словарь, что используют подсказки (suggest.py): pr0 → pro.
# Держится в движке, чтобы поиск и подсказки исправляли опечатку одинаково.
CONFUSABLES = {"0": "o", "1": "l", "3": "e", "4": "a",
               "5": "s", "6": "g", "7": "t", "8": "b", "9": "g", "2": "z"}


def deconfuse(token: str) -> str:
    """Меняет цифры-«двойники» букв на буквы: pr0 → pro."""
    return "".join(CONFUSABLES.get(c, c) for c in token)


# ── извлечение атрибутов ─────────────────────────────────────────

def _extract_years(tokens: list[str]) -> set[str]:
    return {t for t in tokens if _YEAR_RE.match(t)}


def _edition_canon(token: str) -> str | None:
    """Каноническая редакция токена, в т.ч. при опечатке цифрой (pr0 → pro).

    Исправление засчитывается только когда оно ОДНОЗНАЧНО: токен содержит
    цифру, а после замены цифр на похожие буквы попадает в словарь редакций.
    """
    canon = cfg.EDITION_TOKENS.get(token)
    if canon:
        return canon
    # Попытка «распутать» цифру в букву — только при наличии цифры.
    if not any(c.isdigit() for c in token):
        return None
    fixed = deconfuse(token)
    if fixed != token:
        canon = cfg.EDITION_TOKENS.get(fixed)
        if canon:
            return canon
    return None


def _extract_editions(tokens: list[str]) -> set[str]:
    return {c for c in map(_edition_canon, tokens) if c}


# ── проверка атрибутов ───────────────────────────────────────────

def _check_year(item_name: str, required_years: set[str]) -> tuple[str, str]:
    """Возвращает (статус, причина). Статус: ok / conflict / unconfirmed."""
    if not required_years:
        return "ok", ""
    item_years = _extract_years(_tokens(item_name))
    if not item_years:
        return "unconfirmed", "товар без года"
    if required_years & item_years:
        return "ok", ""
    return "conflict", f"год {required_years} vs {item_years}"


def _check_edition(item_name: str, required: set[str]) -> tuple[str, str]:
    """Возвращает (статус, причина). Статус: ok / conflict / unconfirmed.

    Архитектурные токены (x86, x64) исключаются — они обрабатываются
    отдельно в _check_architecture.
    """
    if not required:
        return "ok", ""

    # Исключаем архитектуру из редакционных токенов
    edition_tokens = required - _ARCH_TOKENS
    if not edition_tokens:
        return "ok", ""

    item_tokens = _tokens(item_name)
    item_editions = _extract_editions(item_tokens)

    known_required = {e for e in edition_tokens if e in cfg.EDITION_TOKENS
                      and e not in {"ultimate", "workstation"}}
    unknown_required = edition_tokens - known_required

    if unknown_required and not known_required:
        if item_editions:
            return "unconfirmed", "редакция запрошена не из каталога"
        return "unconfirmed", "товар без явной редакции"

    if known_required & item_editions:
        return "ok", ""

    for pair in cfg.INCOMPATIBLE_EDITIONS:
        if known_required & pair and item_editions & pair:
            return "conflict", f"несовместимые: {known_required & pair} vs {item_editions & pair}"

    if not item_editions:
        return "unconfirmed", "товар без явной редакции"

    if item_editions and not (known_required & item_editions):
        return "conflict", f"редакция {item_editions} vs {known_required}"

    return "ok", ""


def _check_architecture(item_name: str, required: set[str]) -> tuple[str, str]:
    """Архитектура x64/x86 — отдельная проверка."""
    req_arch = required & _ARCH_TOKENS
    if not req_arch:
        return "ok", ""
    item_arch = _extract_editions(_tokens(item_name)) & _ARCH_TOKENS
    if not item_arch:
        return "unconfirmed", "архитектура не указана в товаре"
    if req_arch & item_arch:
        return "ok", ""
    return "conflict", f"архитектура {req_arch} vs {item_arch}"


# ── извлечение продуктовой линейки ────────────────────────────────

def _extract_product_line(remainder: str) -> str | None:
    """Извлекает ключевое слово продуктовой линейки из остатка запроса.

    Остаток уже трансформирован (термины заменены, транслитерация сделана).
    Возвращает подстроку для проверки в названии товара или None.
    """
    tokens = normalize(remainder).split()
    for tok in tokens:
        if tok in cfg.PRODUCT_LINE_KEYWORDS:
            return cfg.PRODUCT_LINE_KEYWORDS[tok]
    return None


# ── извлечение бренда ─────────────────────────────────────────────

_KNOWN_BRANDS = {"taskflow", "pixeldesk", "cloudvault", "netshield",
                 "bytenote", "codeforge", "mediaplay", "datarescue",
                 "securepass", "cloudmail", "reportgen"}

# Порог распознавания бренда по опечатке латиничного токена.
_BRAND_TYPO_RATIO = 85
# Минимальная длина токена, чтобы считать его кандидатом в бренды
# (защита от «pro», «pdf» и прочих коротких слов).
_BRAND_MIN_LEN = 5


def _extract_brand(query: str) -> tuple[str | None, str]:
    """Извлекает бренд из запроса (латиница, алиас, транслитерация).

    Возвращает (латинский бренд или None, текст без бренда).
    """
    return extract_brand_info(query)[:2]


def extract_brand_info(query: str) -> tuple[str | None, str, str, str]:
    """Возвращает (бренд, остаток, источник распознования, исходный токен).

    Источник: latin | alias | typo | translit | none.
    """
    nq = normalize(query)
    tokens = nq.split()

    # 1. Латинский бренд прямо в токенах.
    for i, tok in enumerate(tokens):
        if tok in _KNOWN_BRANDS:
            rest = " ".join(t for t in tokens if t != tok)
            return tok, rest, "latin", tok

    # 2. Кириллический алиас бренда.
    for word, brand in cfg.BRAND_ALIASES_REVERSE.items():
        if word in tokens:
            rest = " ".join(t for t in tokens if t != word)
            return brand, rest, "alias", word

    # 3. Латинский токен с опечаткой, близкий к каноническому бренду.
    for i, tok in enumerate(tokens):
        if _has_cyrillic(tok) or len(tok) < _BRAND_MIN_LEN or tok in _KNOWN_BRANDS:
            continue
        best, best_score = None, 0.0
        for brand in _KNOWN_BRANDS:
            s = fuzz.ratio(tok, brand)
            if s > best_score:
                best, best_score = brand, s
        if best and best_score >= _BRAND_TYPO_RATIO:
            rest = " ".join(t for t in tokens if t != tok)
            return best, rest, "typo", tok

    # 4. Транслитерация каждого кириллического токена → проверка бренда.
    for i, tok in enumerate(tokens):
        if _has_cyrillic(tok):
            trans = cfg.transliterate(tok)
            best, best_score = None, 0.0
            for brand in _KNOWN_BRANDS:
                s = fuzz.ratio(trans, brand)
                if s > best_score:
                    best, best_score = brand, s
            if best_score >= 70:
                rest_tokens = tokens[:i] + tokens[i + 1:]
                return best, " ".join(rest_tokens), "translit", tok

    return None, nq, "none", ""


# ── трансформация текста запроса ──────────────────────────────────

def _sorted_phrases() -> list[str]:
    """Фразы словаря COMMON_TERMS, отсортированные по убыванию длины фразы."""
    return sorted(cfg.COMMON_TERMS.keys(),
                  key=lambda t: (-len(t.split()), -len(t)))


def _apply_common_terms(tokens: list[str]) -> tuple[list[str], list[dict]]:
    """Замена общих терминов ПО ГРАНИЦАМ ТОКЕНОВ (не подстрокой!).

    Подстрочный replace портил части слов: «медиаплей» теряло букву «и»
    через союз «и», а латинский ключ «ide» матчился внутри «video»
    и давал ложную замену «ide -> ide».
    """
    phrases = _sorted_phrases()
    out: list[str] = []
    changed: list[dict] = []
    i = 0
    while i < len(tokens):
        matched = False
        for phrase in phrases:
            pw = phrase.split()
            if tokens[i:i + len(pw)] == pw:
                eng = cfg.COMMON_TERMS[phrase]
                if eng and eng != phrase:
                    out.extend(eng.split())
                    changed.append({"src": phrase, "dst": eng, "kind": "term"})
                elif not eng:
                    # служебное слово (и, с, для...) — просто убираем
                    changed.append({"src": phrase, "dst": "", "kind": "stopword"})
                # eng == phrase: термин уже английский — преобразования нет
                i += len(pw)
                matched = True
                break
        if not matched:
            out.append(tokens[i])
            i += 1
    return out, changed


def _transform_text(text: str) -> tuple[str, list[str]]:
    """Заменяет общие термины, раскрывает сокращения, транслитерирует.

    Возвращает (трансформированный текст, список замен в человекочитаемом
    виде). Реестр фактических преобразований — см. transform_registry.
    """
    registry = transform_registry(text)
    human = []
    for tr in registry["transforms"]:
        if tr["kind"] == "stopword":
            continue
        human.append(f"{tr['src']} -> {tr['dst']}"
                     + (" (транслит)" if tr["kind"] == "translit" else ""))
    return registry["text"], human


def transform_registry(text: str) -> dict:
    """Структурированный реестр преобразований запроса.

    Возвращает dict:
      text       — итоговый трансформированный текст;
      transforms — [{src, dst, kind}] где kind ∈
                   term | abbreviation | translit | stopword.
    Фиксируются ТОЛЬКО фактически выполненные преобразования:
    замена вида firewall → firewall (источник == результат) не записывается.
    """
    nq = normalize(text)
    tokens = nq.split()

    tokens, transforms = _apply_common_terms(tokens)

    out: list[str] = []
    for tok in tokens:
        repl = cfg.ABBREVIATIONS.get(tok)
        if repl and repl != tok:
            out.append(repl)
            transforms.append({"src": tok, "dst": repl, "kind": "abbreviation"})
        elif _has_cyrillic(tok):
            trans = cfg.transliterate(tok)
            if trans != tok:
                out.append(trans)
                transforms.append({"src": tok, "dst": trans, "kind": "translit"})
            else:
                out.append(tok)
        else:
            out.append(tok)

    return {"text": " ".join(out), "transforms": transforms}


# ── структурированное объяснение запроса ──────────────────────────

_ARCH_TOKENS = {"x64", "x86"}

# Редакции, которые каталог не моделирует как известные (чужие значения).
_OUTSIDE_CATALOG_EDITIONS = {"ultimate", "workstation"}


def _extract_product_words(text: str) -> list[str]:
    """Все ключевые слова продуктовой линейки/назначения из текста."""
    hits, seen = [], set()
    for tok in _tokens(text):
        word = cfg.PRODUCT_LINE_KEYWORDS.get(tok)
        if word and word not in seen:
            seen.add(word)
            hits.append(word)
    return hits


def analyze_query(query: str) -> dict:
    """Разносит по полям все изменения запроса и явно запрошенные атрибуты.

    Возвращает dict:
      brand, brand_source, brand_raw   — распознанный бренд (НЕ исправление);
      typo_fixes      [{src, dst}]     — исправленные опечатки;
      abbreviations   [{src, dst}]     — раскрытые сокращения;
      other_transforms[{src, dst, kind}] — термины и транслитерация;
      stopwords      [{src}]           — отброшенные служебные слова;
      requested      {product, years, architecture, editions}
                                        — явно запрошенные атрибуты;
      rewritten      bool              — был ли текст запроса переписан;
      transformed    str               — итоговый текст для сравнения.
    """
    nq = normalize(query)
    reg = transform_registry(nq)
    transformed = reg["text"]
    brand, remainder, brand_source, brand_raw = extract_brand_info(nq)

    typo_fixes: list[dict] = []
    if brand and brand_source == "typo":
        typo_fixes.append({"src": brand_raw, "dst": brand})

    abbreviations = [t for t in reg["transforms"] if t["kind"] == "abbreviation"]
    other = [t for t in reg["transforms"] if t["kind"] in ("term", "translit")]
    stopwords = [t for t in reg["transforms"] if t["kind"] == "stopword"]

    tokens = _tokens(transformed)
    req_years = sorted(_extract_years(tokens))
    req_arch = sorted(t for t in tokens if t in _ARCH_TOKENS)
    editions = []
    for tok in tokens:
        canon = _edition_canon(tok)
        if canon and canon not in {e["canon"] for e in editions} \
                and tok not in _ARCH_TOKENS:
            editions.append({"raw": tok, "canon": canon})
            # Однозначная опечатка цифрой (pr0 → pro) — фактическое
            # исправление текста запроса, то же, что видит подсказка.
            if tok not in cfg.EDITION_TOKENS:
                fixed = deconfuse(tok)
                if fixed != tok and not any(t["src"] == tok for t in typo_fixes):
                    typo_fixes.append({"src": tok, "dst": fixed})

    product_words = _extract_product_words(
        f"{remainder or ''} {transformed}".strip())

    rewritten = bool(typo_fixes or abbreviations or other)

    return {
        "brand": brand,
        "brand_source": brand_source,
        "brand_raw": brand_raw,
        "typo_fixes": typo_fixes,
        "abbreviations": abbreviations,
        "other_transforms": other,
        "stopwords": stopwords,
        "requested": {
            "product": product_words,
            "years": req_years,
            "architecture": req_arch,
            "editions": editions,
        },
        "rewritten": rewritten,
        "transformed": transformed,
    }


def _gaps_for(statuses: dict, requested: dict) -> list[dict]:
    """Конкретные пробелы карточки: запрошенное значение, которое каталог
    не смог подтвердить. Противоречие (conflict) сюда НЕ попадает —
    оно означает «подходящего товара нет», а не «неизвестный пробел».
    """
    gaps = []
    if statuses["architecture"] == "unconfirmed":
        for value in requested["architecture"]:
            gaps.append({"kind": "architecture", "value": value,
                         "field": cfg.ATTRIBUTE_FIELDS["architecture"]})
    if statuses["edition"] == "unconfirmed":
        for ed in requested["editions"]:
            gaps.append({"kind": "edition", "value": ed["raw"],
                         "field": cfg.ATTRIBUTE_FIELDS["edition"]})
    if statuses["year"] == "unconfirmed":
        for value in requested["years"]:
            gaps.append({"kind": "year", "value": value,
                         "field": cfg.ATTRIBUTE_FIELDS["year"]})
    return gaps


# ── baseline (тот же, что и в search.py) ──────────────────────────

def baseline_search(query: str, catalog: list[dict]) -> list[dict]:
    nq = normalize(query)
    hits = [item for item in catalog if normalize(item["name"]) == nq]
    return [{"item": h, "method": "точное совпадение", "score": None}
            for h in hits]


# ── контекстное ранжирование ──────────────────────────────────────

def _item_brand(name: str) -> str | None:
    nl = name.lower()
    for brand in _KNOWN_BRANDS:
        if brand in nl:
            return brand
    return None


def _interest_markers(interest: str) -> list[str]:
    """Маркеры названия товара для категории/термина интереса."""
    if not interest:
        return []
    text, _ = _transform_text(interest)
    markers: list[str] = []
    for tok in text.split():
        if tok in cfg.CONTEXT_CATEGORIES:
            markers.extend(m for m in cfg.CONTEXT_CATEGORIES[tok]
                           if m not in markers)
        elif tok not in markers:
            markers.append(tok)
    return markers


def _context_signals(item: dict, interest_markers: list[str],
                     viewed: list[dict]) -> list[str]:
    """Список сработавших контекстных признаков для одной карточки."""
    nl = item["name"].lower()
    signals = []
    hit = [m for m in interest_markers if m in nl]
    if hit:
        signals.append("интерес: " + "/".join(sorted(set(hit))))
    ib = _item_brand(item["name"])
    ilines = set(_extract_product_words(item["name"]))
    for v in viewed:
        vb = _item_brand(v["name"])
        vlines = set(_extract_product_words(v["name"]))
        if vb and vb == ib:
            signals.append(f"просмотрен бренд {vb} ({v['id']})")
        shared = ilines & vlines
        if shared:
            signals.append("просмотрена линейка " + "/".join(sorted(shared)))
    return signals


def _context_bonus(item: dict, interest_markers: list[str],
                   viewed: list[dict]) -> float:
    bonus = 0.0
    nl = item["name"].lower()
    if interest_markers and any(m in nl for m in interest_markers):
        bonus += cfg.CONTEXT_INTEREST_BONUS
    ib = _item_brand(item["name"])
    ilines = set(_extract_product_words(item["name"]))
    for v in viewed:
        if ib and _item_brand(v["name"]) == ib:
            bonus += cfg.CONTEXT_VIEWED_BRAND_BONUS
        vlines = set(_extract_product_words(v["name"]))
        shared = ilines & vlines
        if shared and vlines:
            # Пропорционально доле совпавших слов линейки: просмотр
            # аудиоплеера сильнее поддерживает аудиотовар, чем видеоплеер
            # (у обоих общее слово player).
            bonus += cfg.CONTEXT_VIEWED_LINE_BONUS * len(shared) / len(vlines)
    return bonus


def _exact_result(item: dict, method: str) -> dict:
    """Результат точного совпадения с корректными служебными полями."""
    return {"item": item, "method": method, "score": None,
            "name_confirmed": True, "explicit_ok": True, "gaps": [],
            "context_signals": [], "base_score": None, "final_score": None}


# ── коммерческий слой ранжирования (ДЕМО-ДАННЫЕ СИНТЕТИЧЕСКИЕ) ─────
#
# ОБЩЕЕ ПРАВИЛО (без привязки к конкретным ID):
#   1. Сначала смысл запроса, обязательные атрибуты (конфликт → товар
#      исключён) и контекст. Коммерция ничего из этого не перебивает.
#   2. Товары с ОДИНАКОВЫМ уровнем релевантности (одинаковый tier:
#      полное явное соответствие / название подтверждено, но есть
#      неподтверждённый атрибут / название не подтверждено) считаются
#      СОПОСТАВИМЫМИ и упорядочиваются по показателям.
#   3. Приоритет: количество покупок за 30 дней. Покупки равны (или
#      оба неизвестны) → валовая маржа.
#   4. Неизвестное значение (пусто в CSV) НЕ даёт преимущества: такой
#      товар не поднимается выше товара с известным показателем того
#      же шага сравнения, но и не удаляется из выдачи.
#   5. Если показатели не различаются — порядок остаётся по релевантности.

def _relevance_signature(row: dict) -> tuple:
    """Подпись уровня релевантности: tier + вид подтверждённости/пробелов."""
    return (0 if row["explicit_ok"] else (1 if row["name_confirmed"] else 2),
            tuple(sorted(g["kind"] for g in row["gaps"])))


def _metric(row: dict, metrics: dict, col: str) -> float | None:
    return (metrics.get(row["item"]["id"]) or {}).get(col)


def _commercial_key(row: dict, metrics: dict) -> tuple:
    """Ключ сортировки внутри группы сопоставимых товаров.

    Неизвестное значение уходит за известное на своём шаге сравнения;
    финальная релевантность (и исходный индекс) — последний тай-брейк.
    """
    p = _metric(row, metrics, "purchases_30d")
    g = _metric(row, metrics, "gross_margin_pct")
    return (0 if p is not None else 1, -(p or 0.0),
            0 if g is not None else 1, -(g or 0.0),
            -(row["final_score"] or 0.0), row["orig_idx"])


def _why_before(a: dict, b: dict, metrics: dict) -> str:
    """Конкретная причина: почему a стоит перед b в коммерческом слое."""
    pa, pb = _metric(a, metrics, "purchases_30d"), _metric(b, metrics, "purchases_30d")
    ga, gb = _metric(a, metrics, "gross_margin_pct"), _metric(b, metrics, "gross_margin_pct")
    fa = f"{a['item']['name']} ({a['item']['id']})"

    if pa is not None and pb is not None and pa != pb:
        return (f"популярнее: {pa:.0f} vs {pb:.0f} покупок за 30 дней "
                f"(маржа {ga:.0f}% vs {gb:.0f}%, если известна — не решает)")
    if pa is not None and pb is None:
        return f"покупки известны ({pa:.0f}/30д), у соседнего — нет"
    if pa is None and pb is not None:
        return f"покупки неизвестны — не поднимается (у соседнего {pb:.0f}/30д)"
    # покупки равны или оба неизвестны → решает маржа
    if ga is not None and gb is not None and ga != gb:
        base = f"покупки равны ({pa:.0f}/30д)" if pa is not None else "покупки неизвестны у обоих"
        return f"{base}, маржа выше: {ga:.0f}% vs {gb:.0f}%"
    if ga is not None and gb is None:
        return f"маржа известна ({ga:.0f}%), у соседнего — нет"
    if ga is None and gb is not None:
        return f"маржа неизвестна — не поднимается (у соседнего {gb:.0f}%)"
    return "показатели равны или неизвестны — порядок по релевантности"


def _commercial_pass(rows: list[dict], metrics: dict) -> list[dict]:
    """Пересортировка внутри групп сопоставимых по релевантности товаров.

    rows должны идти в порядке релевантности+контекста. Возвращает новый
    список; для перемещённых строк заполняет row["commerce_note"].
    """
    ordered: list[dict] = []
    i = 0
    while i < len(rows):
        sig = _relevance_signature(rows[i])
        j = i + 1
        while j < len(rows) and _relevance_signature(rows[j]) == sig:
            j += 1
        group = rows[i:j]
        if len(group) > 1 and metrics:
            before_ids = [r["item"]["id"] for r in group]
            group = sorted(group, key=lambda r: _commercial_key(r, metrics))
            after_ids = [r["item"]["id"] for r in group]
            if before_ids != after_ids:
                for new_pos, r in enumerate(group):
                    old_pos = before_ids.index(r["item"]["id"])
                    if old_pos == new_pos:
                        continue
                    if new_pos < old_pos:
                        # поднялся — объясняем, кого обогнал
                        neighbour = group[new_pos - 1] if new_pos > 0 \
                            else group[new_pos + 1]
                        r["commerce_note"] = _why_before(r, neighbour, metrics)
                    else:
                        # опустился — объясняем, кто оказался выше
                        neighbour = group[new_pos - 1]
                        r["commerce_note"] = (
                            "уступил: " + _why_before(neighbour, r, metrics))
        ordered.extend(group)
        i = j
    return ordered


# ── основной smart-поиск v3 ──────────────────────────────────────

def smart_search_v3(query: str, catalog: list[dict],
                    context: dict | None = None,
                    metrics: dict | None = None,
                    report: dict | None = None) -> list[dict]:
    """Умный поиск v3: бренд → линейка → кандидаты → атрибуты → RapidFuzz.

    context (необязательно): {"interest": str, "viewed": [item_id, ...]}.
    metrics (необязательно): результат load_metrics — ДЕМО-ДАННЫЕ
    СИНТЕТИЧЕСКИЕ; упорядочивают только сопоставимые по релевантности
    товары (см. _commercial_pass). Без metrics порядок не меняется.
    report (необязательно): dict, куда движок пишет подсобную информацию
    для демо (например, список товаров, исключённых по конфликту
    обязательного атрибута) — не влияет на результаты.
    При пустом контексте порядок результатов полностью совпадает с прежним.
    Явно запрошенные продукт, назначение, год и редакция имеют приоритет
    над контекстом: контекст меняет порядок только среди товаров с одинаковым
    уровнем соответствия явному запросу.
    """
    nq = normalize(query)

    # 1. Точное совпадение.
    exact = [item for item in catalog if normalize(item["name"]) == nq]
    if exact:
        return [_exact_result(it, "точное совпадение") for it in exact]

    # 2. Трансформация текста запроса (термины + сокращения + транслитерация).
    transformed, replacements = _transform_text(nq)
    if transformed != nq:
        exact = [item for item in catalog
                 if normalize(item["name"]) == transformed]
        if exact:
            result = [_exact_result(it, "точное совпадение после трансформации")
                      for it in exact]
            if replacements:
                for r in result:
                    r["method"] += ": " + ", ".join(replacements)
            return result

    # 3. Извлечение бренда и подбор кандидатов.
    brand, remainder, brand_source, brand_raw = extract_brand_info(nq)
    if brand:
        candidates = [item for item in catalog
                      if brand in item["name"].lower()]
    else:
        candidates = list(catalog)

    if not candidates:
        return []

    # 3.5. Сужение по продуктовой линейке (общее правило назначения товара).
    # Если бренд определён и остаток содержит слово-продуктовую линейку,
    # исключаем товары ДРУГОЙ линейки этого же бренда.
    if brand and remainder:
        transformed_rem, _ = _transform_text(remainder)
        pl = _extract_product_line(transformed_rem)
        if pl:
            narrowed = [item for item in candidates
                        if pl in item["name"].lower()]
            if narrowed:
                candidates = narrowed

    # 4. Явно запрошенные атрибуты.
    analysis = analyze_query(query)
    requested = analysis["requested"]
    req_years = set(requested["years"])
    req_editions = _extract_editions(_tokens(transformed))

    # 5. Фильтрация кандидатов по атрибутам (conflict → не показываем).
    filtered = []
    conflict_excluded = []
    for item in candidates:
        y_status, _ = _check_year(item["name"], req_years)
        e_status, _ = _check_edition(item["name"], req_editions)
        a_status, _ = _check_architecture(item["name"], req_editions)
        if "conflict" in (y_status, e_status, a_status):
            conflict_excluded.append({"id": item["id"], "name": item["name"],
                                      "why": f"год={y_status}, редакция={e_status}, "
                                             f"архитектура={a_status}"})
            continue
        filtered.append((item, y_status, e_status, a_status))

    if report is not None:
        report["conflict_excluded_raw_count"] = len(conflict_excluded)

    if not filtered:
        if report is not None:
            # Выдача пуста: в отчёт — исключения бренда/линейки запроса.
            pruned = [ex for ex in conflict_excluded
                      if (brand and _item_brand(ex["name"]) == brand)
                      or any(pw in ex["name"].lower()
                             for pw in requested["product"])]
            report["conflict_excluded"] = pruned
        return []

    # 6. RapidFuzz.
    search_text = transformed
    if brand:
        search_text, _ = _transform_text(f"{brand} {remainder}".strip())

    names = [item["name"] for item, *_ in filtered]
    scored = process.extract(search_text, names,
                             scorer=fuzz.WRatio, limit=len(names))

    # 7. Служебные поля: подтверждённость названия, явные атрибуты, пробелы.
    product_words = requested["product"]
    rows = []
    for orig_idx, (name, score, idx) in enumerate(scored):
        if score < cfg.THRESHOLD:
            continue
        item, y_status, e_status, a_status = filtered[idx]
        nl = item["name"].lower()
        name_confirmed = all(pw in nl for pw in product_words)
        statuses = {"year": y_status, "edition": e_status,
                    "architecture": a_status}
        explicit_ok = name_confirmed and "unconfirmed" not in statuses.values()
        gaps = _gaps_for(statuses, requested) if name_confirmed else []
        rows.append({"item": item, "score": score, "method": "",
                     "name_confirmed": name_confirmed,
                     "explicit_ok": explicit_ok, "gaps": gaps,
                     "statuses": statuses, "orig_idx": orig_idx})

    # 8. Контекстные надбавки.
    interest_markers = _interest_markers((context or {}).get("interest", ""))
    viewed_ids = (context or {}).get("viewed", []) or []
    by_id = {it["id"]: it for it in catalog}
    viewed_items = [by_id[v] for v in viewed_ids if v in by_id]

    for row in rows:
        bonus = (_context_bonus(row["item"], interest_markers, viewed_items)
                 if context else 0.0)
        row["base_score"] = row["score"]
        row["final_score"] = (row["score"] + bonus) if row["score"] is not None else None

    # 9. Сортировка: tier (явное соответствие) → -final_score → orig_idx.
    rows.sort(key=lambda r: (0 if r["explicit_ok"] else 1,
                             -(r["final_score"] or 0.0), r["orig_idx"]))

    # 9.5. Коммерческий слой: только внутри групп сопоставимых товаров.
    # (ДЕМО-ДАННЫЕ СИНТЕТИЧЕСКИЕ; без metrics список не меняется.)
    for r in rows:
        r["commerce_note"] = ""
    if metrics:
        rows = _commercial_pass(rows, metrics)

    # Plain order = тот же список, но сортировка по базовому баллу (без бонуса).
    # При контексте это показывает, «как было бы» без контекста.
    plain_order = sorted(rows, key=lambda r: (0 if r["explicit_ok"] else 1,
                                              -(r["base_score"] or 0.0),
                                              r["orig_idx"]))

    # 10. Формирование объяснений.
    results = []
    for pos, row in enumerate(rows[:5], 1):
        method = "нечёткое совпадение (RapidFuzz)"
        if brand:
            method += f", бренд: {brand}"
        if replacements:
            method += ", замены: " + ", ".join(replacements)

        unconfirmed = []
        if row["statuses"]["year"] == "unconfirmed":
            unconfirmed.append("год не подтверждён")
        if row["statuses"]["edition"] == "unconfirmed":
            unconfirmed.append("редакция не подтверждена")
        if row["statuses"]["architecture"] == "unconfirmed":
            unconfirmed.append("архитектура не подтверждена")
        if unconfirmed:
            method += " [НЕ ПОДТВЕРЖДЕНО: " + "; ".join(unconfirmed) + "]"

        signals = (_context_signals(row["item"], interest_markers, viewed_items)
                   if context else [])
        row["context_signals"] = signals
        if signals and context:
            prev_pos = plain_order.index(row) + 1
            if prev_pos != pos:
                method += f" [КОНТЕКСТ: {', '.join(signals)}; " \
                          f"позиция {prev_pos}→{pos}]"
            else:
                method += f" [КОНТЕКСТ: {', '.join(signals)}; порядок не изменён]"

        if row.get("commerce_note"):
            method += " [КОММЕРЦИЯ (данные синтетические): " \
                      + row["commerce_note"] + "]"

        results.append({"item": row["item"], "method": method,
                        "score": row["base_score"],
                        "name_confirmed": row["name_confirmed"],
                        "explicit_ok": row["explicit_ok"],
                        "gaps": row["gaps"],
                        "context_signals": signals,
                        "commerce_note": row.get("commerce_note", ""),
                        "metrics": metrics.get(row["item"]["id"])
                                   if metrics else None})

    # Отчёт для демо: в список исключений — только товары, близкие к
    # ВЫДАННОМУ результату (тот же бренд или общее запрошенное
    # продуктовое слово), а не весь каталог.
    if report is not None:
        res_brands = {_item_brand(r["item"]["name"]) for r in results}
        res_brands.discard(None)
        if not res_brands and brand:
            res_brands = {brand}
        pruned = []
        for ex in conflict_excluded:
            if _item_brand(ex["name"]) in res_brands:
                pruned.append(ex)
            elif any(pw in ex["name"].lower() for pw in requested["product"]):
                pruned.append(ex)
        report["conflict_excluded"] = pruned

    return results


# ── CLI ───────────────────────────────────────────────────────────

def print_results(title: str, results: list[dict]) -> None:
    print(f"\n--- {title} ---")
    if not results:
        print("  ничего не найдено")
        return
    for pos, r in enumerate(results, 1):
        score = (f", мера сходства: {r['score']:.1f}/100"
                 if r["score"] is not None else "")
        print(f"  {pos}. {r['item']['name']} [ID: {r['item']['id']}] "
              f"— {r['method']}{score}")
    if any(r["score"] is not None for r in results):
        print("  (мера сходства RapidFuzz — похожесть строк, а не уверенность в выборе)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Умный поиск v3 (смешанные запросы, бренд, термины)")
    parser.add_argument("query", help="текст запроса")
    args = parser.parse_args(argv)

    catalog = load_catalog()
    print(f"Запрос: {args.query}")
    print_results("baseline", baseline_search(args.query, catalog))
    print_results("smart v3", smart_search_v3(args.query, catalog))
    return 0


if __name__ == "__main__":
    sys.exit(main())
