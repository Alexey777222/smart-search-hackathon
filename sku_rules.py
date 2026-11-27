"""sku_rules.py — ограничения конкретного артикула (SKU) каталога.

Правила проверяют не «семейство» и не общее поле платформы, а сам артикул:
что именно в нём написано про ОС, назначение (сервер / рабочая станция) и
тип лицензии. Нужно это потому, что общее поле `platform` бывает широким:
у SKU «Кибер Бэкап Стандартная редакция …, для рабочей станции Windows»
стоит `Linux/Windows`, хотя сам артикул — рабочая станция под Windows.

Модуль не зависит от selection.py и organizer_search.py, поэтому его
используют оба: поиск (отбор кандидатов до группировки) и консультант.
Никаких списков «плохих» артикулов: только признаки из данных каталога.
"""

from __future__ import annotations

from organizer_index import normalize_text

# ── назначение артикула ───────────────────────────────────────────
# Признаки берутся из названия SKU и параметра «Назначение». Порядок
# важен: «для рабочей станции Windows» не должно распознаться как сервер
# из-за слова «Windows» в конце.
ROLE_MARKERS: dict[str, tuple[str, ...]] = {
    "workstation": (
        "рабочей станции", "рабочая станция", "рабочих станций",
        "рабочее место", "рабочих мест", "workstation",
        "лицензия для пк", "1 пк", "3пк", "5пк",
    ),
    "server": (
        "физического сервера", "физический сервер", "физических серверов",
        "для сервера", "серверная лицензия", "серверные лицензии",
        "на сервер", "сервер с ",
    ),
    "virtual_machine": (
        "виртуальной машины", "виртуальных машин", "виртуальная машина",
    ),
    "virtualization": (
        "платформы виртуализации", "для платформы виртуализации",
    ),
    "db": ("субд", "баз данных"),
    "mailbox": ("почтового ящика", "почтовый ящик"),
    "directory_objects": (
        "объектов каталога", "гранулярн", "granular",
    ),
}

# Платформенное семейство ОС: нужно, чтобы отличить жёсткое ограничение
# артикула от широкого поля `platform`.
PLATFORM_ALIASES: dict[str, tuple[str, ...]] = {
    "linux": ("linux", "астра", "astralinux", "redos", "ред ос", "альт",
              "centos", "debian", "ubuntu", "rhel", "suse", "рос", "unix",
              "аврора"),
    "windows": ("windows", "виндовс"),
}


def _params(offer: dict) -> dict:
    raw = offer.get("params")
    if isinstance(raw, dict):
        return raw
    raw = offer.get("params_json")
    if not raw:
        return {}
    import json
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _param_text(offer: dict, key: str) -> str:
    values = _params(offer).get(key) or []
    if isinstance(values, str):
        values = [values]
    return normalize_text(" ".join(str(v) for v in values))


def roles_of(offer: dict) -> set[str]:
    """Назначение артикула по его названию и параметру «Назначение»."""
    hay = normalize_text(" ".join((offer.get("name") or "",
                                   _param_text(offer, "Назначение"))))
    found: set[str] = set()
    for role, markers in ROLE_MARKERS.items():
        if any(marker in hay for marker in markers):
            found.add(role)
    return found


def platforms_in(text: str) -> set[str]:
    """Платформенные семейства, упомянутые в тексте."""
    low = normalize_text(text)
    return {name for name, aliases in PLATFORM_ALIASES.items()
            if any(alias in low for alias in aliases)}


def name_platforms(offer: dict) -> set[str]:
    """Платформы, названные в самом артикуле (название + назначение)."""
    return platforms_in(" ".join((offer.get("name") or "",
                                  _param_text(offer, "Назначение"))))


def field_platforms(offer: dict) -> set[str]:
    """Платформы из общего поля `platform`."""
    return platforms_in(offer.get("platform") or "")


def platform_conflict(offer: dict, reqs: dict) -> str:
    """Артикул явно ограничен другой платформой, чем обязательная ОС.

    Сначала проверяется название: «…, для рабочей станции Windows» —
    однозначное ограничение артикула, даже если общее поле платформы
    широкое (`Linux/Windows`). Затем — само поле `platform`.
    """
    rule = (reqs or {}).get("os_rule_obj")
    if not rule or rule.get("kind") != "os":
        return ""
    want = rule.get("platform_family")
    if not want:
        return ""
    product = rule.get("product", "обязательная ОС")

    by_name = name_platforms(offer)
    if by_name:
        # Название артикула уже называет платформу — она важнее общего
        # поля `platform`, которое бывает широким или устаревшим.
        if by_name == {"windows"} and want == "linux":
            return (f"в названии артикула указана только Windows, а требуется "
                    f"«{product}»")
        if by_name == {"linux"} and want == "windows":
            return (f"в названии артикула указан только Linux, а требуется "
                    f"«{product}»")
        return ""

    by_field = field_platforms(offer)
    if by_field and by_field == {"windows"} and want == "linux":
        return (f"платформа артикула — только Windows, а требуется "
                f"«{product}»")
    return ""


