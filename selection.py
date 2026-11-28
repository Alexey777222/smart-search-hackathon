"""selection.py — явный топ-4 и программно проверяемые допродажи.

Модуль работает поверх готового `organizer_search.search()`: индекс и
ранжирование не дублируются. Он решает три задачи, которые нельзя
выразить одной сортировкой:

1. Топ-4 из четырёх РАЗНЫХ продуктовых семейств. Семейство проверяется
   по group_id и данным каталога, а не по вендору или категории.
   Прямые совпадения идут первыми, затем — проверенные сопутствующие
   товары и функциональные аналоги с явной ролью.
2. Среди сопоставимых по релевантности SKU выше стоят доступные позиции
   с положительной ценой; недоступные и без цены — резерв.
3. Допродажа проверяется по характеристикам конкретного SKU
   (<param> и однозначные поля фида). Неподтверждённая обязательная
   характеристика не даёт попасть в проверенные допродажи.

Правило совместимости — структурированное: область применимости,
поля-доказательства и ссылка на документацию вендора.
"""

from __future__ import annotations

import json
import re

import organizer_search as osr
import product_scope
import sku_rules
from organizer_index import OFFER_TYPE_LABELS, normalize_text

TOP4_SIZE = 4

ROLE_DIRECT = "прямое совпадение"
ROLE_RELATED = "сопутствующий товар"
ROLE_ANALOG = "функциональный аналог"

# ── структурированные правила совместимости ───────────────────────
#
# Каждое правило задаёт точную область применимости: какие ОС/продукты
# оно покрывает, где в данных SKU искать подтверждение и на какую
# документацию вендора опирается вывод. Правило не подменяет факты
# фида: если подтверждения нет, характеристика считается неизвестной.

# Платформенное семейство ОС: нужно, чтобы отличить жёсткий конфликт
# («SKU только для Windows» при обязательной Astra Linux) от неизвестной
# характеристики (пустая или смешанная платформа).
_PLATFORM_FAMILIES = {
    "linux": ("linux", "астра", "astralinux", "redos", "ред ос", "альт",
              "centos", "debian", "ubuntu", "rhel", "suse", "рос", "unix",
              "аврора"),
    "windows": ("windows", "виндовс"),
}

COMPAT_RULES = (
    {
        "id": "astra-linux-se",
        "kind": "os",
        "product": "Astra Linux Special Edition",
        "platform_family": "linux",
        # aliases — для распознавания ОС в запросе (можно короткие формы).
        "aliases": ("astra linux", "астра линукс", "астра", "astralinux"),
        # evidence — для подтверждения в данных SKU: только однозначные
        # формы, иначе вендор «Группа Астра» ложно подтвердит совместимость.
        "evidence": ("astra linux", "астра линукс", "astralinux"),
        "fields": ("name", "platform", "params"),
        # Кодовые имена редакций подтверждены данными фида:
        # 1.7 → «Смоленск», 1.8 → «Орел»/«Воронеж».
        "codenames": {
            "1.6": ("смоленск",),
            "1.7": ("смоленск",),
            "1.8": ("орел", "воронеж"),
        },
        "doc": "https://wiki.astralinux.ru/",
        "doc_title": "Документация Astra Linux",
        "scope": ("ОС Astra Linux Special Edition: совместимость продукта "
                  "подтверждается названием SKU или полем «Совместимая "
                  "архитектура/платформа»; версия — цифрой или кодовым именем"),
    },
    {
        "id": "cyber-backup",
        "kind": "product",
        "product": "Кибер Бэкап",
        "aliases": ("кибер бэкап", "киберпротект", "cyber backup", "cyberbackup"),
        "evidence": ("кибер бэкап", "киберпротект", "cyber backup",
                     "cyberbackup"),
        "fields": ("name", "platform", "params"),
        "codenames": {},
        "doc": ("https://cyberprotect.ru/podderzhivaemyie-texnologii/"
                "cyber-backup-astra-linux-specialedition/"),
        "doc_title": "Кибер Бэкап: поддерживаемые технологии",
        "scope": ("Кибер Бэкап: поддержка Astra Linux Special Edition "
                  "подтверждена публикацией вендора; конкретная версия ОС "
                  "в фиде не указана"),
    },
)

_RULES_BY_ID = {rule["id"]: rule for rule in COMPAT_RULES}

# Служебные ключи параметров, где лежит «другое или отсутствует».
_PARAM_EMPTY = {"другое или отсутствует", "не определено", "", "нет данных"}

_OS_QUERY_RE = re.compile(
    r"linux|линукс|операционн\w*\s+систем|(?:^|\s)ос(?:\s|$)|windows|ред\s*ос|"
    r"альт\s+сервер|астра")

_SERVER_NAME_RE = re.compile(r"^(сервер|серверная платформа|шасси)\b")

_ARCH_ALIASES = {
    "x64": "x86-64", "x86_64": "x86-64", "x86-64": "x86-64",
    "aarch64": "aarch64", "arm64": "aarch64", "arm": "arm",
    "эльбрус": "e2k", "e2k": "e2k",
}

_LICENSE_INTENT = (
    ("renewal", re.compile(r"продлен|продлить|renew", re.I)),
    ("support", re.compile(r"сопровожд|техподдержк|поддержк", re.I)),
    ("addon", re.compile(r"дополнительн|доп\.? ?устройств|add-?on|докупить",
                         re.I)),
    ("subscription", re.compile(r"подписк|аренд", re.I)),
)

# Отрицание: «без windows», «не windows», «кроме windows», «исключая windows».
_NEGATION_RE = re.compile(
    r"(?:^|\s)(?:без|не|кроме|исключая)\s+([0-9a-zа-яё][0-9a-zа-яё\-]*)")

