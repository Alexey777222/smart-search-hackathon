"""organizer_search.py — поиск по локальному индексу каталога организатора.

Индекс собирается organizer_index.py из data/organizer/Mindbox.yml.gz.xml.
Поиск не использует Excel: таблица запросов нужна только оценщику.

Что делает поиск:
  * находит предложения по обратному индексу токенов;
  * переранжирует кандидатов RapidFuzz + покрытием токенов + префиксом
    названия (чтобы «astra linux» находил саму ОС, а не «КриптоПро CSP
    для Astra Linux»);
  * группирует варианты лицензий по group_id; предложения без group_id
    остаются отдельными карточками;
  * для запроса «сервер» делит выдачу на серверное оборудование,
    комплектующие и ПО.

CLI:
  python organizer_search.py "astra linux"
  python organizer_search.py "сервер"
  python organizer_search.py "astra linux" --baseline
  python organizer_search.py --offer 482091
  python organizer_search.py --group 121231
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from rapidfuzz import fuzz

import config_v3
from organizer_index import (DEFAULT_DB, OFFER_TYPE_LABELS, _family_name,
                             canon_token, connect, get_offer, normalize_text,
                             query_tokens, tokenize, tokenize_cached)
from query_normalize import fix_keyboard_layout, term_known

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")
_SERVER_KINDS = ("server_hardware", "server_component", "server_software")

# ── намерение запроса: какое предложение считать главным ───────────
#
# Правило работает по данным каталога (поле «Тип лицензии» и название),
# а не по конкретным ID: для общего запроса главным считается базовая
# или новая лицензия, а продление/сопровождение/cross-grade/дополнение
# помечаются как отдельный тип. Если пользователь прямо просит продление
# или дополнительные устройства — приоритет меняется.

_INTENT_PATTERNS = (
    ("renewal", re.compile(r"продлен|продлить|renew", re.I)),
    ("support", re.compile(r"сопровожд|техподдержк|тех\.? ?поддержк|поддержк",
                           re.I)),
    ("addon", re.compile(
        r"дополнительн|доп\.? ?устройств|add-?on|расширени|докупить|"
        r"ещё|еще лиценз", re.I)),
    ("subscription", re.compile(r"подписк|аренд|на год|12 мес", re.I)),
)

_INTENT_PRIMARY = {
    "renewal": ("renewal", "base"),
    "support": ("support",),
    "addon": ("addon",),
    "subscription": ("subscription", "base"),
    "general": ("base", "subscription", "unknown", "other"),
}

# Поправка к баллу по типу предложения (в общем запросе).
_TYPE_ADJUST_GENERAL = {
    "base": 8.0,
    "subscription": 4.0,
    "unknown": 0.0,
    "other": -2.0,
    "upgrade": -1.0,
    "addon": -3.0,
    "renewal": -6.0,
    "crossgrade": -8.0,
    "support": -8.0,
}


def detect_intent(query: str) -> dict:
    """Намерение запроса и приоритет типов предложения."""
    for name, rx in _INTENT_PATTERNS:
        if rx.search(query or ""):
            return {"name": name, "primary": _INTENT_PRIMARY[name]}
    return {"name": "general", "primary": _INTENT_PRIMARY["general"]}


def _type_adjust(offer_type: str | None, intent: dict) -> float:
    otype = offer_type or "unknown"
    if intent["name"] == "general":
        return _TYPE_ADJUST_GENERAL.get(otype, 0.0)
    primary = intent["primary"]
    if otype == primary[0]:
        return 12.0
    if len(primary) > 1 and otype == primary[1]:
        return 6.0
    if otype in ("base", "subscription"):
        return 0.0
    return -6.0


def _type_label(offer_type: str | None) -> str:
    return OFFER_TYPE_LABELS.get(offer_type or "unknown", offer_type or "unknown")


def _class_adjust(offer: dict, reqs: dict | None) -> float:
    """Штраф артикулу другого функционального класса.

    Совпадение слова, вендора, платформы или типа лицензии не доказывает
    эквивалентность: ОС не заменяет антивирус. Артикул явно другого
    класса получает штраф, чтобы не задавать вес всему семейству. Товары
    без распознанного класса («unknown») не штрафуются.
    """
    if not reqs:
        return 0.0
    import product_scope
    return -25.0 if product_scope.class_relation(offer, reqs) == "other" else 0.0


def _type_reason(offer: dict, intent: dict) -> str:
    """Короткое «почему показан» по типу предложения."""
    otype = offer.get("offer_type") or "unknown"
    label = _type_label(otype)
    if intent["name"] != "general":
        if otype == intent["primary"][0]:
            return f"запрос прямо про «{label}» — тип совпал"
        if otype in ("base", "subscription"):
            return f"это {label}, а не {intent['primary'][0]} — вариант на случай другой задачи"
        return f"тип «{label}» — не то, что просили, показан как сопутствующий"
    if otype in ("base", "subscription"):
        return f"{label}: подходит для покупки с нуля"
    if otype == "renewal":
        return "продление: нужно, только если лицензия уже есть"
    if otype == "support":
        return "сопровождение/техподдержка, а не сама лицензия"
    if otype == "crossgrade":
        return "cross-grade: переход с другой редакции, не новая покупка"
    if otype == "addon":
        return "дополнение/доп. устройства, а не самостоятельная лицензия"
    if otype == "upgrade":
        return "обновление версии: нужно, если версия уже куплена"
    return "тип в каталоге не указан"


# ── имена карточек ────────────────────────────────────────────────

_DISPLAY_CUTS = (
    " на базе ", " п/а ", " уровень ", " вариант ", " новая лицензия",
    " продление лицензии", " Cross-grade", " лицензия на", " лицензия для",
    " лицензия,", " конфигурация ", " для 64-х", " для ЭВМ", " версии ",
)


def display_name(name: str) -> str:
    """Короткое имя семейства для карточки результата."""
    text = _family_name(name)
    for marker in _DISPLAY_CUTS:
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return text or name


# ── оценка одного предложения ─────────────────────────────────────

def _versions(text: str) -> set[str]:
    return set(_VERSION_RE.findall(text or ""))


def score_offer(query: str, q_norm: str, q_tokens: list[str],
                q_versions: set[str], offer: dict) -> tuple[float, dict]:
    name = offer.get("name") or ""
    vendor = offer.get("vendor") or ""
    category = offer.get("category_path") or ""
    name_norm = normalize_text(name)
    vendor_norm = normalize_text(vendor)
    cat_norm = normalize_text(category)

    offer_tokens = tokenize_cached(f"{name} {vendor} {category}")
    matched = sum(1 for t in q_tokens if t in offer_tokens)
    coverage = matched / len(q_tokens) if q_tokens else 0.0

    # Насколько запрос покрыт НАЧАЛОМ названия: «astra linux» должно
    # находить Astra Linux, а не «КриптоПро CSP для Astra Linux».
    # Берём исходное (не приведённое к нижнему регистру) название, иначе
    # теряется разбиение CamelCase: MaxPatrol → max + patrol.
    words = name.split()
    prefix_text = " ".join(words[: len(q_tokens) + 2])
    prefix_tokens = tokenize_cached(prefix_text)
    prefix_matched = sum(1 for t in q_tokens if t in prefix_tokens)
    prefix_cov = prefix_matched / len(q_tokens) if q_tokens else 0.0

    name_score = max(fuzz.WRatio(q_norm, name_norm),
                     fuzz.token_set_ratio(q_norm, name_norm))
    vendor_score = fuzz.WRatio(q_norm, vendor_norm) if vendor_norm else 0.0
    cat_score = fuzz.WRatio(q_norm, cat_norm) if cat_norm else 0.0

    starts = 1.0 if q_norm and name_norm.startswith(q_norm) else 0.0
    phrase = 1.0 if q_norm and q_norm in name_norm else 0.0

    version_bonus = 0.0
    version_penalty = 0.0
    if q_versions:
        offer_versions = _versions(name)
        if offer_versions:
            if q_versions & offer_versions:
                version_bonus = len(q_versions & offer_versions) / len(q_versions)
            else:
                # Запрошена версия 5.0, а в названии только чужие версии
                # (4.0) — не подменяем версию.
                version_penalty = 18.0
        else:
            version_penalty = 5.0

    # Точное попадание всех токенов в вендора — полезно для «мтс», «anydesk».
    vendor_tokens = tokenize_cached(vendor)
    vendor_cov = (sum(1 for t in q_tokens if t in vendor_tokens) / len(q_tokens)
                  if q_tokens else 0.0)

    score = (
        0.30 * name_score
        + 0.12 * vendor_score
        + 0.05 * cat_score
        + 28.0 * coverage
        + 26.0 * prefix_cov
        + 14.0 * starts
        + 9.0 * phrase
        + 10.0 * version_bonus
        + 10.0 * vendor_cov
        + 2.0 * (1 if offer.get("available") else 0)
        - version_penalty
    )

    details = {
        "name_score": round(name_score, 1),
        "vendor_score": round(vendor_score, 1),
        "category_score": round(cat_score, 1),
        "coverage": round(coverage, 2),
        "prefix_coverage": round(prefix_cov, 2),
        "starts_with_query": bool(starts),
        "contains_phrase": bool(phrase),
        "version_match": round(version_bonus, 2),
        "vendor_coverage": round(vendor_cov, 2),
    }
    return score, details


# ── кандидаты из индекса ──────────────────────────────────────────

_RARE_TOKEN_LIMIT = 1200


def _candidate_ids(conn: sqlite3.Connection, q_tokens: list[str],
                   limit: int = 2600) -> list[str]:
    if not q_tokens:
        return []
    ids: list[str] = []
    priority: list[str] = []

    # Для «сервер» сначала набираем кандидатов из каждого класса, иначе
    # частотный токен вытесняет редкие классы и ПО не попадает в выдачу.
    if "сервер" in q_tokens:
        for kind in _SERVER_KINDS:
            rows = conn.execute(
                "SELECT o.id FROM offers o JOIN offer_tokens t "
                "ON t.offer_id=o.id WHERE t.token='сервер' AND o.kind=? "
                "LIMIT 600", (kind,))
            priority.extend(r["id"] for r in rows)
    ids.extend(priority)

    # Редкие токены (название продукта: «anydesk», «patrol», «мтс») набираем
    # ДО частотных: иначе «продление»/«сопровождение» занимают весь лимит и
    # продукт из запроса в кандидатов не попадает.
    rare: list[str] = []
    for token in q_tokens:
        total = conn.execute(
            "SELECT COUNT(*) FROM offer_tokens WHERE token=?",
            (token,)).fetchone()[0]
        if total <= _RARE_TOKEN_LIMIT:
            rare.extend(r["offer_id"] for r in conn.execute(
                "SELECT offer_id FROM offer_tokens WHERE token=? LIMIT ?",
                (token, _RARE_TOKEN_LIMIT)))

    placeholders = ",".join("?" for _ in q_tokens)
    rows = conn.execute(
        f"SELECT offer_id, COUNT(*) AS hits FROM offer_tokens "
        f"WHERE token IN ({placeholders}) GROUP BY offer_id "
        f"ORDER BY hits DESC LIMIT ?", (*q_tokens, limit))
    ids.extend(r["offer_id"] for r in rows)

    # Уникализация с сохранением порядка + потолок по числу кандидатов.
    merged = dict.fromkeys(ids[:len(priority)] + rare + ids[len(priority):])
    return list(merged)[:limit + len(priority)]


def _load_offers(conn: sqlite3.Connection, ids: list[str]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT id, group_id, name, vendor, vendor_url, url, category_id, "
            f"category_path, price, oldprice, currency, available, params_json, "
            f"description, kind, offer_type, license_type_raw, platform, "
            f"duration FROM offers WHERE id IN ({placeholders})", chunk)
        out.extend(_row_to_offer(r) for r in rows)
    return out


def _row_to_offer(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["available"] = bool(data["available"])
    return data


# ── группировка ───────────────────────────────────────────────────

def _group_key(offer: dict) -> str:
    return offer["group_id"] or f"offer:{offer['id']}"


def _sku_allowed(offer: dict, reqs: dict | None) -> bool:
    """Артикул не противоречит обязательным условиям запроса.

    Проверка идёт ДО выбора представителя группы: если в семействе есть
    и «для рабочей станции Windows», и «для физического сервера», серверный
    вариант оценивается отдельно, а не подменяется первым по релевантности.
    """
    if not reqs:
        return True
    # Локальный импорт: selection импортирует этот модуль.
    from selection import sku_conflict
    return not sku_conflict(offer, reqs)


def _score_group_offers(conn: sqlite3.Connection, group_key: str,
                        query: str, q_norm: str, q_tokens: list[str],
                        q_versions: set[str],
                        cache: dict | None = None,
                        reqs: dict | None = None) -> list[tuple[dict, float, dict]]:
    if group_key.startswith("offer:"):
        offer = get_offer(conn, group_key.split(":", 1)[1])
        offers = [offer] if offer else []
    else:
        rows = conn.execute(
            "SELECT id, group_id, name, vendor, vendor_url, url, category_id, "
            "category_path, price, oldprice, currency, available, params_json, "
            "description, kind, offer_type, license_type_raw, platform, "
            "duration FROM offers WHERE group_id=?", (group_key,))
        offers = [_row_to_offer(r) for r in rows]
    if reqs:
        offers = [o for o in offers if _sku_allowed(o, reqs)]
    scored = []
    for offer in offers:
        hit = cache.get(offer["id"]) if cache is not None else None
        if hit is None:
            s, details = score_offer(query, q_norm, q_tokens, q_versions, offer)
            if cache is not None:
                cache[offer["id"]] = (s, details)
        else:
            s, details = hit
        scored.append((offer, s, details))
    scored.sort(key=lambda x: (-x[1], not x[0]["available"],
                               x[0]["price"] if x[0]["price"] is not None else 1e18,
                               x[0]["id"]))
    return scored


def _pick_representative(scored: list[tuple[dict, float, dict]],
                         intent: dict,
                         reqs: dict | None = None) -> tuple[dict, float, dict, float]:
    """Представитель группы: релевантность текста + приоритет типа.

    Возвращает (offer, текст_балл, детали, итоговый_балл). Именно так
    общий запрос открывает базовую лицензию, а не сертификат сопровождения,
    который тоже содержит слова запроса и часто дешевле.
    """
    best: tuple[dict, float, dict, float] | None = None
    for offer, s, details in scored:
        combined = (s + _type_adjust(offer.get("offer_type"), intent)
                    + _class_adjust(offer, reqs))
        if best is None or combined > best[3]:
            best = (offer, s, details, combined)
    return best


# Слова-квалификаторы не меняют продуктовое семейство: редакция, тип
# поставки, платформа. «Кибер Бэкап Стандартная редакция», «… Расширенная
# редакция» и «… Облачный» — одно семейство одного вендора, поэтому в
# топ-4 попадает максимум одна позиция семейства.
_FAMILY_QUALIFIERS = {
    "стандартн", "расширенн", "базов", "профессиональн", "корпоративн",
    "облачн", "старт", "редакц", "верси", "лиценз", "подписк", "пакет",
    "сертифицированн", "образовательн", "организац", "рабоч", "станци",
    "пользовател", "устройств", "год", "мес", "месяц", "помесячн", "шт",
    "электронн", "коробочн", "дополнительн", "техподдержк", "сопровожден",
    "продлен", "включа", "услуг", "техническ", "поддержк",
    "windows", "виндовс", "linux", "астра", "astra", "astralinux", "unix",
    "macos", "x64", "x86", "x86-64", "aarch64", "arm", "e2k", "для", "на",
}

# Название до первой скобки или запятой — это ядро продукта; всё дальше
# описывает поставку («лицензия для образовательных организаций»,
# «для рабочей станции Windows») и семейство не меняет.
_FAMILY_CORE_RE = re.compile(r"^[^(,]+")


def product_family(offer: dict) -> tuple[str, str]:
    """Ключ продуктового семейства по данным каталога.

    Берётся вендор и значимые токены ядра названия без редакции, типа
    поставки и платформы. Это общее правило: редакции одного продукта
    больше не считаются разными семействами, а разные продукты того же
    вендора — по-прежнему разные.
    """
    vendor = normalize_text(offer.get("vendor") or "")
    name = normalize_text(offer.get("name") or "")
    core = (_FAMILY_CORE_RE.match(name).group(0) if _FAMILY_CORE_RE.match(name)
            else name)
    tokens = sorted(
        t for t in tokenize(core)
        if t not in _FAMILY_QUALIFIERS and len(t) >= 2
    )
    return (vendor, " ".join(tokens) or core)


def _family_key(offer: dict) -> tuple[str, str, str]:
    """Ключ дедупликации линеек: вендор + имя семейства + группа типа."""
    vendor, family = product_family(offer)
    otype = offer.get("offer_type") or "unknown"
    group = "support" if otype == "support" else "license"
    return (vendor, family, group)


_INTENT_TOKENS = {
    "дополнительн", "устройств", "сопровожден", "техподдержк", "поддержк",
    "продлен", "продлить", "подписк", "аренд", "докупить",
}


def _product_tokens(q_tokens: list[str], intent: dict) -> list[str]:
    """Слова запроса без слов о типе предложения («дополнительные», «продление»).

    Они описывают намерение, а не продукт: иначе AnyDesk с запросом
    «anydesk дополнительные устройства» получает покрытие 1/3 и падает
    в «сопутствующие».
    """
    if intent["name"] == "general":
        return q_tokens
    rest = [t for t in q_tokens if t not in _INTENT_TOKENS]
    return rest or q_tokens


def _coverage(offer: dict, tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    toks = tokenize_cached(" ".join((offer.get("name") or "",
                                     offer.get("vendor") or "",
                                     offer.get("category_path") or "")))
    return sum(1 for t in tokens if t in toks) / len(tokens)


def _bucket(offer: dict, coverage: float, intent: dict) -> str:
    """direct — прямое совпадение, alternative — другая линейка,
    related — сопутствующий тип (сопровождение, дополнение) или слабое совпадение.
    """
    otype = offer.get("offer_type") or "unknown"
    if coverage >= 0.99:
        if intent["name"] == "general":
            return "direct" if otype in intent["primary"] else "related"
        return "direct" if otype in intent["primary"] else "alternative"
    if coverage >= 0.5:
        return "alternative"
    return "related"


def _is_software_query(query: str) -> bool:
    """«ПО для сервера» — просили программу, а не железо."""
    return bool(re.search(r"(?<![0-9a-zа-яё])по(?![0-9a-zа-яё])|программн|софт",
                          normalize_text(query)))


# ── разговорное начало запроса ────────────────────────────────────
#
# Текст и голос попадают в один и тот же search(). «Найди Linux» и
# «покажи linux» должны искаться так же, как «linux»: иначе служебное
# слово съедает покрытие и ломает ранжирование. Правило снимает только
# ведущие повелительные/разговорные слова, остальные не трогает.

_PREFIX_WORDS = (
    "найди", "найти", "найдите", "покажи", "показать", "покажите",
    "подбери", "подобрать", "подберите", "поищи", "поискать", "поиск",
    "ищу", "ищем", "хочу", "хотим", "дай", "дайте", "купи", "купить",
    "нужен", "нужна", "нужно", "нужны", "мне", "нам", "пожалуйста",
    "плиз", "please", "show", "find", "search",
)

_PREFIX_RE = re.compile(
    r"^(?:(?:" + "|".join(_PREFIX_WORDS) + r")\b[\s,.:;!—-]*)+", re.I)


def strip_conversational_prefix(query: str) -> str:
    """Снимает «найди/покажи/подбери …» в начале запроса."""
    text = (query or "").strip()
    cleaned = _PREFIX_RE.sub("", text).strip()
    return cleaned or text


def _build_group(conn: sqlite3.Connection, key: str, query: str, q_norm: str,
                 q_tokens: list[str], q_versions: set[str], intent: dict,
                 product_tokens: list[str],
                 variants_limit: int, cache: dict | None = None,
                 reqs: dict | None = None) -> dict | None:
    scored = _score_group_offers(conn, key, query, q_norm, q_tokens, q_versions,
                                 cache, reqs)
    if not scored:
        return None
    best_offer, best_score, best_details, combined = _pick_representative(
        scored, intent, reqs)
    variants = []
    for offer, s, _ in scored[:variants_limit]:
        variants.append({
            "id": offer["id"],
            "name": offer["name"],
            "url": offer["url"],
            "price": offer["price"],
            "oldprice": offer["oldprice"],
            "currency": offer["currency"],
            "available": offer["available"],
            "score": round(s, 1),
            "offer_type": offer.get("offer_type"),
            "type_label": _type_label(offer.get("offer_type")),
        })
    coverage = _coverage(best_offer, product_tokens)
    return {
        "group_id": best_offer["group_id"],
        "key": key,
        "name": display_name(best_offer["name"]),
        "score": round(combined, 1),
        "text_score": round(best_score, 1),
        "kind": best_offer["kind"],
        "best": best_offer,
        "variants": variants,
        "variant_count": len(scored),
        "details": best_details,
        "coverage": round(coverage, 2),
        "offer_type": best_offer.get("offer_type") or "unknown",
        "type_label": _type_label(best_offer.get("offer_type")),
        "reason": _type_reason(best_offer, intent),
        "bucket": _bucket(best_offer, coverage, intent),
    }


def _fill_from_category(conn: sqlite3.Connection, anchor: dict, query: str,
                        q_norm: str, q_tokens: list[str],
                        q_versions: set[str], intent: dict,
                        product_tokens: list[str], variants_limit: int,
                        skip_keys: set[str],
                        seen_family: set[tuple[str, str]],
                        need: int, scope: str = "category",
                        cache: dict | None = None,
                        kind_filter: str | None = None,
                        reqs: dict | None = None) -> list[dict]:
    """Добирает альтернативы из той же категории каталога.

    Нужно, когда прямое совпадение одно (например, «anydesk» — одна
    линейка): показываем соседние решения той же категории с честной
    пометкой, а не дубли лицензий. scope="path" расширяет поиск до
    родительской категории, если в точной категории мало вендоров.
    """
    if need <= 0:
        return []
    order = "ORDER BY (price > 0) DESC, available DESC, price ASC LIMIT 4000"
    if scope == "category":
        category_id = anchor.get("category_id")
        if not category_id:
            return []
        rows = conn.execute(
            f"SELECT id, group_id FROM offers WHERE category_id=? {order}",
            (str(category_id),))
        scope_note = anchor.get("category_path") or "—"
    else:
        top = (anchor.get("category_path") or "").split(" / ")[0].strip()
        if not top:
            return []
        rows = conn.execute(
            f"SELECT id, group_id FROM offers WHERE category_path LIKE ? {order}",
            (top + "%",))
        scope_note = f"{top} (родительская категория)"

    keys: list[str] = []
    for row in rows:
        key = row["group_id"] or f"offer:{row['id']}"
        if key not in skip_keys and key not in keys:
            keys.append(key)

    out: list[dict] = []
    for key in keys:
        g = _build_group(conn, key, query, q_norm, q_tokens, q_versions,
                         intent, product_tokens, variants_limit, cache, reqs)
        if not g:
            continue
        if kind_filter and g["kind"] != kind_filter:
            continue
        fk = _family_key(g["best"])
        if fk in seen_family:
            continue
        seen_family.add(fk)
        g["bucket"] = ("related" if g["offer_type"] in ("support", "addon")
                       else "alternative")
        g["reason"] = ("другая линейка в той же категории каталога: "
                       + scope_note)
        out.append(g)
        if len(out) >= need:
            break
    return out


# ── однозначные опечатки в токенах запроса ────────────────────────
#
# Правило общее и опирается на данные каталога, а не на конкретный запрос.
# Если слова запроса нет в словаре индекса, ищем в словаре слово, которое
# ближе всех по числу правок (не больше двух) и встречается заметно чаще
# остальных равноудалённых кандидатов — такая замена однозначна. Правка
# применяется ДО набора кандидатов, ранжирования и разнесения по секциям,
# поэтому покрытие считается уже исправленным словом: «astra linuc»
# ищется как «astra linux» и попадает в прямые совпадения.
#
# Усечение основы опечаткой не считается: «крипто» → «крипт» не правим.

_VOCAB_CACHE: dict[int, tuple] = {}
_MIN_FIX_LEN = 4
_MIN_FIX_FREQ = 100
_FIX_DOMINANCE = 3.0
_MAX_FIX_DISTANCE = 2


def _vocab(conn: sqlite3.Connection) -> dict:
    """Частоты токенов индекса, разложенные по длине (кеш на соединение)."""
    cached = _VOCAB_CACHE.get(id(conn))
    if cached is not None and cached[0] is conn:
        return cached[1]
    freq: dict[str, int] = {}
    by_len: dict[int, list[tuple[str, int, frozenset]]] = {}
    for token, count in conn.execute(
            "SELECT token, COUNT(*) FROM offer_tokens GROUP BY token"):
        freq[token] = count
        by_len.setdefault(len(token), []).append(
            (token, count, frozenset(token)))
    data = {"freq": freq, "by_len": by_len}
    _VOCAB_CACHE[id(conn)] = (conn, data)
    return data


def _edit_distance(a: str, b: str, cap: int) -> int:
    """Расстояние Левенштейна с ранним выходом за предел cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        row_min = i
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _fix_token(token: str, vocab: dict) -> tuple[str, int] | None:
    """Однозначное исправление токена по словарю индекса (или None)."""
    if token in vocab["freq"] or len(token) < _MIN_FIX_LEN:
        return None
    variants = [token]
    # Русское написание продукта сверяем и в латинской записи:
    # «линукс» → «linuks» → «linux».
    latin = config_v3.transliterate(token)
    if latin and latin != token:
        variants.append(latin)

    best: tuple[int, int, str] | None = None
    for variant in variants:
        found: list[tuple[int, int, str]] = []
        for length in range(len(variant) - _MAX_FIX_DISTANCE,
                            len(variant) + _MAX_FIX_DISTANCE + 1):
            for cand, count, chars in vocab["by_len"].get(length, ()):
                if count < 5:
                    continue
                if len(chars & set(variant)) < len(variant) - _MAX_FIX_DISTANCE:
                    continue
                dist = _edit_distance(variant, cand, _MAX_FIX_DISTANCE)
                if dist <= _MAX_FIX_DISTANCE:
                    found.append((dist, -count, cand))
        if not found:
            continue
        found.sort()
        top = found[0]
        same = [x for x in found if x[0] == top[0]]
        runner_up = -same[1][1] if len(same) > 1 else 0
        count = -top[1]
        if count < _MIN_FIX_FREQ:
            continue
        if runner_up and count < _FIX_DOMINANCE * runner_up:
            continue
        # Усечённая основа — это стемминг, а не опечатка.
        if top[2].startswith(variant) or variant.startswith(top[2]):
            continue
        if best is None or (top[0], top[1]) < (best[0], best[1]):
            best = (top[0], top[1], top[2])
    if best is None:
        return None
    return best[2], best[0]


