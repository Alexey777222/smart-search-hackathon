"""organizer_index.py — потоковый локальный индекс каталога организатора.

Источник: data/organizer/Mindbox.yml.gz.xml (YML, ~292 МБ, уже распакован).
Индекс:   data/organizer_index.sqlite (генерируется, в Git не хранится).

Что сохраняется по каждому предложению:
  id, group_id, name, vendor, vendorUrl, url, category_id, category_path,
  params (характеристики), price, oldprice, currency, available, description.

Дополнительно строится обратный индекс токенов (offer_tokens) и сводка
групп (groups) — варианты лицензий одной линейки.

CLI:
  python organizer_index.py build [--xml ...] [--db ...] [--force]
  python organizer_index.py stats
  python organizer_index.py offer <id>
  python organizer_index.py group <group_id>
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_XML = ROOT / "data" / "organizer" / "Mindbox.yml.gz.xml"
DEFAULT_DB = ROOT / "data" / "organizer_index.sqlite"

SCHEMA_VERSION = 2

# ── токенизация и нормализация ─────────────────────────────────────

_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_CAMEL_RE = re.compile(r"[A-ZА-ЯЁ]?[a-zа-яё]+|[A-ZА-ЯЁ]+(?![a-zа-яё])|[0-9]+")
_ALNUM_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+|[0-9]+")

# Служебные слова, которые не помогают выбрать товар.
STOPWORDS = {
    "для", "и", "в", "на", "с", "по", "от", "до", "из", "или", "а", "но",
    "не", "без", "под", "над", "о", "об", "при", "к", "ко", "у", "же",
    "the", "for", "and", "with", "of", "to", "in", "on",
}


def normalize_text(text: str) -> str:
    return " ".join((text or "").lower().replace("ё", "е").split())


def _stem(token: str) -> str:
    """Очень лёгкая основа для русских словоформ (без внешних зависимостей)."""
    suffixes = (
        "ами", "ями", "ов", "ев", "ей", "ой", "ый", "ая", "ое", "ые", "ие",
        "ии", "ия", "ью", "ам", "ям", "ах", "ях", "ом", "ем", "а", "я",
        "ы", "и", "у", "ю", "е", "о", "ь",
    )
    for suf in suffixes:
        if token.endswith(suf) and len(token) - len(suf) >= 3:
            return token[: -len(suf)]
    return token


def canon_token(token: str) -> str:
    """Канонический токен: нижний регистр, ё→е, лёгкая основа + домены."""
    t = token.lower().replace("ё", "е")
    if t.startswith("сервер"):
        return "сервер"
    if t.startswith("комплектующ"):
        return "комплект"
    if t.startswith("программн"):
        return "программ"
    if t.startswith("оборудован"):
        return "оборудован"
    if t.startswith("лицензи"):
        return "лицензи"
    return _stem(t)


def tokenize(text: str) -> set[str]:
    """Токены названия/категории: слова, camelCase-части и буквенно-цифровые части."""
    out: set[str] = set()
    for match in _TOKEN_RE.finditer(text or ""):
        word = match.group(0)
        low = word.lower().replace("ё", "е")
        out.add(low)
        # MaxPatrol -> max, patrol; КриптоПро -> крипто, про
        for part in _CAMEL_RE.findall(word):
            p = part.lower().replace("ё", "е")
            if len(p) >= 2:
                out.add(p)
        # Microsoft365 -> microsoft, 365
        for part in _ALNUM_RE.findall(word):
            p = part.lower().replace("ё", "е")
            if len(p) >= 2:
                out.add(p)
    return {canon_token(t) for t in out if t}


@lru_cache(maxsize=200_000)
def tokenize_cached(text: str) -> frozenset[str]:
    """Кешированная версия tokenize: пути категорий и вендоры повторяются."""
    return frozenset(tokenize(text))


def query_tokens(text: str) -> list[str]:
    seen: list[str] = []
    for tok in tokenize(text):
        if len(tok) < 2 or tok in STOPWORDS:
            continue
        if tok not in seen:
            seen.append(tok)
    return seen


# ── классификация «сервер» ────────────────────────────────────────

_SOFTWARE_MARKERS = (
    "программ", "по ", "лицензи", "операционная система", "windows server",
    "linux", "субд", "sql", "виртуализац", "антивирус", "резервное копирование",
    "бэкап", "backup", "мониторинг", "консоль", "утилита", "драйвер",
    "прошивка", "калькулятор", "редактор", "клиент", "серверное по",
)
_COMPONENT_MARKERS = (
    "процессор", "cpu", "память", "dimm", "hdd", "ssd", "накопитель", "диск",
    "материнская плата", "motherboard", "сетевая карта", "nic", "блок питания",
    "корпус", "raid", "контроллер", "видеокарта", "gpu", "кулер", "вентилятор",
    "кабель", "трансивер", "sas", "sata", "nvme", "ecc", "модуль памяти",
    "жесткий диск", "жёсткий диск", "оптический привод", "backplane",
)
_HARDWARE_MARKERS = (
    "сервер", "серверный", "стойка", "шкаф", "схд", "система хранения",
    "nas", "san", "blade", "шасси", "платформа", "poweredge", "proliant",
    "thinksystem", "primergy", "суперсервер", "серверное оборудование",
    "монтажный комплект", "источник питания", "ибп",
)


def classify_offer(name: str, category_path: str) -> str:
    """Возвращает server_hardware | server_component | server_software | other.

    Сначала решает категория (она в каталоге размечена точнее названия),
    затем — слова названия. Категория «Серверы и опции» делится на
    готовые серверы/стойки и на шасси, опции, ЗИП.
    """
    text = f"{name} {category_path}".lower().replace("ё", "е")
    cat = category_path.lower().replace("ё", "е")

    # ПО — самый сильный признак, проверяем первым.
    if any(m in cat for m in ("программное обеспечение", "программы",
                              "лицензи", "антивирус", "операционные системы")):
        return "server_software"

    if "серверы и опции" in cat:
        if any(m in cat for m in ("шасси и платформ", "серверные опции",
                                  "монтажные комплекты", "зип",
                                  "комплектующ")):
            return "server_component"
        return "server_hardware"

    if any(m in cat for m in ("системы хранения", "схд", "ибп",
                              "источники питания")):
        return "server_hardware"

    if any(m in text for m in _COMPONENT_MARKERS):
        return "server_component"
    if any(m in text for m in _SOFTWARE_MARKERS):
        return "server_software"
    if any(m in text for m in _HARDWARE_MARKERS):
        return "server_hardware"
    return "other"


# ── тип предложения: лицензия / продление / сопровождение / ... ─────
#
# Тип нужен, чтобы общий запрос не открывал cross-grade или сертификат
# сопровождения вместо базовой лицензии, а прямой запрос «продление» —
# наоборот. Основа — название предложения (оно конкретнее) и поле
# каталога «Тип лицензии» как подтверждение по данным.

OFFER_TYPE_LABELS = {
    "base": "базовая/новая лицензия",
    "subscription": "подписка",
    "renewal": "продление",
    "support": "сопровождение/техподдержка",
    "crossgrade": "cross-grade/переход",
    "upgrade": "обновление версии",
    "addon": "дополнение/доп. устройства",
    "other": "прочее (дистрибутив/восстановление)",
    "unknown": "тип не указан",
}

_NAME_TYPE_PATTERNS = (
    ("addon", re.compile(
        r"additional|add[-\s]?on|bundle of|concurrent connection|"
        r"дополнительн|расширение лицензии|expansion|модуль|модули", re.I)),
    ("support", re.compile(
        r"сертификат на сопровождение|сертификат на техническ|"
        r"сертификат техподдержк|техническ\w* поддержк|техподдержк|"
        r"сопровождение по|maintenance|support", re.I)),
    ("renewal", re.compile(r"продление|продления|продлить|renewal", re.I)),
    ("crossgrade", re.compile(
        r"cross[-\s]?grade|перекрестн|переход с|переход на новую|"
        r"переходы|миграци|migration|обмен", re.I)),
    ("upgrade", re.compile(
        r"комплект обновлений|лицензия на обновление|обновление версии|"
        r"upgrade|\bupg\b", re.I)),
    ("subscription", re.compile(
        r"подписк|аренда|web лицензи|временная лицензи", re.I)),
)

_PARAM_TYPE_MAP = {
    "полная версия": "base", "full": "base", "базовая": "base",
    "постоянные лицензии (лицензия на срок действия иск": "base",
    "постоянные лицензии": "base", "лицензия. постоянная": "base",
    "годовые лицензии": "base", "лицензионный платеж": "base",
    "учебный комплект": "base", "esd": "base",
    "подписка": "subscription", "подписка csp": "subscription",
    "аренда": "subscription", "web лицензия": "subscription",
    "лицензия. временная": "subscription",
    "продление": "renewal",
    "продление технической поддержки для постоянных лиц": "support",
    "техподдержка": "support", "maintenance": "support",
    "support": "support", "maintenance;support": "support",
    "перекрестное обновление": "crossgrade", "миграция": "crossgrade",
    "обмен": "crossgrade",
    "обновление": "upgrade", "upg": "upgrade",
    "add-on": "addon", "модуль": "addon", "модули": "addon",
    "расширение лицензии": "addon",
    "дистрибутив": "other", "восстановление лицензии": "other",
    "n/a": "unknown",
}

_TYPE_PRIORITY = ("support", "renewal", "addon", "crossgrade", "upgrade",
                  "subscription", "base")

_PLATFORM_RE = re.compile(r"Платформа:\s*([^•]+)", re.I)
_DURATION_RE = re.compile(r"Срок действия:\s*([^•]+)", re.I)


def _param_types(params: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for value in params.get("Тип лицензии", []):
        mapped = _PARAM_TYPE_MAP.get((value or "").strip().lower())
        if mapped and mapped not in out:
            out.append(mapped)
    return out


def classify_offer_type(name: str, params: dict[str, list[str]]) -> str:
    """Тип предложения по названию, затем по полю «Тип лицензии»."""
    text = name or ""
    for otype, rx in _NAME_TYPE_PATTERNS:
        if rx.search(text):
            return otype
    found = _param_types(params)
    for otype in _TYPE_PRIORITY:
        if otype in found:
            return otype
    return "unknown"


def _license_type_raw(params: dict[str, list[str]]) -> str:
    values: list[str] = []
    for value in params.get("Тип лицензии", []):
        value = (value or "").strip()
        if value and value.lower() not in {v.lower() for v in values}:
            values.append(value)
    return ", ".join(values)


def _platform(description: str, params: dict[str, list[str]]) -> str:
    match = _PLATFORM_RE.search(description or "")
    if match:
        return match.group(1).strip()
    values = [v for v in params.get("Совместимая архитектура/платформа", [])
              if v and "отсутств" not in v.lower()]
    return ", ".join(values)


def _duration(description: str, params: dict[str, list[str]]) -> str:
    match = _DURATION_RE.search(description or "")
    if match:
        return match.group(1).strip()
    values = [v for v in params.get("Срок действия", [])
              if v and "отсутств" not in v.lower()]
    return ", ".join(values)


# ── схема ──────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    name TEXT NOT NULL,
    path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
    id TEXT PRIMARY KEY,
    group_id TEXT,
    name TEXT NOT NULL,
    vendor TEXT,
    vendor_url TEXT,
    url TEXT,
    category_id TEXT,
    category_path TEXT,
    price REAL,
    oldprice REAL,
    currency TEXT,
    available INTEGER,
    params_json TEXT,
    description TEXT,
    kind TEXT,
    offer_type TEXT,
    license_type_raw TEXT,
    platform TEXT,
    duration TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    vendor TEXT,
    category_path TEXT,
    offer_count INTEGER NOT NULL,
    min_price REAL,
    available INTEGER NOT NULL,
    representative_offer_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offer_tokens (
    token TEXT NOT NULL,
    offer_id TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_offers_group ON offers(group_id);
CREATE INDEX IF NOT EXISTS idx_offers_vendor ON offers(vendor);
CREATE INDEX IF NOT EXISTS idx_offers_category ON offers(category_id);
CREATE INDEX IF NOT EXISTS idx_offers_kind ON offers(kind);
CREATE INDEX IF NOT EXISTS idx_offers_type ON offers(offer_type);
CREATE INDEX IF NOT EXISTS idx_tokens_token ON offer_tokens(token);
CREATE INDEX IF NOT EXISTS idx_tokens_offer ON offer_tokens(offer_id);
"""