# Признаки средства резервного копирования / восстановления и того, что
# оно заявлено для объектов каталога. Нужны, чтобы лицензию на сам каталог,
# просмотрщик и сертификат поддержки не выдать за средство восстановления.
_RESTORE_MARKERS = ("резервн", "восстановлен", "recovery", "backup")
_DIRECTORY_MARKERS = ("гранулярн", "granular", "объект", "каталог")
_NON_RESTORE_MARKERS = ("viewer", "просмотрщик", "сертификат")


def restore_scope_conflict(offer: dict, reqs: dict) -> str:
    """Артикул не является средством восстановления нужного объекта.

    Для задачи «восстановить отдельные объекты каталога ALD Pro» подходит
    только лицензия, в названии которой заявлено и резервное копирование,
    и гранулярное восстановление объектов каталога. Лицензия на сам каталог
    (в названии нет резервного копирования), просмотрщик и сертификат
    техподдержки средством восстановления не являются.
    """
    scope = (reqs or {}).get("restore_scope")
    if scope != "directory":
        return ""
    name = normalize_text(offer.get("name") or "")
    category = normalize_text(offer.get("category_path") or "")
    offer_type = offer.get("offer_type") or "unknown"

    if any(marker in name for marker in _NON_RESTORE_MARKERS):
        return ("просмотрщик или сертификат поддержки — не средство "
                "восстановления объектов каталога")
    if offer_type == "support":
        return "сертификат техподдержки — не средство восстановления"
    if not any(marker in (name + " " + category)
               for marker in _RESTORE_MARKERS):
        return "в артикуле не заявлено резервное копирование/восстановление"
    if not (any(marker in name for marker in _DIRECTORY_MARKERS)
            and ("гранулярн" in name or "granular" in name)):
        return ("в артикуле не заявлено гранулярное восстановление объектов "
                "каталога")
    return ""


def role_conflict(offer: dict, reqs: dict) -> str:
    """Артикул не того назначения, которое нужно покупателю."""
    roles = roles_of(offer)
    if not roles:
        return ""
    want = (reqs or {}).get("role")
    if want == "server" and roles == {"workstation"}:
        return ("артикул — для рабочей станции, а нужен серверный вариант")
    if want == "workstation" and roles == {"server"}:
        return "артикул — для сервера, а нужна рабочая станция"

    scope = (reqs or {}).get("restore_scope")
    if scope == "full_server":
        if roles == {"directory_objects"}:
            return ("артикул восстанавливает только объекты каталога, "
                    "а нужен образ всего сервера")
        if roles & {"virtual_machine", "db", "mailbox"} and "server" not in roles:
            narrow = {"virtual_machine": "одну виртуальную машину",
                      "db": "только СУБД", "mailbox": "только почтовые ящики"}
            what = next(narrow[r] for r in ("virtual_machine", "db", "mailbox")
                        if r in roles)
            return (f"артикул покрывает {what}, а нужен образ всего сервера")
    if scope == "files" and roles == {"directory_objects"}:
        return ("артикул восстанавливает объекты каталога, а нужны "
                "отдельные файлы")
    return ""


def sku_conflict(offer: dict, reqs: dict) -> str:
    """Первая причина, по которой артикул не подходит. Пусто — не противоречит."""
    return (platform_conflict(offer, reqs)
            or role_conflict(offer, reqs)
            or restore_scope_conflict(offer, reqs))


def unresolved(offer: dict, reqs: dict) -> list[str]:
    """Какие характеристики артикула нужно проверить вручную.

    Возвращает только то, чего в данных артикула действительно нет.
    """
    out: list[str] = []
    roles = roles_of(offer)
    want_role = (reqs or {}).get("role")
    if want_role and not roles:
        out.append("назначение артикула (сервер или рабочая станция)")
    if (reqs or {}).get("os"):
        versions = [str(v) for v in (reqs.get("version") or [])]
        hay = normalize_text(" ".join((offer.get("name") or "",
                                       offer.get("platform") or "",
                                       _param_text(offer,
                                                   "Совместимая архитектура/"
                                                   "платформа"),
                                       _param_text(offer, "Версия"))))
        if versions and not any(v in hay for v in versions):
            out.append("выпуск ОС " + ", ".join(versions))
    return out