def fix_layout(conn: sqlite3.Connection, text: str) -> tuple[str, list[dict]]:
    """Общая нормализация раскладки: до нечёткого поиска и до вызова модели.

    Слово заменяется, только если его нет в словаре каталога и словаре
    поисковых терминов, а вариант в другой раскладке там есть. Токены с
    цифрами (артикулы, версии, архитектуры) не трогаются.
    """
    freq = _vocab(conn)["freq"]

    def known(word: str) -> bool:
        w = word.lower().replace("ё", "е")
        return w in freq or canon_token(w) in freq or term_known(w)

    return fix_keyboard_layout(text, known)


def _correct_tokens(conn: sqlite3.Connection, q_tokens: list[str],
                    q_norm: str) -> tuple[list[str], str, list[dict]]:
    """Исправляет однозначные опечатки до ранжирования."""
    vocab = _vocab(conn)
    tokens: list[str] = []
    notes: list[dict] = []
    for token in q_tokens:
        hit = _fix_token(token, vocab)
        if hit:
            notes.append({"from": token, "to": hit[0], "distance": hit[1]})
            tokens.append(hit[0])
        else:
            tokens.append(token)
    fixed_norm = q_norm
    for note in notes:
        fixed_norm = fixed_norm.replace(note["from"], note["to"])
    return tokens, fixed_norm, notes