def _as_float(value: str | None) -> float | None:
    if value is None:
        return None
    raw = value.strip().replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _as_bool_int(value: str | None, default: int = 0) -> int:
    if value is None:
        return default
    return 1 if value.strip().lower() in {"true", "1", "yes"} else 0


def _family_name(name: str) -> str:
    """Короткое имя линейки для карточки группы."""
    text = (name or "").strip()
    # Отрезаем хвост с вариантом лицензии: «..., лицензия, 12 мес.»
    cut = re.split(r"\s*[,(]\s*", text, maxsplit=1)[0].strip()
    return cut or text


_REP_TYPE_ORDER = {"base": 0, "subscription": 1, "upgrade": 2, "renewal": 3,
                   "addon": 4, "crossgrade": 5, "support": 6, "other": 7,
                   "unknown": 8}


def _rep_rank(offer_type: str, available: int, price: float | None) -> tuple:
    """Ключ выбора представителя группы: базовая лицензия важнее дополнений.

    Меньше — лучше: сначала тип, затем наличие, затем ненулевая цена.
    """
    return (
        _REP_TYPE_ORDER.get(offer_type, 9),
        0 if available else 1,
        0 if price and price > 0 else 1,
    )


def _category_path(cid: str | None, categories: dict[str, dict]) -> str:
    if not cid:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    cur = cid
    while cur and cur not in seen:
        seen.add(cur)
        cat = categories.get(cur)
        if not cat:
            break
        parts.append(cat["name"])
        cur = cat.get("parent_id")
    return " / ".join(reversed(parts))


