"""suggest.py — подсказки покупателю по мере ввода (демо через CLI).

Строятся ОБЩИМ ПРАВИЛОМ из каталога, словарей и нормализации движка
(search_v3), без списков отдельных запросов. Источники подсказок:

  completion — введённый префикс (после транслитерации и словаря
               терминов) продолжает название из каталога. Сначала точный
               префикс по границам слов; если точных нет — нечёткий
               префикс (частичный транслит «пиксельд» → «pixeldesk»);
  related    — бренд или продуктовая линейка введены полностью целыми
               словами, ЛИБО кириллическое написание распознано движком
               как бренд («медиаплей» → mediaplay): предлагаем доступные
               варианты из каталога — в подсказке указан источник;
  typo_fix   — введённое слово близко к слову из названия, но не равен ему
               (в т.ч. цифра вместо буквы: «pr0» → «pro»): предлагаем
               название с исправленной опечаткой и пометкой;
  history    — предыдущие запросы покупателя, начинающиеся с ввода.

До MAX_SUGGESTIONS подсказок; дополнений — не более MAX_COMPLETIONS.
Если подходящих нет — пустой список.
Точное совпадение с названием подсказку не создаёт (дополнять нечего,
исправление не предлагается).

ВАЖНО: запрос подсказки НЕ является поиском и НЕ создаёт событие в
журнале — иначе каждый введённый символ завышает спрос.
"""

from rapidfuzz import fuzz

import search_v3 as sv

MAX_SUGGESTIONS = 3
# Дополнений одного ввода показываем не больше этого числа.
MAX_COMPLETIONS = 3
# Слова короче этого порога не «исправляем» — слишком велик риск шума.
# Токены со цифрами (опечатка вида pr0) проверяются без этого ограничения.
TYPO_MIN_LEN = 5
# Порог похожести введённого слова на слово из названия каталога.
TYPO_RATIO = 85
# Порог нечёткого префикса (частичная транслитерация).
FUZZY_PREFIX_RATIO = 75
# Цифры, которые печатают вместо похожих букв — ОБЩИЙ словарь движка
# (search_v3.CONFUSABLES): подсказка и поиск исправляют опечатку одинаково.
CONFUSABLES = sv.CONFUSABLES

_CATALOG_VOCAB: list[str] = []


def _tokens(text: str) -> list[str]:
    return sv.normalize(text).split()


def _deconfuse(tok: str) -> str:
    """Заменяет цифры-«двойники» на похожие буквы: pr0 → pro."""
    return sv.deconfuse(tok)


def _vocab(catalog: list[dict]) -> list[str]:
    return sorted({t for item in catalog for t in _tokens(item["name"])})


def _prefix_match(in_tokens: list[str], nt: list[str],
                  fuzzy: bool) -> bool:
    """Каждое введённое слово — префикс (или нечёткий префикс) слова названия.

    fuzzy=True допускает частичную транслитерацию: «pikseld» ≈ «pixeldesk».
    """
    if len(in_tokens) > len(nt):
        return False
    for p, t in zip(in_tokens, nt):
        if t.startswith(p):
            continue
        if not fuzzy:
            return False
        if p in _CATALOG_VOCAB:
            return False
        if fuzz.partial_ratio(p, t) < FUZZY_PREFIX_RATIO:
            return False
    return True


def _classify(in_tokens: list[str], catalog: list[dict],
              vocab: list[str], fuzzy: bool = False) -> tuple[list, list]:
    """Разделяет названия каталога на дополнения и связанные запросы.

    Префиксное совпадение по ГРАНИЦАМ СЛОВ:
      completion — хотя бы одно слово не дописано («таскф» → «taskflow»);
      related    — все слова целые, название длиннее
                   («cloudvault backup» → «...Backup Pro/Standard»).
    """
    global _CATALOG_VOCAB
    _CATALOG_VOCAB = vocab
    completions, related = [], []
    if not in_tokens:
        return completions, related
    for item in catalog:
        nt = _tokens(item["name"])
        if not _prefix_match(in_tokens, nt, fuzzy):
            continue
        if all(t == p for p, t in zip(in_tokens, nt)):
            if len(nt) > len(in_tokens):
                related.append(item)
            # полное совпадение — дополнять нечем
        else:
            completions.append(item)
    return completions, related