# ── товары «для этой ОС» отдельной секцией ────────────────────────
#
# Правило по данным каталога: если лучшее прямое совпадение — операционная
# система, то предложения, в названии которых эта ОС указана как платформа
# («… для Astra Linux», «… в комплекте с Astra Linux»), — это товары и
# дополнения для неё, а не сама ОС. Они уходят в отдельную секцию, чтобы
# прямой ответ не смешивался с сопутствующими продуктами.

_OS_CATEGORY_MARK = "операционные системы"
_GENERIC_NAME_TOKENS = {
    "лиценз", "для", "на", "год", "мес", "верси", "редакц", "сертификат",
    "подписк", "шт", "электронн", "коробочн", "по",
}


def _leading_tokens(name: str, limit: int = 2) -> list[str]:
    """Ведущие слова названия без служебных слов лицензий (в порядке текста)."""
    out: list[str] = []
    for word in re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", name or ""):
        for token in sorted(tokenize(word)):
            if token in _GENERIC_NAME_TOKENS or len(token) < 2:
                continue
            out.append(token)
            if len(out) >= limit:
                return out
    return out


def _split_for_os(groups: list[dict]) -> dict | None:
    """Переносит товары «для <ОС>» в отдельную секцию. Возвращает заголовок."""
    core = next((g for g in groups if g["bucket"] == "direct"), None)
    if core is None:
        return None
    category = normalize_text(core["best"].get("category_path") or "")
    if not category.startswith(_OS_CATEGORY_MARK):
        return None
    core_tokens = _leading_tokens(core["name"])
    if len(core_tokens) < 2:
        return None
    for group in groups:
        if group is core or group["bucket"] not in ("direct", "alternative"):
            continue
        offer = group["best"]
        text_tokens = tokenize_cached(" ".join((
            offer.get("name") or "", offer.get("vendor") or "",
            offer.get("category_path") or "")))
        if not all(t in text_tokens for t in core_tokens):
            continue
        if _leading_tokens(offer.get("name") or "") == core_tokens:
            continue  # другое издание той же ОС — остаётся прямым совпадением
        group["bucket"] = "for_os"
        group["reason"] = (f"товар/дополнение для «{core['name']}»: "
                           f"эта ОС указана в названии предложения")
    return {"for_os": f"Товары и дополнения для {core['name']}"}