# ── сборка ─────────────────────────────────────────────────────────

def build_index(xml_path: Path = DEFAULT_XML, db_path: Path = DEFAULT_DB,
                force: bool = False) -> dict:
    xml_path = Path(xml_path)
    db_path = Path(db_path)
    if not xml_path.exists():
        raise SystemExit(f"XML не найден: {xml_path}")
    if db_path.exists():
        if not force:
            raise SystemExit(f"Индекс уже существует: {db_path}\n"
                             f"Пересобрать: --force")
        db_path.unlink()

    t0 = time.time()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    categories: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    offer_count = 0
    no_group_count = 0
    token_count = 0

    offer_batch: list[tuple] = []
    token_batch: list[tuple] = []

    def flush() -> None:
        nonlocal offer_batch, token_batch
        if offer_batch:
            conn.executemany(
                "INSERT OR REPLACE INTO offers (id, group_id, name, vendor, "
                "vendor_url, url, category_id, category_path, price, oldprice, "
                "currency, available, params_json, description, kind, "
                "offer_type, license_type_raw, platform, duration) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", offer_batch)
            offer_batch = []
        if token_batch:
            conn.executemany("INSERT INTO offer_tokens (token, offer_id) "
                             "VALUES (?,?)", token_batch)
            token_batch = []

    # Первый проход: категории (они идут до <offers>, но соберём весь файл
    # до старта предложений, чтобы пути были полными).
    context = ET.iterparse(str(xml_path), events=("start", "end"))
    offers_started = False
    for event, elem in context:
        if event == "start" and elem.tag == "offers":
            offers_started = True
            break
        if event == "end" and elem.tag == "category":
            cid = elem.get("id") or ""
            categories[cid] = {
                "parent_id": elem.get("parentId") or "",
                "name": (elem.text or "").strip(),
            }
            elem.clear()
    # Дочитываем категории, если они встретились после <offers> (на практике нет).
    for cid, cat in categories.items():
        cat["path"] = _category_path(cid, categories)
    conn.executemany(
        "INSERT OR REPLACE INTO categories (id, parent_id, name, path) "
        "VALUES (?,?,?,?)",
        [(cid, c["parent_id"], c["name"], c["path"])
         for cid, c in categories.items()])
    conn.commit()

    # Второй проход: предложения.
    context = ET.iterparse(str(xml_path), events=("end",))
    for event, elem in context:
        if elem.tag != "offer":
            continue
        oid = (elem.get("id") or "").strip()
        if not oid:
            elem.clear()
            continue

        group_id = (elem.get("group_id") or "").strip()
        if not group_id:
            group_id = (elem.findtext("group_id") or "").strip()

        name = (elem.findtext("name") or "").strip()
        vendor = (elem.findtext("vendor") or "").strip()
        vendor_url = (elem.findtext("vendorUrl") or "").strip()
        url = (elem.findtext("url") or "").strip()
        category_id = (elem.findtext("categoryId") or "").strip()
        category_path = _category_path(category_id, categories)
        price = _as_float(elem.findtext("price"))
        oldprice = _as_float(elem.findtext("oldprice"))
        currency = (elem.findtext("currencyId") or "").strip()
        available = _as_bool_int(elem.get("available"),
                                 _as_bool_int(elem.findtext("available")))
        description = (elem.findtext("description") or "").strip()

        params: dict[str, list[str]] = {}
        for param in elem.findall("param"):
            pname = (param.get("name") or "").strip()
            pvalue = (param.text or "").strip()
            if not pname:
                continue
            params.setdefault(pname, []).append(pvalue)

        kind = classify_offer(name, category_path)
        offer_type = classify_offer_type(name, params)
        license_type_raw = _license_type_raw(params)
        platform = _platform(description, params)
        duration = _duration(description, params)
        offer_batch.append((
            oid, group_id or None, name, vendor, vendor_url, url,
            category_id, category_path, price, oldprice, currency,
            available, json.dumps(params, ensure_ascii=False), description,
            kind, offer_type, license_type_raw, platform, duration,
        ))

        # Токены: название + вендор + путь категории.
        text_for_tokens = " ".join((name, vendor, category_path))
        for tok in tokenize(text_for_tokens):
            token_batch.append((tok, oid))
            token_count += 1

        if group_id:
            g = groups.get(group_id)
            rank = _rep_rank(offer_type, available, price)
            if g is None:
                groups[group_id] = {
                    "name": _family_name(name),
                    "vendor": vendor,
                    "category_path": category_path,
                    "offer_count": 1,
                    "min_price": price,
                    "available": available,
                    "representative_offer_id": oid,
                    "rep_rank": rank,
                    "rep_offer_type": offer_type,
                }
            else:
                g["offer_count"] += 1
                g["available"] = max(g["available"], available)
                if price is not None and price > 0:
                    if g["min_price"] is None or g["min_price"] <= 0 \
                            or price < g["min_price"]:
                        g["min_price"] = price
                if rank < g["rep_rank"]:
                    g["rep_rank"] = rank
                    g["representative_offer_id"] = oid
                    g["rep_offer_type"] = offer_type
        else:
            no_group_count += 1

        offer_count += 1
        elem.clear()

        if offer_count % 2000 == 0:
            flush()
            conn.commit()
            print(f"\r  обработано предложений: {offer_count:,}".replace(",", " "),
                  end="", flush=True)

    flush()
    conn.commit()
    print(f"\r  обработано предложений: {offer_count:,}".replace(",", " "))

    # Сводка групп.
    conn.executemany(
        "INSERT OR REPLACE INTO groups (group_id, name, vendor, category_path, "
        "offer_count, min_price, available, representative_offer_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(gid, g["name"], g["vendor"], g["category_path"], g["offer_count"],
          g["min_price"], g["available"], g["representative_offer_id"])
         for gid, g in groups.items()])
    conn.commit()

    # Индексы — после массовой вставки.
    conn.executescript(INDEXES)
    conn.commit()

    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "source": str(xml_path),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "offer_count": str(offer_count),
        "group_count": str(len(groups)),
        "no_group_count": str(no_group_count),
        "category_count": str(len(categories)),
        "token_count": str(token_count),
        "build_seconds": f"{time.time() - t0:.1f}",
    }
    conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                     list(meta.items()))
    conn.commit()
    conn.close()

    return {
        "db": str(db_path),
        "offers": offer_count,
        "groups": len(groups),
        "no_group": no_group_count,
        "categories": len(categories),
        "tokens": token_count,
        "seconds": round(time.time() - t0, 1),
    }