_WINDOWS_WORDS = {"windows", "виндовс", "win"}

# Назначение из запроса: что именно покупатель собирается защищать.
_SERVER_WORDS = re.compile(r"сервер|серверн")
_WORKSTATION_WORDS = re.compile(
    r"рабоч\w*\s+станци|ноутбук|рабоч\w*\s+мест|(?:^|\s)пк(?:\s|$)")

# Что нужно восстанавливать. Уточняется вопросом консультанта.
_SCOPE_FULL_SERVER = re.compile(
    r"весь\s+сервер|образ\s+сервера|сервер\s+целиком|системн\w*\s+образ|"
    r"полн\w*\s+восстановлен|всего\s+сервер")
_SCOPE_FILES = re.compile(r"отдельн\w*\s+файл|файл\w*|файлов\w*")
_SCOPE_DIRECTORY = re.compile(
    r"\bald\b|ald\s*pro|служб\w*\s+каталог\w*|объект\w*\s+каталог\w*|"
    r"active\s+directory|домен\w*\s+каталог")

# Сервер уже куплен: покупать железо не нужно.
_SERVER_OWNED = re.compile(
    r"(?:сервер|серверн\w*)[^.!?]{0,30}(?:уже\s+есть|есть|имеется|куплен)|"
    r"(?:уже\s+есть|есть|имеется)\s+(?:\w+\s+){0,2}сервер")


# ── извлечение обязательных условий из запроса ────────────────────

def extract_requirements(query: str, conn=None) -> dict:
    """Что пользователь потребовал явно: ОС, версия, архитектура, тип лицензии.

    Отрицание («без windows», «не windows») снимает условие и превращается
    в исключение: такие платформы не должны попадать в проверенные
    допродажи.

    Если передан `conn`, условия дополняются классом продукта
    (`product_scope`): «Касперски для линукс» — это антивирус с условием
    Linux, а не запрос про операционную систему. `wants_os` в этом случае
    пересчитывается по фактическому классу продукта.
    """
    text = normalize_text(query)
    exclude = set(_NEGATION_RE.findall(text))
    # Текст без отрицаемых фрагментов: «не windows» не делает запрос
    # запросом про ОС.
    positive = _NEGATION_RE.sub(" ", text)

    reqs: dict = {
        "os": None, "os_rule": None, "version": [], "arch": None,
        "license_type": None, "exclude": sorted(exclude),
        "wants_os": bool(_OS_QUERY_RE.search(positive)),
    }

    # Конкретная ОС по правилу (самый длинный алиас выигрывает).
    best = None
    for rule in COMPAT_RULES:
        if rule["kind"] != "os":
            continue
        for alias in rule["aliases"]:
            if alias in exclude:
                continue
            if alias in positive and (best is None or len(alias) > best[0]):
                best = (len(alias), rule)
    if best:
        reqs["os"] = best[1]["product"]
        reqs["os_rule"] = best[1]["id"]

    reqs["version"] = re.findall(r"\d+\.\d+(?:\.\d+)?", positive)
    # Год в запросе — тоже версия: «photoshop 2025» не должен выдавать
    # Photoshop CC за точное совпадение.
    for year in re.findall(r"(?<![0-9])(?:19|20)\d{2}(?![0-9])", positive):
        if year not in reqs["version"]:
            reqs["version"].append(year)

    for alias, canon in _ARCH_ALIASES.items():
        if re.search(r"(?<![0-9a-zа-яё])" + re.escape(alias) + r"(?![0-9a-zа-яё])",
                     positive):
            reqs["arch"] = canon
            break

    for name, rx in _LICENSE_INTENT:
        if rx.search(positive):
            reqs["license_type"] = name
            break

    # Назначение: сервер или рабочая станция. Нужно, чтобы артикул
    # «для рабочей станции Windows» не считался подходящим серверу.
    reqs["role"] = None
    if _SERVER_WORDS.search(positive):
        reqs["role"] = "server"
    elif _WORKSTATION_WORDS.search(positive):
        reqs["role"] = "workstation"

    # Что восстанавливать: влияет на подбор средства резервного копирования.
    reqs["restore_scope"] = None
    if _SCOPE_FULL_SERVER.search(positive):
        reqs["restore_scope"] = "full_server"
    elif _SCOPE_DIRECTORY.search(positive):
        reqs["restore_scope"] = "directory"
    elif _SCOPE_FILES.search(positive):
        reqs["restore_scope"] = "files"

    # Версия ALD Pro: нужна для точного выбора средства восстановления
    # объектов каталога. Если её нет в запросе, это надо уточнить.
    reqs["ald_version"] = None
    ald = re.search(r"ald\s*pro(?:\s*(?:версии|версия|v))?\s*(\d+(?:\.\d+)*)",
                    positive)
    if ald:
        reqs["ald_version"] = ald.group(1)

    # Сервер уже куплен — железо не предлагаем.
    reqs["server_owned"] = bool(_SERVER_OWNED.search(positive))

    # Платформенное семейство из запроса: «для Windows», «для Linux»,
    # «под Astra Linux» и т.п. — извлекается независимо от COMPAT_RULES.
    # Когда покупатель называет ОС как условие совместимости (`os_is_constraint`),
    # это семейство становится требованием к платформе SKU.
    _QUERY_PLATFORMS = {
        "windows": _WINDOWS_WORDS,
        "linux": {"linux", "линукс", "астра", "astra", "редос", "альт",
                  "centos", "debian", "ubuntu", "rhel", "suse", "рос", "unix"},
    }
    reqs["platform_family"] = None
    for family, words in _QUERY_PLATFORMS.items():
        if any(w in positive for w in words):
            reqs["platform_family"] = family
            break

    # Требование к платформе ставит `product_scope`: нужен индекс, чтобы
    # отличить «ОС — сам продукт» от «ОС — условие совместимости».
    if conn is None:
        reqs["platform_family"] = None

    # Класс продукта и роль ОС: обобщённо, по таксономии каталога.
    # Только при наличии соединения — оно нужно, чтобы сопоставить бренд
    # с категориями его товаров.
    if conn is not None:
        reqs = product_scope.refine_requirements(conn, reqs, query)

    return reqs