def search(conn: sqlite3.Connection, query: str, limit: int = 10,
           variants_limit: int = 8, reqs: dict | None = None) -> dict:
    # Один путь для текстового и голосового ввода: «Найди Linux» → «Linux».
    effective_query = strip_conversational_prefix(query)
    # Раскладку правим до нечёткого поиска: «cthdth» → «сервер», а уже потом
    # словарь каталога исправляет опечатки вроде «linuc» → «linux».
    effective_query, layout_fixes = fix_layout(conn, effective_query)
    q_norm = normalize_text(effective_query)
    q_tokens = query_tokens(effective_query)
    q_tokens, q_norm, corrections = _correct_tokens(conn, q_tokens, q_norm)
    q_versions = _versions(effective_query)
    intent = detect_intent(effective_query)
    score_cache: dict[str, tuple[float, dict]] = {}
    product_tokens = _product_tokens(q_tokens, intent)
    if not q_tokens:
        return {"query": query, "effective_query": effective_query,
                "groups": [], "sections": None, "buckets": None,
                "layout_fixes": layout_fixes, "corrections": [],
                "intent": intent, "candidates": 0, "total": 0, "shortfall": True}

    # reqs нужен для отбраковки SKU до выбора представителя группы.
    if reqs is None:
        from selection import extract_requirements
        reqs = extract_requirements(q_norm or effective_query, conn)
    else:
        # Даже переданные условия дополняем классом продукта: запрос мог
        # уточниться между репликами («Касперски» → «… для линукс»).
        import product_scope
        reqs = product_scope.refine_requirements(conn, reqs, effective_query)

    # «ОС с поддержкой» — составная потребность: покупают лицензию, а
    # поддержку как компонент. Намерение «сопровождение» ставило бы услуги
    # выше самих лицензий, поэтому здесь оно не главное.
    if (intent["name"] == "support" and reqs.get("support_component")
            and (reqs.get("product_is_os") or reqs.get("product_classes"))):
        intent = {"name": "general", "primary": _INTENT_PRIMARY["general"]}

    ids = _candidate_ids(conn, q_tokens)
    offers = _load_offers(conn, ids)
    # Отбраковка несовместимых артикулов до ранжирования групп: иначе
    # «для рабочей станции Windows» задаёт вес всему семейству, а нужный
    # «для физического сервера» вариант до представителя не доходит.
    if reqs:
        offers = [o for o in offers if _sku_allowed(o, reqs)]

    # «ПО для сервера»: оставляем только программы, иначе частотное
    # «сервер» вытесняет софт железом.
    software_only = "сервер" in q_tokens and _is_software_query(effective_query)
    if software_only:
        offers = [o for o in offers if o["kind"] == "server_software"]
        if not offers:
            return {"query": query, "effective_query": effective_query,
                    "groups": [], "sections": None,
                    "buckets": None, "intent": intent, "candidates": 0,
                    "total": 0, "shortfall": True, "software_only": True,
                    "layout_fixes": layout_fixes, "corrections": corrections}

    # Первичный отбор: лучший вариант каждой группы по релевантности текста.
    # Тип предложения здесь только разрешает ничьи: иначе бонус за базовую
    # лицензию поднимает менее релевантный товар выше точного совпадения.
    best_by_group: dict[str, tuple[dict, float, dict, float]] = {}
    for offer in offers:
        s, details = score_offer(effective_query, q_norm, q_tokens, q_versions,
                                 offer)
        score_cache[offer["id"]] = (s, details)
        combined = (s + _type_adjust(offer.get("offer_type"), intent)
                    + _class_adjust(offer, reqs))
        key = _group_key(offer)
        cur = best_by_group.get(key)
        if cur is None or s > cur[1] or (s == cur[1] and combined > cur[3]):
            best_by_group[key] = (offer, s, details, combined)

    ranked_keys = sorted(best_by_group, key=lambda k: -best_by_group[k][1])
    # Пересчитываем лучший вариант по ВСЕМ предложениям верхних групп:
    # так представителем становится нужная лицензия, а не случайный вариант.
    top_keys = ranked_keys[: max(limit * 4, 40)]

    groups = []
    for key in top_keys:
        g = _build_group(conn, key, effective_query, q_norm, q_tokens,
                         q_versions, intent, product_tokens, variants_limit,
                         score_cache, reqs)
        if not g:
            continue
        # Группа может содержать предложения разных классов: при запросе
        # «ПО для сервера» оставляем только программные карточки.
        if software_only and g["kind"] != "server_software":
            continue
        groups.append(g)

    groups.sort(key=lambda g: (-g["text_score"], -g["score"]))

    # Отсекаем карточки, где не совпало ни одного значимого слова продукта:
    # иначе «anydesk дополнительные устройства» приносит чужие «дополнения».
    relevant = [g for g in groups if g["coverage"] >= 0.5]
    if relevant:
        groups = relevant

    # Не добираем выдачу дублями лицензий одной линейки.
    seen_family: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for g in groups:
        fk = _family_key(g["best"])
        if fk in seen_family:
            continue
        seen_family.add(fk)
        unique.append(g)
    groups = unique

    # Минимум 4 товара: недостающее берём из категории лучшего совпадения.
    if len(groups) < 4:
        anchor = next((g["best"] for g in groups if g["bucket"] == "direct"),
                      groups[0]["best"] if groups else None)
        if anchor:
            skip_keys = {g["key"] for g in groups}
            # 1. та же категория
            extra = _fill_from_category(
                conn, anchor, query, q_norm, q_tokens, q_versions, intent,
                product_tokens, variants_limit, skip_keys, seen_family,
                4 - len(groups), scope="category", cache=score_cache,
                kind_filter="server_software" if software_only else None,
                reqs=reqs)
            groups.extend(extra)
            # 2. родительская категория
            if len(groups) < 4:
                skip_keys.update(g["key"] for g in extra)
                extra2 = _fill_from_category(
                    conn, anchor, query, q_norm, q_tokens, q_versions, intent,
                    product_tokens, variants_limit, skip_keys, seen_family,
                    4 - len(groups), scope="path", cache=score_cache,
                    kind_filter="server_software" if software_only else None,
                    reqs=reqs)
                groups.extend(extra2)

    # Товары «для этой ОС» — отдельной секцией, сама ОС остаётся в прямых.
    # Порядок groups остаётся по релевантности: первый элемент — лучший ответ
    # на запрос, независимо от того, в какую секцию он попал.
    bucket_titles = _split_for_os(groups) or {}

    buckets = {"direct": [], "for_os": [], "alternative": [], "related": []}
    for g in groups:
        buckets.setdefault(g["bucket"], []).append(g)

    sections = None
    if "сервер" in q_tokens:
        sections = {}
        for g in groups:
            sections.setdefault(g["kind"], []).append(g)
        sections = {k: v[:limit] for k, v in sections.items() if v}

    total = len(groups)
    return {
        "query": query,
        "effective_query": effective_query,
        "normalized": q_norm,
        "tokens": q_tokens,
        "corrections": corrections,
        "layout_fixes": layout_fixes,
        "versions": sorted(q_versions),
        "intent": intent,
        "software_only": software_only,
        "candidates": len(offers),
        "total": total,
        "shortfall": total < 4,
        "requirements": reqs,
        "groups": groups[: max(limit, 4)],
        "buckets": {k: v[: max(limit, 4)] for k, v in buckets.items() if v},
        "bucket_titles": bucket_titles,
        "sections": sections,
    }