def _exact_name_matches(tokens: list[str], catalog: list[dict]) -> list[dict]:
    """Товары, чьё название целиком совпадает с токенами (для typo_fix)."""
    return [item for item in catalog if _tokens(item["name"]) == tokens]


def _typo_fixes(in_tokens: list[str], vocab: list[str]) -> dict[str, str]:
    """Опечатки: слово близко к слову из каталога, но не равен ему.

    Токен с цифрой (pr0) проверяется без порога длины: подмена цифры
    вместо буквы — явный признак опечатки.
    Возвращает {исходное: исправленное}.
    """
    fixes: dict[str, str] = {}
    for tok in in_tokens:
        if tok in vocab or tok.isdigit():
            continue
        has_digit = any(c.isdigit() for c in tok)
        if len(tok) < TYPO_MIN_LEN and not has_digit:
            continue
        deconf = _deconfuse(tok)
        if deconf != tok and deconf in vocab:
            fixes[tok] = deconf
            continue
        best, score = None, 0.0
        for v in vocab:
            s = fuzz.ratio(tok, v)
            if s > score:
                best, score = v, s
        if best is not None and score >= TYPO_RATIO:
            fixes[tok] = best
    return fixes


def _brand_related(text: str, catalog: list[dict]
                   ) -> tuple[list[dict], str, str, str]:
    """Бренд, распознанный движком (в т.ч. кириллическим написанием).

    Возвращает (товары бренда, бренд, источник, исходный токен).
    Источник: latin | alias | translit | typo | none. «медиаплей» → бренд
    mediaplay (translit) → аудио- и видеоплееры. Это НЕ опечатка:
    корректное русское написание.
    """
    brand, remainder, source, raw_tok = sv.extract_brand_info(sv.normalize(text))
    if not brand:
        return [], "", "", ""
    items = [it for it in catalog if brand in it["name"].lower()]
    return items, brand, source, raw_tok


