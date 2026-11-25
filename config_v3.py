"""Конфигурация для v3 — обработка смешанных запросов.

Порог RapidFuzz = 55 (не подгоняется).
Не изменяет основной config.py.
"""

# ── транслитерация кириллица → латиница ───────────────────────────
# Однобуквенная, для узнавания названий продуктов в кириллице.
TRANSLIT_MAP = {
    "а": "a",  "б": "b",  "в": "v",  "г": "g",  "д": "d",
    "е": "e",  "ё": "yo", "ж": "zh", "з": "z",  "и": "i",
    "й": "y",  "к": "k",  "л": "l",  "м": "m",  "н": "n",
    "о": "o",  "п": "p",  "р": "r",  "с": "s",  "т": "t",
    "у": "u",  "ф": "f",  "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sh", "ъ": "",   "ы": "y",  "ь": "",
    "э": "e",  "ю": "yu", "я": "ya",
}

def transliterate(text: str) -> str:
    """Кириллица → латиница посимвольно."""
    return "".join(TRANSLIT_MAP.get(c, c) for c in text.lower())


# ── словарь общих терминов (кириллица → английский) ────────────────
# Помогают различать товары внутри бренда.
COMMON_TERMS = {
    "бэкап": "backup",
    "бэкапы": "backup",
    "резервное копирование": "backup",
    "синхронизация": "sync",
    "синхр": "sync",
    "менеджер паролей": "password manager",
    "менеджер паро": "password manager",
    "хранитель паролей": "password manager",
    "текстовый редактор": "text editor",
    "текстовик": "text editor",
    "pdf генератор": "pdf generator",
    "генератор pdf": "pdf generator",
    "почтовый клиент": "mail client",
    "почта": "mail",
    "видеоплеер": "video player",
    "аудиоплеер": "audio player",
    "восстановление данных": "recovery",
    "антивирус": "antivirus",
    "фаервол": "firewall",
    "фаерволл": "firewall",
    "файрвол": "firewall",
    "редактор фото": "photo editor",
    "редактор видео": "video editor",
    "ide": "ide",
    "редактор": "editor",
    "плеер": "player",
    "плеера": "player",
    "клиент": "client",
    "генератор": "generator",
    "менеджер": "manager",
    "хранилище": "storage",
    "облако": "cloud",
    "облачный": "cloud",
    "облачное": "cloud",
    "аудио": "audio",
    "видео": "video",
    "студио": "studio",
    "студия": "studio",
    "портативный": "portable",
    "портативная": "portable",
    "про": "pro",
    "стандарт": "standard",
    "домашний": "home",
    "домашняя": "home",
    "бизнес": "business",
    "предприятие": "enterprise",
    "корпоративный": "enterprise",
    "студент": "student",
    "бесплатный": "free",
    "сервер": "server",
    "серверный": "server",
    "профессиональный": "professional",
    "профи": "professional",
    "рекавери": "recovery",
    "рескю": "rescue",
    "файл": "file",
    "файлы": "files",
    "данных": "data",
    "пароль": "password",
    "паролей": "password",
    "для": "",
    "с": "",
    "и": "",
    "на": "",
    "в": "",
    "от": "",
    "по": "",
    "не": "",
    "документ": "document",
    "документы": "documents",
    "текст": "text",
    "текста": "text",
    # Конструкции явного отказа от атрибута: пользователь просит «любую»
    # версию/редакцию → слова не должны зашумлять сравнение с названием.
    "без версии": "",
    "без редакции": "",
    "любой версии": "",
    "любая версия": "",
}


# ── алиасы брендов (кириллица → латиница) ─────────────────────────
# Уникальные названия продуктов для подбора кандидатов.
BRAND_ALIASES = {
    "taskflow": ["таскфлоу", "таск флоу", "таскфло"],
    "pixeldesk": ["пиксельдеск", "пиксель деск"],
    "cloudvault": ["клаудвольт", "клауд вольт", "клаудволт", "клаудвольт"],
    "netshield": ["нетшилд", "нет шилд", "нетшил"],
    "bytenote": ["байтнот", "байт нот"],
    "codeforge": ["кодофодж", "код фордж"],
    "mediaplay": ["медиаплей", "медиа плеи", "медиаплей"],
    "datarescue": ["дата рескю", "дата реску", "дата реске"],
    "securepass": ["сюарепасс", "сьюарепасс", "сикурepass", "сикурepass"],
    "cloudmail": ["клаудмейл", "клауд мейл", "клаудмэйл"],
    "reportgen": ["рипортджен", "рипорт ген"],
}

# Обратный словарь: кириллическое слово → латинский бренд
BRAND_ALIASES_REVERSE = {}
for brand, alts in BRAND_ALIASES.items():
    BRAND_ALIASES_REVERSE[brand] = brand
    for alt in alts:
        BRAND_ALIASES_REVERSE[alt] = brand