# ── локальный базовый (простой текстовый) поиск ───────────────────
#
# Это НЕ действующий поиск сайта Softline: живой поиск магазина не
# измерялся. Простое сравнение на том же индексе: все значимые слова
# запроса должны встречаться подстрокой в названии или вендоре; порядок —
# наличие, затем короткое название, затем ID. Ни словоформы, ни версии,
# ни группировка лицензий здесь не учитываются.

_BASELINE_MIN_LEN = 2


def _ensure_py_lower(conn: sqlite3.Connection) -> None:
    """SQLite lower() не понижает регистр кириллицы — регистрируем свою."""
    conn.create_function("py_lower", 1, lambda t: t.lower() if t else "")


def baseline_text_search(conn: sqlite3.Connection, query: str,
                         limit: int = 5) -> list[dict]:
    """Простой текстовый поиск (все слова от 2 символов подстрокой в названии).

    Это по-прежнему наивный поиск, но регистр понижается Unicode-функцией:
    встроенный SQLite lower() оставлял кириллицу в верхнем регистре, из-за
    чего «кибер бэкап» и «мтс» не находились вовсе.
    """
    _ensure_py_lower(conn)

    words = [w for w in re.findall(r"[0-9A-Za-zА-Яа-яЁё]+",
                                   normalize_text(query))
             if len(w) >= _BASELINE_MIN_LEN]
    if not words:
        return []
    where = " AND ".join(
        "(py_lower(name) LIKE ? OR py_lower(vendor) LIKE ?)" for _ in words)
    params: list[str] = []
    for w in words:
        params.extend([f"%{w}%", f"%{w}%"])
    rows = conn.execute(
        f"SELECT id, group_id, name, vendor, url, price, currency, available "
        f"FROM offers WHERE {where} "
        f"ORDER BY available DESC, length(name) ASC, id ASC LIMIT ?",
        (*params, limit))
    return [dict(r) for r in rows]