def _params(offer: dict) -> dict:
    raw = offer.get("params")
    if isinstance(raw, dict):
        return raw
    raw = offer.get("params_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _param_values(offer: dict, key: str) -> list[str]:
    values = _params(offer).get(key) or []
    if isinstance(values, str):
        values = [values]
    return [str(v) for v in values
            if normalize_text(str(v)) not in _PARAM_EMPTY]


def _haystack(offer: dict) -> str:
    """Однозначные поля фида, где может быть подтверждение характеристики."""
    parts = [offer.get("name") or "", offer.get("platform") or "",
             offer.get("category_path") or ""]
    for key in ("Совместимая архитектура/платформа", "Версия", "Тип лицензии",
                "Назначение"):
        parts.extend(_param_values(offer, key))
    return normalize_text(" ".join(parts))


def _alias_hit(offer: dict, aliases) -> str | None:
    hay = _haystack(offer)
    for alias in aliases:
        if alias in hay:
            return alias
    return None


def is_os_offer(offer: dict) -> bool:
    """SKU — операционная система.

    Категория каталога надёжнее названия: антивирус «F-Secure … Linux»
    или САПР «… для Linux» — не ОС, хотя слово Linux в названии есть.
    """
    category = normalize_text(offer.get("category_path") or "")
    if "операционные системы" in category:
        return True
    name = normalize_text(offer.get("name") or "")
    return bool(re.search(r"операционная систем", name))


def platform_conflict(offer: dict, reqs: dict) -> str:
    """Явный конфликт платформы артикула с требованием покупателя.

    Требование берётся из опознанной ОС или из платформенного семейства
    запроса (`platform_family`) — см. `sku_rules.platform_conflict`.
    """
    rule = _RULES_BY_ID.get((reqs or {}).get("os_rule") or "")
    return sku_rules.platform_conflict(offer, {**(reqs or {}),
                                               "os_rule_obj": rule})


def sku_conflict(offer: dict, reqs: dict) -> str:
    """Причина, по которой конкретный артикул не подходит. Пусто — годится."""
    rule = _RULES_BY_ID.get((reqs or {}).get("os_rule") or "")
    return sku_rules.sku_conflict(offer, {**(reqs or {}), "os_rule_obj": rule})


# ── проверка обязательных характеристик SKU ───────────────────────

def verify_requirements(offer: dict, reqs: dict) -> dict:
    """Проверяет обязательные характеристики конкретного SKU.

    Возвращает {"verified": bool, "facts": [...], "problems": [...],
    "conflict": bool, "rule": rule|None}. `conflict` — явное противоречие
    (например, SKU только для Windows при обязательной Astra Linux):
    такой товар нельзя показывать даже как аналог. Пустые обязательные
    условия — проверять нечего, такой товар годится как аналог
    (не как «проверенная допродажа»).
    """
    facts: list[str] = []
    problems: list[str] = []
    rule = _RULES_BY_ID.get(reqs.get("os_rule") or "") if reqs else None

    # 0. Исключения из запроса: «без windows», «кроме windows».
    for word in (reqs or {}).get("exclude") or ():
        if word in _WINDOWS_WORDS:
            platform = normalize_text(offer.get("platform") or "")
            other = any(x in platform for x in
                        ("linux", "астра", "astralinux", "redos", "альт", "рос",
                         "unix", "macos"))
            if "windows" in platform and not other:
                problems.append("платформа только Windows, а Windows исключён "
                                "запросом")
                return {"verified": False, "facts": facts,
                        "problems": problems, "conflict": True, "rule": rule}

    # 0а. Функциональный класс: операционная система не может быть
    # аналогом антивируса, средства резервного копирования и любого
    # другого не-ОС класса — даже при совпадении слова, вендора, платформы
    # или типа лицензии. Правило общее, из таксономии `product_scope`.
    scope_reason = product_scope.functional_conflict(offer, reqs or {})
    if scope_reason:
        problems.append(scope_reason)
        return {"verified": False, "facts": facts, "problems": problems,
                "conflict": True, "rule": rule}

    # 0б. Ограничения конкретного артикула: ОС в названии, назначение
    # (сервер / рабочая станция), объект восстановления. Проверяются до
    # группировки в семейства: «Кибер Бэкап …, для рабочей станции Windows»
    # не годится серверу, даже если поле платформы — Linux/Windows.
    sku_reason = sku_conflict(offer, reqs or {})
    if sku_reason:
        problems.append(sku_reason)
        return {"verified": False, "facts": facts, "problems": problems,
                "conflict": True, "rule": rule}

    # 0в. Восстановление объектов каталога: назначение подтверждено
    # названием артикула (иначе sku_conflict уже отбраковал бы), но версия
    # ALD Pro для точной совместимости обычно не указана — это уточняется.
    if (reqs or {}).get("restore_scope") == "directory":
        facts.append("в названии артикула заявлено резервное копирование и "
                     "гранулярное восстановление объектов каталога")
        if not (reqs or {}).get("ald_version"):
            problems.append("версия ALD Pro в запросе не указана — уточните "
                            "совместимость с вашей версией каталога")

    # 1. Платформа как условие совместимости («антивирус для Windows»).
    # Конфликт с другой платформой отбракован выше; здесь проверяется
    # подтверждение: платформа должна быть названа в данных SKU.
    family = (reqs or {}).get("platform_family")
    if family:
        stated = (sku_rules.name_platforms(offer)
                  or sku_rules.field_platforms(offer))
        if family in stated:
            facts.append(f"платформа {family} указана в данных SKU")
        else:
            problems.append(
                f"поддержка платформы {family} в данных SKU не указана")
            return {"verified": False, "facts": facts, "problems": problems,
                    "conflict": False, "rule": rule}

    # 2. ОС из запроса.
    if rule:
        alias = _alias_hit(offer, rule.get("evidence") or rule["aliases"])
        if not alias:
            # Подтверждения в данных SKU нет. Если артикул явно ограничен
            # другой платформой — это конфликт (проверен выше), иначе
            # характеристика просто неизвестна.
            problems.append(
                f"совместимость с «{rule['product']}» не подтверждена")
            return {"verified": False, "facts": facts, "problems": problems,
                    "conflict": False, "rule": rule}
        facts.append(f"ОС «{rule['product']}» указана в данных SKU")

        # 2. Версия ОС (если запрошена явно).
        if reqs.get("version"):
            hay = _haystack(offer)
            codenames = rule.get("codenames") or {}
            version_ok = False
            for version in reqs["version"]:
                if version in hay:
                    facts.append(f"версия {version} указана в данных SKU")
                    version_ok = True
                    break
                for code in codenames.get(version, ()):
                    if code in hay:
                        facts.append(
                            f"версия {version} подтверждена кодовым именем «{code}»")
                        version_ok = True
                        break
                if version_ok:
                    break
            if not version_ok:
                problems.append(
                    f"версия {', '.join(reqs['version'])} не подтверждена")
                return {"verified": False, "facts": facts,
                        "problems": problems, "conflict": False, "rule": rule}

    # 2б. Версия вне правила ОС: «photoshop 2025» без 2025 в SKU —
    # не точное совпадение.
    if reqs and reqs.get("version") and not rule:
        hay = _haystack(offer)
        hit = next((v for v in reqs["version"] if v in hay), None)
        if hit:
            facts.append(f"версия {hit} указана в данных SKU")
        else:
            problems.append(
                f"версия {', '.join(reqs['version'])} не подтверждена")
            return {"verified": False, "facts": facts, "problems": problems,
                    "conflict": False, "rule": rule}

    # 3. Архитектура.
    if reqs and reqs.get("arch"):
        arch = reqs["arch"]
        hay = _haystack(offer)
        arch_values = " ".join(
            _param_values(offer, "Совместимая архитектура/платформа")).lower()
        if arch in hay or arch in arch_values:
            facts.append(f"архитектура {arch} указана в данных SKU")
        else:
            problems.append(f"архитектура {arch} не подтверждена")
            return {"verified": False, "facts": facts, "problems": problems,
                    "conflict": False, "rule": rule}

    # 4. Тип лицензии.
    if reqs and reqs.get("license_type"):
        want = reqs["license_type"]
        have = offer.get("offer_type") or "unknown"
        ok = have == want or (want == "subscription" and have == "base")
        if ok:
            facts.append(
                f"тип лицензии «{OFFER_TYPE_LABELS.get(have, have)}» соответствует запросу")
        else:
            problems.append(
                f"тип лицензии «{OFFER_TYPE_LABELS.get(have, have)}» "
                f"не соответствует запросу «{OFFER_TYPE_LABELS.get(want, want)}»")
            return {"verified": False, "facts": facts, "problems": problems,
                    "conflict": False, "rule": rule}

    # 5. Намерение «купить ОС»: кандидат должен быть ОС, но сопровождение,
    # продление и дополнение — сопутствующие услуги к ОС, они допустимы
    # (например, «российская ОС … с возможностью технической поддержки»).
    if reqs and reqs.get("wants_os") and not rule and not is_os_offer(offer):
        otype = offer.get("offer_type") or "unknown"
        if otype not in ("support", "addon", "renewal"):
            problems.append("запрос про операционную систему, а SKU — не ОС")
            return {"verified": False, "facts": facts, "problems": problems,
                    "conflict": False, "rule": rule}

    return {"verified": True, "facts": facts, "problems": problems,
            "conflict": False, "rule": rule}


def _requirement_summary(reqs: dict) -> str:
    parts = []
    if reqs.get("os"):
        parts.append(reqs["os"])
    if reqs.get("version"):
        parts.append("версия " + ", ".join(reqs["version"]))
    if reqs.get("arch"):
        parts.append("арх. " + reqs["arch"])
    if reqs.get("platform_family"):
        parts.append("платформа " + reqs["platform_family"])
    if reqs.get("license_type"):
        parts.append(OFFER_TYPE_LABELS.get(reqs["license_type"],
                                          reqs["license_type"]))
    if reqs.get("needs_support"):
        parts.append("нужна техническая поддержка")
    if reqs.get("product_classes"):
        parts.append("класс: " + ", ".join(reqs["product_classes"]))
    if reqs.get("role") == "server":
        parts.append("серверная лицензия")
    elif reqs.get("role") == "workstation":
        parts.append("рабочая станция")
    scope = reqs.get("restore_scope")
    if scope:
        parts.append({"full_server": "восстановление всего сервера",
                      "files": "восстановление отдельных файлов",
                      "directory": "восстановление объектов каталога"}
                     .get(scope, scope))
    if reqs.get("server_owned"):
        parts.append("сервер уже есть — железо не требуется")
    return "; ".join(parts)


# ── семейства и предпочтение доступных SKU с ценой ────────────────

def family_id(offer: dict) -> str:
    """Ключ семейства по данным каталога: вендор + имя без редакции.

    Редакции одного продукта (group_id в фиде у них разные) — одно
    семейство, поэтому в топ-4 попадает максимум одна позиция семейства.
    У серверов group_id нет, а название содержит конфигурацию:
    «Сервер INFERIT RS108 R1G2D24/…». Конфигурации одной модели — это
    одно семейство, поэтому для оборудования берём вендора и модель.
    """
    name = normalize_text(offer.get("name") or "")
    vendor = normalize_text(offer.get("vendor") or "")
    if not offer.get("group_id") and _SERVER_NAME_RE.match(name):
        tokens = [t for t in re.split(r"[\s/,]+", name) if t][:3]
        return "h:" + vendor + "|" + " ".join(tokens)
    return "f:" + "|".join(osr.product_family(offer))


def _pref_tier(group: dict) -> int:
    """0 — доступно и с ценой; 1 — что-то одно; 2 — резерв."""
    offer = group["best"]
    available = bool(offer.get("available"))
    has_price = (offer.get("price") or 0) > 0
    if available and has_price:
        return 0
    if available or has_price:
        return 1
    return 2


def _relevance_rank(group: dict) -> int:
    coverage = group.get("coverage") or 0
    if coverage >= 0.99:
        return 0
    if coverage >= 0.5:
        return 1
    return 2


def _sort_key(group: dict, reqs: dict | None = None) -> tuple:
    price = group["best"].get("price") or 0
    # «Найди Linux»: при намерении купить ОС сами ОС идут раньше
    # утилит и антивирусов «для Linux».
    os_rank = 0
    if reqs and reqs.get("wants_os") and not reqs.get("os"):
        os_rank = 0 if is_os_offer(group["best"]) else 1
    return (os_rank, _relevance_rank(group), _pref_tier(group),
            -group.get("text_score", 0), price if price else 1e18)


# ── сборка топ-4 ──────────────────────────────────────────────────

def _entry(group: dict, role: str, reason: str, verification: dict | None) -> dict:
    offer = group["best"]
    entry = {
        "id": offer["id"],
        "group_id": offer.get("group_id"),
        "family_id": family_id(offer),
        "name": group["name"],
        "full_name": offer.get("name"),
        "vendor": offer.get("vendor"),
        "url": offer.get("url"),
        "category_path": offer.get("category_path"),
        "price": offer.get("price"),
        "price_text": (f"{offer['price']:,.0f} ₽".replace(",", " ")
                       if offer.get("price") else "Цена не указана"),
        "available": bool(offer.get("available")),
        "available_text": ("в наличии" if offer.get("available")
                           else "нет в наличии"),
        "offer_type": offer.get("offer_type") or "unknown",
        "type_label": OFFER_TYPE_LABELS.get(offer.get("offer_type") or "unknown",
                                            "тип не указан"),
        "platform": offer.get("platform") or "",
        "duration": offer.get("duration") or "",
        "bucket": group.get("bucket"),
        "role": role,
        # Позиция без цены и недоступная — резерв: её нельзя предлагать
        # как покупку, но она показывает, что есть в каталоге.
        "reserve": _pref_tier(group) == 2,
        "reason": reason,
        "facts": (verification or {}).get("facts") or [],
        "problems": (verification or {}).get("problems") or [],
    }
    # Ограничение лицензии из названия артикула: «для образовательных
    # организаций» — применимость зависит от типа покупателя.
    full_name = normalize_text(offer.get("name") or "")
    if "образовательн" in full_name:
        entry["limitation"] = ("лицензия для образовательных организаций — "
                               "применимость требует уточнения типа покупателя")
    rule = (verification or {}).get("rule")
    if rule:
        entry["rule"] = {"id": rule["id"], "scope": rule["scope"],
                         "doc": rule["doc"], "doc_title": rule["doc_title"]}
    return entry


_NON_PRODUCT_TOKEN_RE = re.compile(
    r"^(?:x|x86|x64|86|64|32|arm|arm64|aarch64|amd64|e2k|эльбрус|"
    r"\d+(?:\.\d+)*)$")


def _core_tokens(result: dict) -> list[str]:
    """Токены запроса без архитектуры и версий: «astra linux 1.7 x86-64»
    → ['astra', 'linux']."""
    out = []
    for token in result.get("tokens") or []:
        if _NON_PRODUCT_TOKEN_RE.match(token):
            continue
        out.append(token)
    return out


def _candidate_groups(result: dict) -> list[dict]:
    """Все кандидаты из секций: топ-4 не должен зависеть от усечения groups.

    `result["groups"]` обрезан до верхних N по релевантности, из-за чего
    товары для ОС и альтернативы могли в него не попасть. Секции `buckets`
    хранят до N карточек каждого вида — этого достаточно для топ-4.
    """
    seen: set[int] = set()
    out: list[dict] = []
    for bucket_groups in (result.get("buckets") or {}).values():
        for group in bucket_groups:
            if id(group) in seen:
                continue
            seen.add(id(group))
            out.append(group)
    if not out:
        out = list(result.get("groups") or [])
    return out


def _verified_candidates(groups: list[dict], reqs: dict) -> list[dict]:
    """Оставляет SKU, у которых подтверждены все обязательные условия."""
    out = []
    for group in groups:
        check = verify_requirements(group["best"], reqs)
        if check["verified"]:
            out.append((group, check))
    return out


def _short_os(product: str) -> str:
    """Короткое имя ОС для формулировок: «… Special Edition» → «…»."""
    return re.sub(r"\s+(?:special\s+edition|se)$", "", product or "",
                  flags=re.I).strip()


def compat_requirement_label(reqs: dict) -> str:
    """«Astra Linux 1.7» из обязательных условий (ОС + версия)."""
    if not reqs or not reqs.get("os"):
        return ""
    label = _short_os(reqs["os"])
    versions = [str(v) for v in (reqs.get("version") or [])]
    return (label + (" " + " ".join(versions) if versions else "")).strip()


NO_CONFIRMED_TEMPLATE = ("В каталоге не найдено предложений с подтверждённой "
                         "совместимостью с {label}")
NEEDS_CHECK_NOTE = "Совместимость требует проверки"


def _field_platform_conflict(offer: dict, want_family: str | None) -> bool:
    """Поле `platform` артикула явно ограничено другой платформой.

    Для списка «требует проверки» этого достаточно, чтобы не показывать
    заведомо несовместимый SKU: если общее поле платформы называет только
    другую ОС, кандидат не годится. Смешанная или пустая платформа
    конфликтом не считается.
    """
    if not want_family:
        return False
    stated = sku_rules.field_platforms(offer)
    return bool(stated) and want_family not in stated


def build_top4(result: dict, reqs: dict | None = None) -> dict:
    """Один явный топ-4 из четырёх разных семейств с ролями и причинами.

    Если задана обязательная ОС, считаются только SKU с подтверждённой
    совместимостью. Явные конфликты (например, SKU только для Windows при
    обязательной Astra Linux) не показываются вовсе, а неподтверждённые
    аналоги уходят в отдельный список `needs_check` и не добивают топ-4.
    """
    groups = _candidate_groups(result)
    query = result.get("effective_query") or result.get("query") or ""
    if reqs is None:
        # Обязательные условия ищем по исправленному запросу: «astra linuc»
        # уже нормализован до «astra linux» в поле normalized.
        reqs = extract_requirements(result.get("normalized") or query)
    else:
        # Условия из поиска уже содержат класс продукта (`product_scope`).
        # Если вызывающий передал их без него, добираем из результата.
        reqs = dict(reqs)
        res_reqs = result.get("requirements") or {}
        for key in ("product_classes", "product_is_os", "os_is_constraint",
                    "scope_brand", "platform_family"):
            if key not in reqs and key in res_reqs:
                reqs[key] = res_reqs[key]

    wanted = product_scope.wanted_classes(reqs)

    def class_ok(group: dict, strict: bool = False) -> bool:
        """Артикул другого функционального класса не считается результатом.

        Функциональная релевантность — это класс продукта, а не отдельное
        совпавшее слово. ОС не заменяет антивирус, средство резервного
        копирования — антивирус и т.п. `class_relation` учитывает неточную
        категорию: название артикула важнее категории (Granulex Recovery в
        категории ОС — это средство восстановления).

        `strict` — для ролей «прямое совпадение» и «функциональный аналог»:
        там нужна подтверждённая принадлежность классу (`same`). Позиции с
        нераспознанным классом (`unknown`) не получают эти роли, но могут
        остаться сопутствующими.
        """
        if not wanted:
            return True
        relation = product_scope.class_relation(group["best"], reqs)
        return relation == "same" if strict else relation != "other"

    # Обязательная ОС как условие совместимости («резервное копирование на
    # Astra Linux») не превращает запрос в запрос про операционную систему:
    # требовать от кандидата быть ОС нельзя, иначе подборка пуста.
    strict_os = bool(reqs.get("os")) and not reqs.get("os_is_constraint")
    direct = [g for g in groups if g["bucket"] == "direct"]
    for_os = [g for g in groups if g["bucket"] == "for_os"]
    related = [g for g in groups if g["bucket"] == "related"]
    alternative = [g for g in groups if g["bucket"] == "alternative"]

    picked: list[dict] = []
    needs_check: list[dict] = []
    seen_family: set[str] = set()
    seen_group: set[str] = set()
    used_vendors: set[str] = set()

    # Категория-якорь: та же категория каталога, что у лучшего совпадения.
    # Аналоги «требует проверки» берём только из неё — иначе в список
    # попадают чужие продукты, совпавшие одним словом запроса.
    anchor = (direct[0]["best"] if direct
              else (groups[0]["best"] if groups else None))
    anchor_category = str((anchor or {}).get("category_id") or "")
    anchor_path = normalize_text((anchor or {}).get("category_path") or "")

    def same_category(group: dict) -> bool:
        offer = group["best"]
        category = str(offer.get("category_id") or "")
        if anchor_category and category:
            return category == anchor_category
        path = normalize_text(offer.get("category_path") or "")
        return bool(anchor_path) and path == anchor_path

    def try_add(group: dict, role: str, reason: str,
                verification: dict | None) -> bool:
        offer = group["best"]
        fam = family_id(offer)
        gid = offer.get("group_id")
        if fam in seen_family:
            return False
        if gid and gid in seen_group:
            return False
        seen_family.add(fam)
        if gid:
            seen_group.add(gid)
        used_vendors.add(normalize_text(offer.get("vendor") or ""))
        picked.append(_entry(group, role, reason, verification))
        return True

    def fill(pool: list[tuple[dict, dict | None]], role,
             reason_fn, need: int) -> int:
        # Сначала доступные и с ценой, потом резерв — в пределах
        # сопоставимой релевантности. При прочих равных предпочитаем
        # нового вендора: одного вендора недостаточно для топ-4.
        # Порядок пересчитывается после каждого выбора, иначе все позиции
        # одного вендора получают одинаковый приоритет заранее.
        def order(item):
            vendor = normalize_text(item[0]["best"].get("vendor") or "")
            repeat = 1 if vendor in used_vendors else 0
            return (repeat,) + _sort_key(item[0], reqs)

        remaining = list(pool)
        added = 0
        while added < need and remaining:
            remaining.sort(key=order)
            group, verification = remaining.pop(0)
            item_role = role(group, verification) if callable(role) else role
            if try_add(group, item_role, reason_fn(group, verification),
                       verification):
                added += 1
        return added

    def collect_needs_check(candidates: list[tuple[dict, dict]]) -> None:
        """Неподтверждённые, но без явного конфликта — отдельным списком.

        Только товары категории-якоря: это и есть поиск по категории среди
        разных производителей. Одна позиция на семейство, до четырёх.
        Внутри семейства представлять будет позиция, подходящая для новой
        покупки (базовая лицензия или подписка), а не сопровождение.

        Товары с явным конфликтом платформы по полю `platform` не включаются
        — они заведомо несовместимы, даже как кандидаты на проверку.
        """
        # Требуемое семейство из правила ОС или из запроса.
        rule_obj = _RULES_BY_ID.get((reqs or {}).get("os_rule") or "")
        want_family = ((rule_obj or {}).get("platform_family")
                       or (reqs or {}).get("platform_family"))

        pool = [(g, v) for g, v in candidates
                if same_category(g)
                and not _field_platform_conflict(g["best"], want_family)]

        def purchase_rank(group: dict) -> int:
            otype = group["best"].get("offer_type") or "unknown"
            return 0 if otype in ("base", "subscription") else 1

        pool.sort(key=lambda item: (purchase_rank(item[0]),
                                    _pref_tier(item[0]),
                                    -item[0].get("text_score", 0),
                                    item[0]["best"].get("price") or 1e18))
        # Разные производители в приоритете: один вендор не показывает
        # выбор на рынке.
        best_by_family: dict[str, tuple[dict, dict]] = {}
        ordered: list[tuple[dict, dict]] = []
        seen_vendors: set[str] = set()
        taken = {item.get("_family") for item in needs_check}
        for repeat in (0, 1):
            for group, verification in pool:
                vendor = normalize_text(group["best"].get("vendor") or "")
                if (vendor in seen_vendors) != bool(repeat):
                    continue
                seen_vendors.add(vendor)
                fam = family_id(group["best"])
                if fam in taken:
                    continue
                if fam not in best_by_family:
                    best_by_family[fam] = (group, verification)
                    ordered.append((group, verification))
        for group, verification in ordered:
            if len(needs_check) >= TOP4_SIZE:
                break
            entry = _entry(group, ROLE_ANALOG,
                           "; ".join(verification.get("problems") or [])
                           or "совместимость не подтверждена", verification)
            entry["needs_check"] = True
            entry["verification_note"] = NEEDS_CHECK_NOTE
            entry["_family"] = family_id(group["best"])
            needs_check.append(entry)

    # 1. Прямые совпадения — только подтверждённого запрошенного класса.
    direct_pool = [(g, verify_requirements(g["best"], reqs)) for g in direct
                   if class_ok(g, strict=True)]
    has_hard_condition = bool(reqs.get("os") or reqs.get("version")
                              or reqs.get("arch") or reqs.get("license_type")
                              or reqs.get("platform_family")
                              or (reqs.get("wants_os") and not reqs.get("os")))
    if strict_os:
        # Только подтверждённые: неподтверждённые не считаются результатом.
        direct_ok = [(g, v) for g, v in direct_pool if v["verified"]]
        direct_rejected = [(g, v) for g, v in direct_pool
                           if not v["verified"] and not v.get("conflict")]
        collect_needs_check(direct_rejected)
    else:
        direct_ok = [(g, v) for g, v in direct_pool
                     if not v.get("conflict")
                     and (v["verified"] or not has_hard_condition)]
        direct_rejected = [(g, v) for g, v in direct_pool
                           if id(g) not in {id(x) for x, _ in direct_ok}
                           and not v.get("conflict")]
    fill(direct_ok, ROLE_DIRECT, lambda g, v: g.get("reason") or "", TOP4_SIZE)

    # 2. Проверенные сопутствующие товары (товары для ОС и related).
    if len(picked) < TOP4_SIZE:
        related_pool = _verified_candidates(
            [g for g in for_os + related if class_ok(g)], reqs)
        fill(related_pool, ROLE_RELATED,
             lambda g, v: "; ".join(v["facts"]) or g.get("reason") or "",
             TOP4_SIZE - len(picked))

    # 3. Функциональные аналоги (другая линейка той же категории).
    # Если альтернатива на деле совпадает по названию и всем условиям
    # (токенизатор разбил «x86-64» на части), не называем её аналогом.
    if len(picked) < TOP4_SIZE:
        core = _core_tokens(result)

        def analog_role(group, _verification):
            # Восстановление объектов каталога: всё, что прошло отбраковку
            # по названию артикула, — это и есть средство под задачу, а не
            # «аналог» постороннего товара.
            if (reqs.get("restore_scope") == "directory"
                    and not sku_conflict(group["best"], reqs)):
                return ROLE_DIRECT
            hay = _haystack(group["best"])
            if core and all(t in hay for t in core):
                return ROLE_DIRECT
            return ROLE_ANALOG

        def analog_reason(group, verification):
            if analog_role(group, verification) == ROLE_DIRECT:
                return "; ".join(verification["facts"]) or "совпадает по названию"
            return ("точного товара нет — аналог: "
                    + (group.get("reason") or "другая линейка"))

        analog_pool = _verified_candidates(
            [g for g in alternative if class_ok(g, strict=True)], reqs)
        fill(analog_pool, analog_role, analog_reason,
             TOP4_SIZE - len(picked))

    # 3б. При обязательной ОС (или при os_is_constraint + жёсткое условие)
    # неподтверждённые аналоги — только в отдельный список, без добора топ-4.
    strict_compat = strict_os or (reqs.get("os_is_constraint")
                                  and has_hard_condition)
    if strict_compat:
        extra: list[tuple[dict, dict]] = []
        for group in for_os + related + alternative:
            if not class_ok(group):
                continue
            if any(id(group) == id(g) for g, _ in direct_rejected):
                continue
            check = verify_requirements(group["best"], reqs)
            if not check["verified"] and not check.get("conflict"):
                extra.append((group, check))
        collect_needs_check(extra)
        # Неподтверждённые прямые совпадения тоже в needs_check.
        if not strict_os:
            collect_needs_check(direct_rejected)

    # 4. Если прямые не прошли проверку — вернуть их как резерв в конце.
    # При обязательной ОС или при условии совместимости с жёстким требованием
    # (версия/архитектура/платформа) этого не делаем: неподтверждёнными
    # не добиваем.
    if (len(picked) < TOP4_SIZE and direct_rejected
            and not strict_os
            and not (reqs.get("os_is_constraint") and has_hard_condition)):
        fill(direct_rejected, ROLE_ANALOG,
             lambda g, v: ("не подтверждено: " + "; ".join(v["problems"])
                           if v["problems"] else "резерв"),
             TOP4_SIZE - len(picked))

    # 5. Совсем нет проверенных — показать ближайший аналог с пометкой,
    # чтобы пользователь не получил пустой топ-4. При обязательной ОС
    # аналог без подтверждения не считается результатом.
    if (not picked and alternative and not strict_os
            and not (reqs.get("os_is_constraint") and has_hard_condition)):
        loose = [(g, verify_requirements(g["best"], reqs)) for g in alternative
                 if class_ok(g)
                 and not verify_requirements(g["best"], reqs).get("conflict")]
        fill(loose, ROLE_ANALOG,
             lambda g, v: ("не подтверждено: " + "; ".join(v["problems"])
                           if v["problems"] else "резерв"),
             TOP4_SIZE)

    reserve_note = ""
    if picked and all(it.get("reserve") for it in picked):
        reserve_note = ("все найденные позиции без цены и/или недоступны — "
                        "показаны как резерв; покупка недоступна")

    label = compat_requirement_label(reqs)
    no_confirmed = ""
    if strict_os and not picked:
        no_confirmed = NO_CONFIRMED_TEMPLATE.format(label=label or reqs["os"])
    elif (reqs.get("os_is_constraint") and has_hard_condition and not picked
          and reqs.get("os")):
        no_confirmed = NO_CONFIRMED_TEMPLATE.format(label=label or reqs["os"])

    shortfall = len(picked) < TOP4_SIZE
    reason = ""
    if shortfall:
        reason = (f"подходящих позиций меньше {TOP4_SIZE}: найдено "
                  f"{len(picked)}")
        if reqs and (reqs.get("os") or reqs.get("version") or reqs.get("arch")):
            reason += f" (обязательные условия: {_requirement_summary(reqs)})"
        if not groups:
            reason = "в каталоге нет подходящих позиций"

    for item in needs_check:
        item.pop("_family", None)

    # Явно неподходящие товары с причинами — для вопроса «что не подойдёт».
    rejected: list[dict] = []
    seen_rejected: set[str] = set()
    for group, verification in direct_pool:
        if verification.get("verified"):
            continue
        fam = family_id(group["best"])
        if fam in seen_rejected:
            continue
        seen_rejected.add(fam)
        entry = _entry(group, ROLE_ANALOG,
                       "; ".join(verification.get("problems") or [])
                       or "не подходит", verification)
        entry["rejected"] = True
        rejected.append(entry)
        if len(rejected) >= 6:
            break

    return {
        "items": picked,
        "count": len(picked),
        "verified_count": len(picked),
        "size": TOP4_SIZE,
        "strict_os": strict_os,
        "needs_check": needs_check,
        "needs_check_note": NEEDS_CHECK_NOTE if needs_check else "",
        "rejected": rejected,
        "no_confirmed_message": no_confirmed,
        "shortfall": shortfall,
        "shortfall_reason": no_confirmed or reason,
        "all_reserve": bool(picked) and all(it.get("reserve") for it in picked),
        "reserve_note": reserve_note,
        "requirements": {
            "os": reqs.get("os"),
            "version": reqs.get("version") or [],
            "arch": reqs.get("arch"),
            "license_type": reqs.get("license_type"),
            "platform_family": reqs.get("platform_family"),
            "wants_os": reqs.get("wants_os", False),
            "product_classes": reqs.get("product_classes") or [],
            "product_is_os": reqs.get("product_is_os", False),
            "os_is_constraint": reqs.get("os_is_constraint", False),
            "needs_support": reqs.get("needs_support", False),
            "summary": _requirement_summary(reqs),
        },
        "candidate_pools": {
            "direct": len(direct),
            "for_os": len(for_os),
            "related": len(related),
            "alternative": len(alternative),
        },
    }

