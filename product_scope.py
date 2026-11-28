"""product_scope.py — что покупатель ищет и что является лишь ограничением.

Модуль отвечает на два разных вопроса, которые раньше были смешаны:

1. Какой КЛАСС продукта нужен покупателю (антивирус, резервное копирование,
   операционная система, офисный пакет…). Класс задаётся обобщённой
   таксономией: домены сопоставлены с фактическими категориями каталога,
   а производитель относится к домену по категориям своих же товаров.
   Списков «плохих» артикулов здесь нет.
2. Является ли упомянутая ОС самим продуктом или только условием
   совместимости. «Антивирус для Linux» — продукт антивирус, Linux —
   ограничение; «нужна операционная система» — продукт ОС.

Из этого следует общее правило: операционная система не может быть
функциональным аналогом антивируса, средства резервного копирования или
любого другого не-ОС класса. Правило не привязано к конкретному вендору.
"""

from __future__ import annotations

import re

from organizer_index import normalize_text

# ── обобщённая таксономия функциональных классов ──────────────────
#
# `markers` — основы слов, которыми покупатель называет задачу
#   (сравнение по префиксу, поэтому «антивирус» ловит «антивирусы»).
# `categories` — подстроки фактических category_path каталога, которые
#   определяют принадлежность артикула к классу.
# Набор проверен по индексу: каждый класс реально присутствует в каталоге.

DOMAINS: dict[str, dict] = {
    "os": {
        "markers": ("операционн", "linux", "windows", "астра", "astra",
                    "redos", "редос", "unix", "macos", "дистрибутив"),
        "categories": ("операционные системы",),
    },
    "antivirus": {
        "markers": ("антивирус", "защиты информации", "защита информации",
                    "кибербезопасн", "информационн безопасн", "security",
                    "endpoint", "sandbox"),
        "categories": ("антивирусы и безопасность",),
    },
    "backup": {
        "markers": ("резервн", "бэкап", "backup", "восстановлен",
                    "репликац", "recover"),
        "categories": ("резервного копирования",),
    },
    "office": {
        # «офисный компьютер» — это рабочая станция, а не офисный пакет,
        # поэтому класс офисного ПО требуем только с уточняющим словом.
        # «365» покупатель пишет, когда имеет в виду офисный пакет
        # Microsoft 365 — это название класса, а не конкретного артикула.
        "markers": ("офисн пакет", "офисн программ", "офисн по",
                    "документооборот", "word", "excel", "office", "365"),
        "categories": ("офисные программы",),
    },
    "db": {
        "markers": ("субд", "баз данных", "базы данных", "database"),
        "categories": ("работы с базами данных",),
    },
    "virtualization": {
        "markers": ("виртуализац", "гипервизор", "virtual"),
        "categories": ("виртуализаци",),
    },
    "crm": {
        "markers": ("crm", "erp", "управления бизнесом", "автоматизац"),
        "categories": ("управление бизнесом",),
    },
    "graphics": {
        "markers": ("график", "дизайн", "сапр", "гис", "3d"),
        "categories": ("графики и дизайна", "сапр и гис"),
    },
    "network": {
        "markers": ("сетев", "интернет"),
        "categories": ("сеть и интернет",),
    },
    "dev": {
        "markers": ("разработк", "программирован", "библиотек"),
        "categories": ("программирования",),
    },
    "system": {
        "markers": ("настройк системы", "расширени системы", "утилит"),
        "categories": ("системные программы",),
    },
    "hardware": {
        "markers": ("сервер", "комплектующ", "оборудован", "железо"),
        "categories": ("серверы и опции", "комплектующие"),
    },
}

# Аппаратные категории каталога: это изделие, а не программа. Оборудование
# не может быть функциональным аналогом программного класса, даже если в
# названии модели встретился тот же токен, что в запросе («MM-365» — модель
# мыши, а не офисный пакет).
_HARDWARE_CATEGORIES = ("компьютерная периферия", "комплектующие",
                        "серверы и опции", "расходные", "нкту",
                        "оборудование")