def baseline_diagnose(conn: sqlite3.Connection, query: str) -> dict:
    """Пошаговая диагностика: на каком шаге базовый поиск теряет совпадение."""
    _ensure_py_lower(conn)
    raw_words = re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", normalize_text(query))
    words = [w for w in raw_words if len(w) >= _BASELINE_MIN_LEN]
    dropped = [w for w in raw_words if len(w) < _BASELINE_MIN_LEN]

    steps: list[dict] = []
    for word in words:
        pattern = f"%{word}%"
        # Настоящее наличие без учёта регистра (Unicode-функция).
        present = conn.execute(
            "SELECT COUNT(*) FROM offers WHERE py_lower(name) LIKE ? "
            "OR py_lower(vendor) LIKE ?", (pattern, pattern)).fetchone()[0]
        # Что видел старый вариант со встроенным lower() (ASCII-only).
        sqlite_lower = conn.execute(
            "SELECT COUNT(*) FROM offers WHERE lower(name) LIKE ? "
            "OR lower(vendor) LIKE ?", (pattern, pattern)).fetchone()[0]
        steps.append({
            "word": word,
            "present": present,
            "sqlite_lower_like": sqlite_lower,
            "lost_by_sqlite_lower": present - sqlite_lower,
        })

    final = len(baseline_text_search(conn, query, 5))
    # Сколько было бы найдено старым (сломанным) вариантом с lower().
    before = 0
    if words:
        where_old = " AND ".join(
            "(lower(name) LIKE ? OR lower(vendor) LIKE ?)" for _ in words)
        params_old: list[str] = []
        for w in words:
            params_old.extend([f"%{w}%", f"%{w}%"])
        before = conn.execute(
            f"SELECT COUNT(*) FROM offers WHERE {where_old}",
            params_old).fetchone()[0]

    losses = [s for s in steps if s["lost_by_sqlite_lower"] > 0]
    conclusion = ""
    if not raw_words:
        conclusion = "запрос пуст"
    elif not words:
        conclusion = ("все слова короче порога "
                      f"{_BASELINE_MIN_LEN}: {', '.join(dropped)}")
    elif losses:
        biggest = max(losses, key=lambda s: s["lost_by_sqlite_lower"])
        conclusion = (
            f"встроенный lower() в SQLite не понижает регистр кириллицы: "
            f"по слову «{biggest['word']}» он терял "
            f"{biggest['lost_by_sqlite_lower']} из {biggest['present']} карточек; "
            f"было найдено {before}, после py_lower() — {final}")
    elif final:
        conclusion = "совпадения есть, lower() ничего не потерял"
    else:
        conclusion = ("все слова найдены по отдельности, но не встречаются "
                      "вместе в одной карточке")
    return {"query": query, "words": words, "dropped": dropped,
            "steps": steps, "found": final, "found_before": before,
            "conclusion": conclusion}