# ── улучшенные токены редакций ────────────────────────────────────
# Расширяем словарь, добавляем несовместимые пары.
EDITION_TOKENS = {
    "pro": "professional",
    "professional": "professional",
    "home": "home",
    "enterprise": "enterprise",
    "ent": "enterprise",
    "standard": "standard",
    "std": "standard",
    "lite": "lite",
    "portable": "portable",
    "port": "portable",
    "student": "student",
    "stu": "student",
    "plus": "plus",
    "free": "free",
    "business": "business",
    "biz": "business",
    "server": "server",
    "srv": "server",
    "x64": "x64",
    "x86": "x86",
    "ultimate": "ultimate",
    "workstation": "workstation",
    # кириллические варианты
    "про": "professional",
    "стандарт": "standard",
    "домашний": "home",
    "домашняя": "home",
    "бизнес": "business",
    "предприятие": "enterprise",
    "корпоративный": "enterprise",
    "студент": "student",
    "бесплатный": "free",
    "сервер": "server",
    "серверный": "server",
    "профессиональный": "professional",
    "профи": "professional",
    "лайт": "lite",
    "портативный": "portable",
    "портативная": "portable",
    "рекавери": "recovery",
    "рескю": "rescue",
}

# Некоммутативные пары: если в запросе указана одна, товар с другой не показываем.
INCOMPATIBLE_EDITIONS = [
    {"x64", "x86"},
    {"portable", "x64"},       # portable обычно без привязки к архитектуре, но x86 явно указывает 32-бит
    {"professional", "home"},
    {"professional", "free"},
    {"student", "business"},
    {"server", "workstation"},  # server ≠ workstation
]


# ── сокращения (расширяем) ───────────────────────────────────────
ABBREVIATIONS = {
    "pwd": "password",
    "mgr": "manager",
    "antiv": "antivirus",
    "txt": "text",
    "ent": "enterprise",
    "prof": "professional",
    "std": "standard",
    "port": "portable",
    "rec": "recovery",
    "gen": "generator",
    "stu": "student",
    "biz": "business",
    "srv": "server",
    "calc": "calculator",
    "firewall": "firewall",
}


# ── продуктовые линейки (ключевое слово запроса → подстрока в названии) ──
# Когда бренд определён, а остаток запроса содержит одно из этих слов,
# кандидаты сужаются до товаров, чьё название содержит эту подстроку.
# Это предотвращает подмену продуктовой линейки совпадением по редакции:
#   "cloudvault sync pro"  → Sync, а не Backup Pro
#   "netshield firewall free" → Firewall, а не Antivirus Free
PRODUCT_LINE_KEYWORDS = {
    "sync": "sync",
    "backup": "backup",
    "antivirus": "antivirus",
    "firewall": "firewall",
    "studio": "studio",
    "ide": "ide",
    "video": "video",
    "audio": "audio",
    "recovery": "recovery",
    "password": "password",
    "pdf": "pdf",
    "mail": "mail",
    "editor": "editor",
    "player": "player",
    "generator": "generator",
    "client": "client",
    "portable": "portable",
    "text": "text",
}


# ── категории контекста (интерес → маркеры в названии товара) ──────
# Используются контекстным ранжированием: если интерес пользователя
# совпадает с категорией, товары с этими маркерами в названии получают
# надбавку. Явно запрошенные продукт/назначение/год/редакция приоритетнее.
CONTEXT_CATEGORIES = {
    "media": ["video", "audio", "player"],
    "video": ["video"],
    "audio": ["audio"],
    "office": ["taskflow", "task", "flow"],
    "productivity": ["bytenote", "note", "editor", "text"],
    "graphics": ["pixeldesk", "pixel", "desk"],
    "security": ["netshield", "shield", "antivirus", "firewall",
                 "securepass", "password"],
    "backup": ["cloudvault", "vault", "backup", "sync"],
    "documents": ["reportgen", "pdf", "generator", "document"],
    "email": ["cloudmail", "mail", "client"],
    "recovery": ["datarescue", "rescue", "recovery"],
    "development": ["codeforge", "forge", "ide"],
}

# Вес надбавки за попадание в категорию интереса.
CONTEXT_INTEREST_BONUS = 20.0
# Вес надбавки за тот же бренд, что у просмотренного товара.
CONTEXT_VIEWED_BRAND_BONUS = 8.0
# Вес надбавки за ту же продуктовую линейку, что у просмотренного товара.
CONTEXT_VIEWED_LINE_BONUS = 20.0


# ── поля карточки для рекомендаций менеджеру ──────────────────────
# kind → (метка поля в карточке, шаблон проверки)
ATTRIBUTE_FIELDS = {
    "architecture": "Архитектура",
    "edition": "Редакция",
    "year": "Версия (год)",
}

# Формулировка проверки по типу пробела.
ATTRIBUTE_CHECK_VERB = {
    "architecture": "Проверить поддержку {value}",
    "edition": "Проверить наличие редакции {value}",
    "year": "Проверить наличие версии {value}",
}


# ── минимальный порог RapidFuzz ───────────────────────────────────
THRESHOLD = 55