# Слова, которые не могут быть названием производителя. Сюда входят
# служебные слова, названия классов продуктов и общие русские слова,
# которые встречаются в именах вендоров каталога («Системы…», «Российские…»)
# и не должны распознаваться как бренд.
_NOT_BRAND = {
    "linux", "линукс", "windows", "виндовс", "астра", "astra", "unix",
    "macos", "redos", "редос", "альт",
    "рос", "росси", "российск", "отечествен", "систем", "программ",
    "обеспечен", "лаборатор", "группа", "софт", "software", "корпорац",
    "компан", "ооо", "нпо", "нпп", "нпф", "ао", "технолог", "центр",
    "информ", "сервис", "решение", "решени", "продукт", "виртуализаци",
    "операционн", "управлени", "защит", "средств", "комплекс", "платформ",
    "научн", "производств", "баз", "данн", "офисн", "компьютер", "рабоч",
    "станци", "поддержк", "техническ", "возможн", "лиценз", "сервер",
    "серверн", "сетев", "интернет", "разработк", "мобильн", "облачн",
}

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _translit(word: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in word)

# Признаки того, что ОС — условие совместимости, а не продукт.
_OS_CONSTRAINT_RE = re.compile(
    r"(?:для|под|на|совместим\w*\s+с|работа\w*\s+(?:на|под|в))\s+"
    r"(?:astra|астра|linux|линукс|windows|виндовс|ред\s*ос|redos|альт|"
    r"рос\w*|unix|macos)", re.I)

# Слово «сервер» в запросе про ПО — это платформа, а не товар-железо.
_HINT_SOFTWARE = re.compile(r"(?:^|\s)(?:по|программн\w*|софт)(?:\s|$)")

# «ОС» аббревиатурой: текст нормализуется к нижнему регистру, поэтому
# границы слова отделяют её от «мойофис», «основа» и «остров».
_OS_ABBREV_RE = re.compile(r"(?:^|[^0-9a-zа-яё])(?:ос|os)(?:$|[^0-9a-zа-яё])")

# «с поддержкой», «с возможностью технической поддержки», «с сопровождением»
# — поддержка как компонент, а не покупаемый тип лицензии.
_SUPPORT_COMPONENT_RE = re.compile(
    r"\bс\s+(?:\w+\s+){0,3}(?:поддержк|сопровожден|техподдержк)")

_CACHE: dict = {}


def _top_level(category_path: str) -> str:
    return (category_path or "").split(" / ")[0].strip()


def _norm(s: str) -> str:
    return normalize_text(s or "")


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[0-9a-zа-яё]+", _norm(text)) if len(w) >= 2]