# ── печать ────────────────────────────────────────────────────────

_KIND_LABELS = {
    "server_hardware": "Серверное оборудование",
    "server_component": "Комплектующие",
    "server_software": "ПО",
    "other": "Прочее",
}


def _print_group(pos: int, g: dict, show_variants: int = 3) -> None:
    best = g["best"]
    print(f"  {pos}. {g['name']} [{best['id']}]  балл {g['score']:.1f}")
    print(f"     Тип: {g.get('type_label') or '—'}"
          + (f" | фид: {best['license_type_raw']}"
             if best.get("license_type_raw") else ""))
    if best.get("platform"):
        print(f"     Платформа: {best['platform']}")
    print(f"     Почему показан: {g.get('reason') or '—'}")
    print(f"     URL: {best['url']}")
    print(f"     Вендор: {best['vendor']} | "
          f"{'в наличии' if best['available'] else 'нет в наличии'} | "
          f"{best['price'] if best['price'] is not None else '—'} "
          f"{best['currency'] or ''}".rstrip())
    print(f"     Категория: {best['category_path']}")
    if g["variant_count"] > 1:
        print(f"     Вариантов в группе: {g['variant_count']} "
              f"(показаны {min(show_variants, len(g['variants']))})")
        for v in g["variants"][:show_variants]:
            print(f"       - [{v['id']}] {v['name'][:90]}")
            print(f"         {v.get('type_label', '')} | {v['url']} | "
                  f"{v['price']} {v['currency'] or ''} | "
                  f"{'в наличии' if v['available'] else 'нет в наличии'}")


