"""query_normalize.py — общая нормализация запросов поиска и консультанта.

Автоисправление ошибочной раскладки клавиатуры (ЙЦУКЕН ↔ QWERTY).

Правила:
  * преобразование идёт по физическим клавишам: «cthdth» → «сервер»;
  * слово заменяется, только если исходное слово не распознано словарём,
    а преобразованный вариант в словаре есть (однозначное подтверждение);
  * токены с цифрами (артикулы, версии «1.7», архитектуры «x86-64») и уже
    распознанные названия не трогаются;
  * в смешанном запросе каждое слово обрабатывается отдельно.

Один и тот же код используют обычный поиск (organizer_search.search) и
ИИ-консультант (ai_consultant.Consultant.ask) — правила не расходятся.
"""

from __future__ import annotations

import re

import config_v3
from organizer_index import canon_token

# ── соответствие клавиш ЙЦУКЕН ↔ QWERTY ───────────────────────────
# Кириллическая раскладка → символ латинской на той же физической клавише.
_RU_TO_EN = {
    "й": "q", "ц": "w", "у": "e", "к": "r", "е": "t", "н": "y", "г": "u",
    "ш": "i", "щ": "o", "з": "p", "х": "[", "ъ": "]",
    "ф": "a", "ы": "s", "в": "d", "а": "f", "п": "g", "р": "h", "о": "j",
    "л": "k", "д": "l", "ж": ";", "э": "'",
    "я": "z", "ч": "x", "с": "c", "м": "v", "и": "b", "т": "n", "ь": "m",
    "б": ",", "ю": ".",
}
# Обратное соответствие: только буквы (пунктуация не нужна — латиница,
# набранная в русской раскладке, состоит из букв).
_EN_TO_RU = {en: ru for ru, en in _RU_TO_EN.items() if en.isalpha()}

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")

_CYRILLIC = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")

# ── словарь поисковых терминов ────────────────────────────────────
# Общие слова запросов: нужны, чтобы исправление работало и на небольшом
# индексе. Названия товаров и брендов берутся из словаря каталога.
_COMMON_QUERY_WORDS = {
    "сервер", "серверный", "компьютер", "ноутбук", "монитор", "принтер",
    "сканер", "клавиатура", "мышь", "накопитель", "диск", "память",
    "процессор", "сеть", "коммутатор", "маршрутизатор", "роутер",
    "лицензия", "подписка", "версия", "редакция", "антивирус", "брандмауэр",
    "резервное", "копирование", "бэкап", "операционная", "система",
    "программное", "обеспечение", "оборудование", "комплектующие",
    "кабель", "адаптер", "карта", "видеокарта", "материнская", "плата",
    "источник", "питание", "корпус", "кулер", "мфу", "проектор", "ибп",
    "стойка", "консоль", "хранилище", "облако", "почта", "редактор",
    "плеер", "клиент", "менеджер", "генератор", "восстановление",
    "синхронизация", "фаервол", "пароль", "документ", "текст",
}


def _dictionary_words() -> set[str]:
    """Слова из словаря поисковых терминов (кириллица и латиница)."""
    words: set[str] = set()
    for phrase in (list(config_v3.COMMON_TERMS)
                   + list(config_v3.BRAND_ALIASES_REVERSE)
                   + list(config_v3.EDITION_TOKENS)
                   + list(config_v3.ABBREVIATIONS)
                   + list(config_v3.PRODUCT_LINE_KEYWORDS)):
        for part in _WORD_RE.findall(phrase or ""):
            words.add(part.lower().replace("ё", "е"))
    words.update(_COMMON_QUERY_WORDS)
    return words


# Сравнение по канонической основе: «серверы» и «сервер» — одно слово.
_TERM_CANON = {canon_token(word) for word in _dictionary_words()}


def term_known(word: str) -> bool:
    """Слово подтверждено словарём поисковых терминов."""
    w = (word or "").lower().replace("ё", "е")
    return bool(w) and canon_token(w) in _TERM_CANON


def _match_case(source: str, converted: str) -> str:
    if source.isupper():
        return converted.upper()
    if source[:1].isupper() and source[1:].islower():
        return converted[:1].upper() + converted[1:]
    return converted


def _convert_word(word: str) -> str | None:
    """Перевод слова в другую раскладку или None, если это не слово."""
    lowered = word.lower()
    if all("a" <= c <= "z" for c in lowered):
        table = _EN_TO_RU
    elif all(c in _CYRILLIC for c in lowered):
        table = _RU_TO_EN
    else:
        return None
    converted = "".join(table.get(c, c) for c in lowered)
    return _match_case(word, converted) if converted != lowered else None


def fix_keyboard_layout(text: str, known) -> tuple[str, list[dict]]:
    """Исправляет слова, набранные не в той раскладке.

    `known(word)` — проверка распознавания слова (словарь каталога и/или
    поисковых терминов). Замена принимается, только когда исходное слово
    не распознано, а преобразованное — распознано.
    """
    notes: list[dict] = []

    def replace(match: re.Match) -> str:
        chunk = match.group(0)
        if any(c.isdigit() for c in chunk):
            # Артикулы, версии, архитектуры: «1.7», «x86-64», «530606».
            return chunk
        if known(chunk):
            return chunk
        converted = _convert_word(chunk)
        if not converted or not known(converted):
            return chunk
        notes.append({"from": chunk, "to": converted, "kind": "layout"})
        return converted

    fixed = _WORD_RE.sub(replace, text or "")
    return fixed, notes


def layout_note(notes: list[dict]) -> str:
    """Готовая подпись: «Исправлена раскладка: cthdth → сервер»."""
    if not notes:
        return ""
    fixes = ", ".join(f"{n['from']} → {n['to']}" for n in notes)
    return f"Исправлена раскладка: {fixes}"