def _stem(word: str) -> str:
    """Грубая основа слова: отсекаем частые русские окончания.

    Достаточно для сравнения по префиксу: «касперски» и «касперского»
    дают одну основу «касперск», «антивирус» и «антивирусы» — «антивирус».
    """
    word = word.lower()
    for suffix in ("иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми",
                   "ими", "ей", "ой", "ый", "ий", "ая", "яя", "ое", "ее",
                   "ые", "ие", "ов", "ев", "ам", "ям", "ах", "ях", "ом",
                   "ем", "ую", "юю", "а", "я", "о", "е", "ы", "и", "у",
                   "ю", "ь"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[:-len(suffix)]
    return word


def _stem_match(token: str, marker: str) -> bool:
    """Совпадает ли основа слова покупателя с маркером класса.

    Токен может быть длиннее маркера (падеж, производное: «антивирус» →
    «антивирусы», «касперского» → «касперск»), поэтому сравниваем его как
    продолжение маркера. Обратное направление допустимо только для близких
    по длине основ: «кибер» НЕ равен маркеру «кибербезопасн».
    """
    if token.startswith(marker):
        return True
    return (marker.startswith(token)
            and len(token) >= max(4, len(marker) - 3))


def _hits(text: str, markers) -> bool:
    """Есть ли в тексте класс, названный одним из маркеров.

    Маркер — это фраза из одного или нескольких слов («баз данных»,
    «офисн пакет»). Слова маркера должны стоять в тексте друг за другом
    подряд (по основам), иначе «офисный компьютер» ложно даст офисный
    пакет, а «база знаний» — СУБД. Сравнение по основам, поэтому падежи
    не мешают: «антивирус»/«антивирусы», «касперски»/«касперского».
    """
    stems = [_stem(w) for w in _words(text)]
    for marker in markers:
        parts = [_stem(p) for p in (_words(marker) or [marker])]
        n = len(parts)
        if not n:
            continue
        for i in range(len(stems) - n + 1):
            if all(_stem_match(stems[i + j], parts[j]) for j in range(n)):
                return True
    return False


def domains_of_text(text: str) -> set[str]:
    """Функциональные классы, названные в тексте запроса или названия."""
    domains = {name for name, spec in DOMAINS.items()
               if _hits(text, spec["markers"])}
    if _OS_ABBREV_RE.search(_norm(text)):
        domains.add("os")
    return domains


def domains_of_offer(offer: dict) -> set[str]:
    """Функциональные классы артикула по его категории каталога."""
    path = _norm(offer.get("category_path") or "")
    return {name for name, spec in DOMAINS.items()
            if any(cat in path for cat in spec["categories"])}


def name_classes(offer: dict) -> set[str]:
    """Классы, названные в самом названии артикула.

    Название точнее категории: средство восстановления, ошибочно отнесённое
    к «Операционным системам», остаётся средством восстановления, а
    сопровождение «на ОС Astra Linux» — услугой к операционной системе.
    """
    return domains_of_text(offer.get("name") or "")


def is_hardware_offer(offer: dict) -> bool:
    """Артикул — изделие (периферия, комплектующие, оборудование)."""
    path = _norm(offer.get("category_path") or "")
    return any(cat in path for cat in _HARDWARE_CATEGORIES)


def wanted_classes(reqs: dict) -> set[str]:
    """Какие классы удовлетворяют запрос покупателя.

    Если класс назван словами — берём его. Если покупатель просит
    операционную систему («нужна ОС», «российская офисная ОС»), класс —
    операционные системы, даже если отдельный класс не упоминался.
    """
    reqs = reqs or {}
    classes = set(reqs.get("product_classes") or [])
    if classes:
        return classes
    if reqs.get("product_is_os") or reqs.get("wants_os"):
        return {"os"}
    return set()


def _vendor_stems(conn) -> set[str]:
    cached = _CACHE.get(("vendors", id(conn)))
    if cached is not None:
        return cached
    stems: set[str] = set()
    for (vendor,) in conn.execute(
            "SELECT DISTINCT vendor FROM offers WHERE vendor IS NOT NULL "
            "AND vendor != ''"):
        for word in _words(vendor):
            stem = _stem(word)
            stems.add(stem)
            # Латиница и кириллица должны сходиться: «kaspersky» в запросе
            # и «Касперского» в вендоре — один бренд.
            latin = _translit(stem)
            if latin != stem:
                stems.add(latin)
    _CACHE[("vendors", id(conn))] = stems
    return stems


def brand_in(text: str, conn) -> str:
    """Названный в тексте производитель (самая длинная основа).

    Слово запроса считается брендом, только если оно не служебное и не
    название класса продукта (`_NOT_BRAND`). Это отсекает «система»,
    «российская», «офисный» и подобные общие слова, которые встречаются
    в именах вендоров каталога, но брендом не являются.
    """
    stems = {_stem(w) for w in _words(text)}
    best = ""
    for token in _vendor_stems(conn):
        if token in _NOT_BRAND or len(token) < 4:
            continue
        if any(stem.startswith(token) and stem not in _NOT_BRAND
               for stem in stems) and len(token) > len(best):
            best = token
    return best


def _vendor_domains(conn, brand_stem: str) -> set[str]:
    """Классы товаров, которые производитель реально продаёт в каталоге."""
    key = ("vendor_domains", id(conn), brand_stem)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    domains: set[str] = set()
    for vendor, path in conn.execute(
            "SELECT vendor, category_path FROM offers WHERE vendor IS NOT NULL "
            "AND vendor != ''"):
        stems = set()
        for word in _words(vendor):
            stem = _stem(word)
            stems.add(stem)
            stems.add(_translit(stem))
        if brand_stem not in stems:
            continue
        domains |= domains_of_offer({"category_path": path})
    _CACHE[key] = domains
    return domains


def is_os_category(name: str) -> bool:
    return "операционные системы" in _norm(name)


def offer_class(offer: dict) -> str:
    """Верхнеуровневый класс каталога для карточки."""
    return _top_level(offer.get("category_path") or "")


def resolve(conn, text: str) -> dict:
    """Класс продукта и роль упомянутой ОС для текста запроса.

    Класс продукта складывается из того, что покупатель назвал словами,
    и — если назван только бренд без класса — из категорий товаров этого
    бренда в каталоге. Домены вендора НЕ добавляются к уже названному
    классу: иначе «российская ОС» через вендоров приносит CRM и прочее.

    Когда покупатель называет только ОС-маркеры (например, «астра линукс»),
    продукт — операционная система, а не вендоровы программы. Домены
    вендора используются только если ОС явно выступает ограничением
    («Kaspersky для Astra Linux»).
    """
    named = domains_of_text(text)
    brand = brand_in(text, conn)
    brand_domains = _vendor_domains(conn, brand) if brand else set()

    os_constraint_hint = bool(_OS_CONSTRAINT_RE.search(_norm(text)))
    # Класс продукта — всё, что названо и не является ОС-ограничением.
    product_named = named - {"os"}
    # Домены вендора определяют продукт, только когда класс не назван
    # словами. Исключение: если названа одна ОС без признака ограничения
    # («астра линукс») — продукт именно ОС, а не программы вендора.
    if not product_named and brand_domains:
        # Названа только ОС — продукт именно ОС, а не программы вендора
        # («астра линукс»). Но только если бренд вообще выпускает
        # операционные системы: «касперски linux» — это антивирус, где
        # Linux названа платформой, а не товаром.
        only_os = ("os" in named and not os_constraint_hint
                   and not (brand_domains and "os" not in brand_domains))
        if not only_os:
            product_named = brand_domains - {"os"}

    # «Сервер» в запросе может означать и платформу, и железо.
    # Если текст содержит указание на ПО («по», «программ») или есть
    # другие классы — «сервер» выступает как платформа, а не товар.
    if "hardware" in product_named:
        software_hint = bool(_HINT_SOFTWARE.search(_norm(text)))
        if software_hint or len(product_named) > 1:
            product_named.discard("hardware")

    product_is_os = ("os" in named) and not product_named
    os_named = "os" in named or os_constraint_hint
    os_is_constraint = os_named and bool(product_named)

    return {
        "classes": sorted(named | (brand_domains if not named else set())),
        "product_classes": sorted(product_named),
        "product_is_os": product_is_os,
        "os_is_constraint": os_is_constraint,
        "os_named": os_named,
        "brand": brand,
    }


def refine_requirements(conn, reqs: dict, text: str) -> dict:
    """Дополняет условия поиска классом продукта и ролью ОС.

    Возвращает новый словарь: исходные regex-условия сохраняются, но
    `wants_os` пересчитывается по фактическому классу продукта. Это то
    место, где «Касперски для линукс» перестаёт быть запросом про ОС.

    Отдельно разбирается составная потребность: «российская ОС … с
    возможностью технической поддержки» — это базовая лицензия ОС плюс
    поддержка как компонент, а не запрос только на услугу сопровождения.
    Поэтому `license_type=support` снимается, а запрос помечается
    `needs_support`.
    """
    scope = resolve(conn, text)
    out = dict(reqs or {})
    out["product_classes"] = scope["product_classes"]
    out["product_is_os"] = scope["product_is_os"]
    out["os_is_constraint"] = scope["os_is_constraint"]
    out["scope_brand"] = scope["brand"]

    # Платформа остаётся обязательным условием, только когда ОС названа
    # условием совместимости и конкретная ОС не опознана: «антивирус для
    # Windows». Если покупают ОС («Linux», «российская ОС») или опознана
    # конкретная ОС («… на Astra Linux»), проверка идёт по классу и
    # правилу ОС — требование платформы там только мешает.
    if out.get("platform_family") and not (scope["os_is_constraint"]
                                           and not out.get("os")):
        out["platform_family"] = None
    if scope["product_is_os"]:
        out["wants_os"] = True
    elif scope["os_is_constraint"]:
        # ОС названа как условие совместимости: покупают не её.
        out["wants_os"] = False

    # Поддержка как компонент («с поддержкой», «с возможностью
    # технической поддержки»), а не как покупаемый тип лицензии.
    if out.get("license_type") == "support":
        component = bool(_SUPPORT_COMPONENT_RE.search(_norm(text)))
        if component or scope["product_is_os"] or scope["product_classes"]:
            out["license_type"] = None
            out["needs_support"] = True
            # Покупает лицензию, а поддержку — как компонент. Это не
            # запрос услуги сопровождения («кибер бэкап сопровождение»).
            out["support_component"] = component
    return out


def functional_conflict(offer: dict, reqs: dict) -> str:
    """Артикул другого класса не доказывает функциональную эквивалентность.

    Правило двустороннее и не привязано к вендору:

    - просят не-ОС класс (антивирус, резервное копирование…) — операционная
      система не может быть его функциональным аналогом;
    - просят операционную систему — артикул, который по своему названию
      является другим классом ПО (средство восстановления, офисный пакет…),
      не является операционной системой, даже если лежит в категории
      «Операционные системы».

    Категория каталога не всегда точна, поэтому название артикула
    приоритетнее категории.
    """
    if not reqs:
        return ""
    wanted = wanted_classes(reqs)
    if not wanted:
        return ""
    named = name_classes(offer)
    cat = domains_of_offer(offer)
    # Роль лицензии («серверная», «для рабочей станции») классом продукта
    # не является. Если запрошена именно ОС, а в названии появился другой
    # класс («резервное копирование ОС…»), то ОС названа объектом действия,
    # а не продуктом.
    foreign = named - wanted - {"hardware"}
    names_other = bool(named) and not (named & wanted)
    if wanted == {"os"} and foreign:
        names_other = True
    if not names_other:
        return ""
    if cat & wanted:
        return (f"по названию это другой класс продукта "
                f"({', '.join(sorted(named))}), а запрошено: "
                f"{', '.join(sorted(wanted))}")
    if "os" in cat:
        return (f"это операционная система, а нужен другой класс продукта "
                f"({', '.join(sorted(wanted))}) — ОС не является "
                f"функциональным аналогом")
    return ""


def class_relation(offer: dict, reqs: dict) -> str:
    """Насколько класс артикула близок к запрошенному: same / other / unknown.

    Название артикула приоритетнее категории каталога: категория неточна
    (средство восстановления лежит в категории «Операционные системы»).
    Сопутствующая услуга — сопровождение, продление, дополнение, подписка —
    относится к нужному классу только тогда, когда этот класс назван в её
    собственном названии: «Granulex Recovery (сертификат на продление)» —
    продление средства восстановления, а лицензия на агента из категории
    CRM к антивирусу отношения не имеет.

    Оборудование (периферия, комплектующие) не бывает аналогом
    программного класса.
    """
    wanted = wanted_classes(reqs)
    if not wanted:
        return "unknown"
    if is_hardware_offer(offer) and "hardware" not in wanted:
        return "other"
    named = name_classes(offer)
    if named:
        foreign = named - wanted - {"hardware"}
        if wanted == {"os"} and foreign:
            return "other"
        return "same" if named & wanted else "other"
    cat = domains_of_offer(offer)
    if not cat:
        return "unknown"
    return "same" if cat & wanted else "other"