# ── чтение индекса ─────────────────────────────────────────────────

def connect(db_path: Path = DEFAULT_DB,
            check_same_thread: bool = True) -> sqlite3.Connection:
    db_path = Path(db_path)
    if not db_path.exists():
        raise SystemExit(
            f"Индекс не найден: {db_path}\n"
            f"Соберите его: python organizer_index.py build")
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    return conn


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM meta")}


def get_offer(conn: sqlite3.Connection, offer_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM offers WHERE id = ?",
                       (str(offer_id),)).fetchone()
    return _offer_row(row) if row else None


def get_group(conn: sqlite3.Connection, group_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM groups WHERE group_id = ?",
                       (str(group_id),)).fetchone()
    if not row:
        return None
    offers = [ _offer_row(r) for r in conn.execute(
        "SELECT * FROM offers WHERE group_id = ? ORDER BY available DESC, "
        "price ASC, name ASC", (str(group_id),))]
    return {"group": dict(row), "offers": offers}


def _offer_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["params"] = json.loads(data.pop("params_json") or "{}")
    data["available"] = bool(data["available"])
    return data


# ── CLI ────────────────────────────────────────────────────────────

def _print_offer(offer: dict) -> None:
    print(f"ID: {offer['id']}")
    print(f"group_id: {offer['group_id'] or '—'}")
    print(f"Название: {offer['name']}")
    print(f"Вендор: {offer['vendor']}")
    print(f"vendorUrl: {offer['vendor_url']}")
    print(f"URL товара: {offer['url']}")
    print(f"Категория: {offer['category_path']}")
    print(f"Цена: {offer['price']} {offer['currency'] or ''}".rstrip())
    print(f"Старая цена: {offer['oldprice']}")
    print(f"Доступность: {'да' if offer['available'] else 'нет'}")
    print(f"Тип: {offer['kind']}")
    print(f"Тип предложения: {OFFER_TYPE_LABELS.get(offer.get('offer_type') or 'unknown')}"
          f" [{offer.get('offer_type') or 'unknown'}]")
    if offer.get("license_type_raw"):
        print(f"Тип лицензии (фид): {offer['license_type_raw']}")
    if offer.get("platform"):
        print(f"Платформа: {offer['platform']}")
    if offer.get("duration"):
        print(f"Срок действия: {offer['duration']}")
    if offer["params"]:
        print("Характеристики:")
        for name, values in offer["params"].items():
            print(f"  {name}: {', '.join(values)}")
    if offer["description"]:
        print(f"Описание: {offer['description']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Локальный индекс каталога организатора")
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="собрать индекс из XML")
    b.add_argument("--xml", default=str(DEFAULT_XML))
    b.add_argument("--db", default=str(DEFAULT_DB))
    b.add_argument("--force", action="store_true")

    sub.add_parser("stats", help="показать статистику индекса")

    o = sub.add_parser("offer", help="показать предложение по ID")
    o.add_argument("offer_id")

    g = sub.add_parser("group", help="показать варианты группы")
    g.add_argument("group_id")

    args = parser.parse_args(argv)

    if args.cmd == "build":
        result = build_index(Path(args.xml), Path(args.db), args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    conn = connect(Path(getattr(args, "db", DEFAULT_DB)))
    if args.cmd == "stats":
        print(json.dumps(get_meta(conn), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "offer":
        offer = get_offer(conn, args.offer_id)
        if not offer:
            print(f"Предложение не найдено: {args.offer_id}")
            return 1
        _print_offer(offer)
        return 0
    if args.cmd == "group":
        data = get_group(conn, args.group_id)
        if not data:
            print(f"Группа не найдена: {args.group_id}")
            return 1
        print(f"Группа {data['group']['group_id']}: {data['group']['name']} "
              f"({data['group']['offer_count']} вариантов)")
        for offer in data["offers"]:
            print(f"  [{offer['id']}] {offer['name']}")
            print(f"      {offer['url']} | {offer['price']} "
                  f"{offer['currency'] or ''} | "
                  f"{'в наличии' if offer['available'] else 'нет в наличии'}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