_BUCKET_LABELS = {
    "direct": "ПРЯМЫЕ СОВПАДЕНИЯ",
    "alternative": "АЛЬТЕРНАТИВЫ",
    "related": "СОПУТСТВУЮЩИЕ / ДРУГОЙ ТИП",
}


def print_search(result: dict, variants: int = 3) -> None:
    print(f"Запрос: {result['query']}")
    print(f"Токены: {', '.join(result['tokens'])}"
          + (f" | версии: {', '.join(result['versions'])}"
             if result.get("versions") else ""))
    intent = result.get("intent") or {}
    print(f"Намерение: {intent.get('name', '—')} | "
          f"главные типы: {', '.join(intent.get('primary', ()))}")
    if result.get("software_only"):
        print("Режим: только ПО (запрос «ПО для сервера»)")
    print(f"Кандидатов просмотрено: {result['candidates']} | "
          f"найдено карточек: {result.get('total', 0)}")

    if result.get("sections"):
        n = 0
        for kind, groups in result["sections"].items():
            print(f"\n--- {_KIND_LABELS.get(kind, kind)} ({len(groups)}) ---")
            for g in groups:
                n += 1
                _print_group(n, g, variants)
        if n == 0:
            print("  ничего не найдено")
        if result.get("shortfall"):
            print("\n  ТРЕБОВАНИЕ НЕ ВЫПОЛНЕНО: найдено меньше 4 товаров.")
        return

    buckets = result.get("buckets") or {}
    if not buckets:
        print("  ничего не найдено")
        return
    n = 0
    for bucket in ("direct", "alternative", "related"):
        items = buckets.get(bucket) or []
        if not items:
            continue
        print(f"\n--- {_BUCKET_LABELS[bucket]} ({len(items)}) ---")
        for g in items:
            n += 1
            _print_group(n, g, variants)
    if result.get("shortfall"):
        print(f"\n  ТРЕБОВАНИЕ НЕ ВЫПОЛНЕНО: найдено {result.get('total', 0)} "
              f"товаров вместо 4.")


# ── CLI ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Поиск по индексу организатора")
    parser.add_argument("query", nargs="?", default="", help="текст запроса")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--variants", type=int, default=3,
                        help="сколько вариантов группы печатать")
    parser.add_argument("--json", action="store_true", help="вывод JSON")
    parser.add_argument("--baseline", action="store_true",
                        help="показать локальный базовый (простой) поиск")
    parser.add_argument("--diagnose", action="store_true",
                        help="пошаговая диагностика потерь базового поиска")
    parser.add_argument("--offer", default="", help="показать предложение по ID")
    parser.add_argument("--group", default="", help="показать варианты группы")
    args = parser.parse_args(argv)

    conn = connect(Path(args.db))

    if args.diagnose:
        diag = baseline_diagnose(conn, args.query)
        if args.json:
            print(json.dumps(diag, ensure_ascii=False, indent=2))
        else:
            print(f"ДИАГНОСТИКА: «{diag['query']}»")
            print(f"Слова: {', '.join(diag['words'])}"
                  + (f" | отброшены: {', '.join(diag['dropped'])}"
                     if diag["dropped"] else ""))
            print(f"Найдено: {diag['found']} (до исправления: "
                  f"{diag['found_before']})")
            for s in diag["steps"]:
                lost = s["lost_by_sqlite_lower"]
                print(f"  «{s['word']}»: есть в каталоге {s['present']}, "
                      f"lower() видел {s['sqlite_lower_like']}"
                      + (f" → потеряно {lost} из-за lower()" if lost else ""))
            print(f"  → {diag['conclusion']}")
        return

    if args.offer:
        offer = get_offer(conn, args.offer)
        if not offer:
            print(f"Предложение не найдено: {args.offer}")
            return 1
        if args.json:
            print(json.dumps(offer, ensure_ascii=False, indent=2))
        else:
            for key in ("id", "group_id", "name", "vendor", "vendor_url", "url",
                        "category_path", "price", "oldprice", "currency",
                        "available", "kind", "params", "description"):
                print(f"{key}: {offer.get(key)}")
        return 0

    if args.group:
        rows = conn.execute(
            "SELECT id, name, url, price, oldprice, currency, available "
            "FROM offers WHERE group_id=? ORDER BY available DESC, price ASC, "
            "name ASC", (args.group,))
        offers = [dict(r) for r in rows]
        if not offers:
            print(f"Группа не найдена: {args.group}")
            return 1
        if args.json:
            print(json.dumps(offers, ensure_ascii=False, indent=2))
        else:
            print(f"Группа {args.group}: {len(offers)} вариантов")
            for o in offers:
                print(f"  [{o['id']}] {o['name']}")
                print(f"      {o['url']} | {o['price']} {o['currency'] or ''} | "
                      f"{'в наличии' if o['available'] else 'нет в наличии'}")
        return 0

    if not args.query:
        parser.error("нужен запрос или --offer/--group")

    result = search(conn, args.query, args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if args.baseline:
            print("--- локальный базовый поиск (исправленный: Unicode-нижний регистр) ---")
            base = baseline_text_search(conn, args.query, 5)
            if not base:
                print("  ничего не найдено")
            for pos, b in enumerate(base, 1):
                print(f"  {pos}. {b['name']} [{b['id']}]")
                print(f"     URL: {b['url']}")
            print()
        print_search(result, args.variants)
    return 0


if __name__ == "__main__":
    sys.exit(main())