def get_suggestions(text: str, catalog: list[dict] | None = None,
                    limit: int = MAX_SUGGESTIONS,
                    history: list[str] | None = None,
                    history_label: str = "") -> list[dict]:
    """До `limit` подсказок для введённого текста.

    Каждая подсказка: {"text", "id", "kind", "reason", "source"}.
    kind: completion | typo_fix | related | history.
    history — предыдущие запросы покупателя (обычно из журнала).
    history_label — текст источника для истории (пусто = «раньше вы искали
    это», иначе — подставляется в reason).
    """
    catalog = catalog if catalog is not None else sv.load_catalog()
    vocab = _vocab(catalog)
    raw = sv.normalize(text)
    if not raw:
        return []

    reg = sv.transform_registry(raw)
    in_tokens = _tokens(reg["text"])
    note = "" if reg["text"] == raw else f"преобразование «{raw}» → «{reg['text']}»"

    out: list[dict] = []
    # Ключ нормализованного текста — для удаления дублей.
    # При дублировании каталог (с ID) побеждает историю (без ID).
    seen_norm: dict[str, dict] = {}

    def _norm_key(item_or_text: dict | str) -> str:
        if isinstance(item_or_text, dict):
            return sv.normalize(item_or_text["name"])
        return sv.normalize(item_or_text)

    def add(item: dict, kind: str, reason: str, source: str) -> None:
        nk = _norm_key(item)
        if nk in seen_norm:
            # Если уже есть каталог — пропускаем (каталог > история).
            # Если была история — заменяем на каталог (с ID).
            existing = seen_norm[nk]
            if existing["id"]:
                return  # каталог уже есть, пропускаем
            # Заменяем историю на каталог
            existing["text"] = item["name"]
            existing["id"] = item["id"]
            existing["kind"] = kind
            existing["reason"] = reason
            existing["source"] = source
            return
        seen_norm[nk] = {"text": item["name"], "id": item["id"],
                         "kind": kind, "reason": reason, "source": source}
        out.append(seen_norm[nk])

    def add_text(line: str, kind: str, reason: str, source: str) -> None:
        nk = sv.normalize(line)
        if nk in seen_norm:
            # Если уже есть каталог — пропускаем (каталог > история)
            return
        seen_norm[nk] = {"text": line, "id": "",
                         "kind": kind, "reason": reason, "source": source}
        out.append(seen_norm[nk])

    # 0. История запросов покупателя.
    for h in history or []:
        hn = sv.normalize(h)
        if not hn or hn == raw:
            continue
        ht = _tokens(hn)
        if len(ht) > len(in_tokens) and _prefix_match(in_tokens, ht, False):
            hist_label = history_label or "раньше вы искали это"
            add_text(hn, "history", hist_label, "query history")

    # Ввод — точное название товара: дополнять и исправлять нечего.
    if _exact_name_matches(in_tokens, catalog):
        return out[:limit]

    # Бренд движок распознаёт и в латинице, и в кириллице («медиаплей»).
    brand_items, brand, brand_source, brand_raw = _brand_related(raw, catalog)

    # 1. Дополнения названий из каталога (точный префикс).
    completions, related = _classify(in_tokens, catalog, vocab)

    # 2. Исправление опечатки считаем по токенам ВНЕ бренда: корректное
    #    кириллическое написание бренда («mediapley») — не опечатка.
    #    При опечатке в самом бренде (source=typo) токен оставляем в fixes.
    brand_ignore: set[str] = set()
    if brand and brand_source != "typo":
        brand_ignore.add(brand)
        # Кириллическое/алиасное написание бренда после транслита движка
        # («медиаплей» → «mediapley») тоже не опечатка.
        if brand_raw:
            brand_ignore.add(sv.cfg.transliterate(brand_raw))
            brand_ignore.add(sv.normalize(brand_raw))
    fix_tokens = [t for t in in_tokens if t not in brand_ignore]
    fixes = _typo_fixes(fix_tokens, vocab) if not completions and not related else {}
    corrected = [fixes.get(t, t) for t in in_tokens] if fixes else in_tokens

    # Дополнения: не больше MAX_COMPLETIONS.
    shown = 0
    for item in completions:
        if shown >= MAX_COMPLETIONS:
            break
        reason = "дополнение названия из каталога"
        if note:
            reason += f" ({note})"
        add(item, "completion", reason, "catalog")
        shown += 1

    # 3. Исправление опечатки — приоритет над связанными запросами бренда:
    #    «TaskFlow Pr0 2024» → конкретный «TaskFlow Pro 2024» с годом.
    if fixes:
        desc = "; ".join(f"«{a}» → «{b}»" for a, b in sorted(fixes.items()))
        reason = f"исправление опечатки: {desc}"
        exact = _exact_name_matches(corrected, catalog)
        comp2, rel2 = _classify(corrected, catalog, vocab)
        for item in exact + comp2 + rel2:
            add(item, "typo_fix", reason, "catalog:typo")

    # 4. Связанные запросы бренда («медиаплей» → аудио/видео плееры).
    if brand_items and not fixes and not completions and not related:
        for item in brand_items[:MAX_COMPLETIONS]:
            add(item, "related",
                f"связанный запрос: продукты бренда «{brand}»",
                f"brand: {brand}")

    # 5. Связанные запросы: целые слова ввода → варианты линейки/бренда.
    for item in related:
        n = len(in_tokens)
        line = " ".join(item["name"].split()[:n])
        variant = "продукты бренда" if n == 1 else "редакции линейки"
        add(item, "related",
            f"связанный запрос: доступные {variant} «{line}»",
            f"product line: {line.lower()}")

    # 6. Нечёткий префикс — если точный не дал ничего (частичный транслит).
    if (not completions and not related and not fixes
            and not brand_items):
        fcomp, _ = _classify(in_tokens, catalog, vocab, fuzzy=True)
        shown = 0
        for item in fcomp:
            if shown >= MAX_COMPLETIONS:
                break
            reason = "дополнение названия из каталога"
            if note:
                reason += f" ({note})"
            add(item, "completion", reason, "catalog")
            shown += 1

    return out[:limit]
