"""ai_consultant.py — ИИ-консультант поверх локального индекса каталога.

Провайдер выбирается переменной AI_PROVIDER:
  * gigachat — GigaChat для физлиц (scope GIGACHAT_API_PERS) через
    официальный Python SDK `gigachat`. Модель делает ОДИН вызов на реплику
    и только выделяет поисковый запрос и обязательные условия; товары,
    цены, наличие, ссылки и совместимость берутся из локального индекса
    и правил подборки (selection.py).
  * yandexgpt — YandexGPT Pro 5.1 через OpenAI-совместимый REST API
    (https://llm.api.cloud.yandex.net/v1, заголовок Authorization: Api-Key).
  * без ключа — имитация провайдера (mock): та же структура ответа, но
    решение принимает локальная логика. Ответ явно помечается как
    полученный без модели.

Для GigaChat TLS-проверка НЕ отключается: путь к корневому сертификату
НУЦ Минцифры задаётся в GIGACHAT_CA_BUNDLE_FILE (см. .env.example).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field

import httpx

import organizer_search as osr
import product_scope
from organizer_index import (OFFER_TYPE_LABELS, STOPWORDS, connect, get_offer,
                             get_meta, normalize_text, query_tokens, tokenize)
from selection import COMPAT_RULES, build_top4, extract_requirements
from query_normalize import layout_note

DEFAULT_BASE_URL = "https://llm.api.cloud.yandex.net/v1"
DEFAULT_MODEL = "gpt://{folder}/yandexgpt/latest"
# Тариф YandexGPT Pro 5.1 (синхронный вызов), ₽ за 1000 токенов.
# Источник: открытые тарифы Yandex AI Studio; значение расчётное и
# подлежит сверке с договором.
PRICE_PER_1K_TOKENS_RUB = 0.8

# GigaChat: актуальный endpoint и scope для физлиц (документация Сбера).
DEFAULT_GIGACHAT_BASE_URL = "https://api.giga.chat/v1"
DEFAULT_GIGACHAT_MODEL = "GigaChat-2"
DEFAULT_GIGACHAT_SCOPE = "GIGACHAT_API_PERS"
# Ориентир по тарифу GigaChat для физлиц, ₽ за 1000 токенов.
# Значение расчётное и подлежит сверке с тарифом выбранной модели.
GIGACHAT_PRICE_PER_1K_TOKENS_RUB = 0.065
# Проверенные тарифы из таблицы документации Сбера (₽ за 1000 токенов).
# Модель, которой здесь нет (например, GigaChat-2), считается без
# подтверждённого тарифа — стоимость не показываем.
GIGACHAT_VERIFIED_TARIFFS = {
    "gigachat-2-lite": 0.065,
    "gigachat-2-pro": 0.5,
    "gigachat-2-max": 0.65,
}


def verified_tariff(model: str) -> float | None:
    """Тариф ₽/1000 токенов, если он подтверждён документацией модели."""
    return GIGACHAT_VERIFIED_TARIFFS.get((model or "").strip().lower())

MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "800"))
TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "20"))
MAX_CALLS_PER_SESSION = int(os.getenv("AI_MAX_CALLS_PER_SESSION", "8"))
MAX_TOOL_ROUNDS = 3

# Публикации вендоров, которые нельзя подтвердить по фиду.
VENDOR_PUBLICATIONS = {
    "кибер бэкап": {
        "url": ("https://cyberprotect.ru/podderzhivaemyie-texnologii/"
                "cyber-backup-astra-linux-specialedition/"),
        "title": ("Кибер Бэкап: поддерживаемые технологии — "
                  "Astra Linux Special Edition"),
        "supports": ("Astra Linux Special Edition",),
        "note": ("публикация вендора подтверждает поддержку Astra Linux "
                 "Special Edition; конкретная версия (например, 1.7) в фиде "
                 "не указана — применимость к предложению нужно сверить "
                 "с вендором"),
    },
}


# ── карточки ───────────────────────────────────────────────────────

def make_card(offer: dict, reason: str = "", bucket: str = "") -> dict:
    """Карточка товара строго из данных индекса."""
    price = offer.get("price")
    return {
        "id": offer["id"],
        "group_id": offer.get("group_id"),
        "name": offer.get("name"),
        "vendor": offer.get("vendor"),
        "url": offer.get("url"),
        "category_path": offer.get("category_path"),
        "price": price,
        "price_text": (f"{price:,.0f} ₽".replace(",", " ")
                       if price else "Цена не указана"),
        "currency": offer.get("currency"),
        "available": bool(offer.get("available")),
        "available_text": ("в наличии" if offer.get("available")
                           else "нет в наличии"),
        "offer_type": offer.get("offer_type") or "unknown",
        "type_label": OFFER_TYPE_LABELS.get(offer.get("offer_type") or "unknown",
                                            "тип не указан"),
        "license_type_raw": offer.get("license_type_raw") or "",
        "platform": offer.get("platform") or "",
        "duration": offer.get("duration") or "",
        "reason": reason,
        "bucket": bucket,
    }


# ── функции каталога (только чтение) ───────────────────────────────

class Catalog:
    """Обёртка над индексом: ровно две функции для модели."""

    def __init__(self, db_path=None, check_same_thread: bool = False):
        # check_same_thread=False: веб-стенд обрабатывает запросы в пуле потоков,
        # индекс при этом открывается один раз на процесс.
        self.conn = (connect(check_same_thread=check_same_thread)
                     if db_path is None else connect(db_path))

    def stats(self) -> dict:
        meta = get_meta(self.conn)
        return {
            "offers": int(meta.get("offer_count", 0)),
            "groups": int(meta.get("group_count", 0)),
            "built_at": meta.get("built_at", ""),
            "schema_version": meta.get("schema_version", ""),
        }

    def search(self, query: str, limit: int = 6,
               reqs: dict | None = None) -> dict:
        result = osr.search(self.conn, query, limit=max(limit, 4), reqs=reqs)
        cards = []
        for group in result["groups"][:limit]:
            cards.append(make_card(group["best"], group["reason"],
                                   group["bucket"]))
        return {
            "query": query,
            "intent": result["intent"]["name"],
            "total": result["total"],
            "shortfall": result["shortfall"],
            "buckets": {k: len(v) for k, v in (result.get("buckets") or {}).items()},
            "cards": cards,
        }

    def related(self, offer_id: str, limit: int = 4) -> dict:
        base = get_offer(self.conn, str(offer_id))
        if not base:
            return {"offer_id": offer_id, "error": "предложение не найдено",
                    "cards": []}

        family = osr.display_name(base.get("name") or "")
        rows = self.conn.execute(
            "SELECT * FROM offers WHERE id != ? AND available = 1 "
            "AND (vendor = ? OR category_id = ?) "
            "ORDER BY (price > 0) DESC, price ASC LIMIT 400",
            (base["id"], base.get("vendor") or "", base.get("category_id") or ""))
        cards, seen_groups, seen_families = [], {base.get("group_id")}, set()
        for row in rows:
            offer = dict(row)
            offer["available"] = bool(offer["available"])
            offer["params"] = {}
            if offer.get("group_id") in seen_groups:
                continue
            if (offer.get("offer_type") or "unknown") not in (
                    "base", "subscription", "support", "renewal", "addon"):
                continue
            key = (offer.get("vendor"), osr.display_name(offer.get("name") or ""))
            if key in seen_families:
                continue
            seen_families.add(key)
            seen_groups.add(offer.get("group_id"))

            reasons = []
            if offer.get("vendor") == base.get("vendor"):
                reasons.append("тот же вендор")
            if offer.get("category_id") == base.get("category_id"):
                reasons.append("та же категория каталога")
            otype = offer.get("offer_type") or "unknown"
            if otype in ("support", "renewal") and (
                    family.split()[0].lower() in (offer.get("name") or "").lower()):
                reasons.append("сопровождение/продление этой же линейки")
            cards.append(make_card(offer, "; ".join(reasons) or "соседнее решение",
                                   "related"))
            if len(cards) >= limit:
                break
        return {"offer_id": base["id"], "product": base.get("name"),
                "cards": cards}

    def card(self, offer_id: str) -> dict | None:
        offer = get_offer(self.conn, str(offer_id))
        return make_card(offer) if offer else None


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_catalog",
            "description": ("Поиск товаров в каталоге организатора. Возвращает "
                            "карточки с ID, типом предложения, ценой, наличием "
                            "и ссылкой. Только чтение."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "слова запроса, например «резервное копирование»"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_related",
            "description": ("Соседние предложения к товару: та же категория, "
                            "тот же вендор, сопровождение/продление. Каждая "
                            "карточка содержит основание рекомендации."),
            "parameters": {
                "type": "object",
                "properties": {
                    "offer_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 6},
                },
                "required": ["offer_id"],
            },
        },
    },
]

SYSTEM_PROMPT = """Ты — консультант магазина Softline. Помогаешь подобрать товар \
из каталога организатора.

Правила:
1. Данные о товарах бери ТОЛЬКО через функции search_catalog и get_related.
   Не придумывай названия, ID, цены, наличие и ссылки.
2. Различай тип предложения: базовая/новая лицензия, подписка, продление,
   сопровождение/техподдержка, cross-grade, обновление, дополнение.
   Для новой покупки предлагай базовую лицензию или подписку; продление и
   сопровождение — только если у пользователя уже есть лицензия.
3. Для каждого рекомендованного товара называй основание: тип предложения,
   совпадение с задачей, цену и наличие из карточки.
4. Если не хватает одного факта (например, ОС или редакция) — задай ОДИН
   короткий вопрос и не задавай больше.
5. Совместимость, которую нельзя подтвердить по фиду, помечай как
   «не подтверждено по фиду» и ссылайся на публикацию вендора.
6. Не обещай скидок, сроков поставки и не считай итоговую стоимость заказа.
7. Отвечай по-русски, кратко: 3–6 предложений и список товаров.
"""


# ── сессия ─────────────────────────────────────────────────────────

@dataclass
class Session:
    id: str
    messages: list[dict] = field(default_factory=list)
    intent: str = ""
    product: dict | None = None
    requirements: dict = field(default_factory=dict)
    calls: int = 0
    cards: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"prompt": 0, "completion": 0})
    created: float = field(default_factory=time.time)
    # Последний поисковый запрос и его топ-4: уточняющие вопросы
    # (лицензия, дополнения, стоимость…) отвечают по уже найденному,
    # а не запускают тот же поиск заново.
    last_query: str = ""
    last_top4: dict | None = None
    question_type: str = ""

    def context_summary(self) -> dict:
        return {"intent": self.intent,
                "product": dict(self.product) if self.product else None,
                "requirements": dict(self.requirements)}


# ── провайдеры ─────────────────────────────────────────────────────

class ProviderError(Exception):
    pass


# ── GigaChat: разбор реплики одним вызовом ─────────────────────────

EXTRACT_PROMPT = """Ты — разборщик запросов магазина Softline. По фразе \
пользователя и контексту диалога выдели:
1. query — короткий поисковый запрос к каталогу (продукт, категория или задача);
2. requirements — обязательные условия, которые пользователь назвал явно:
   os (операционная система), version (список версий), arch (архитектура),
   license_type (renewal/support/addon/subscription), wants_os (bool).

Правила:
- Не придумывай условия, которых нет в фразе или контексте.
- Сохраняй ранее названные условия из контекста, если пользователь их не отменил.
- Версию ОС указывай отдельно от названия: «Astra Linux 1.7» → os «Astra Linux Special Edition», version ["1.7"].
- Для задачи «резервное копирование» query — «кибер бэкап».
- Если пользователь просит сервер, query — «сервер»; если ПО для сервера — «ПО для сервера».
- Ответ — ТОЛЬКО JSON без пояснений и без markdown:
{"query": "...", "requirements": {"os": null, "version": [], "arch": null, "license_type": null, "wants_os": false}}
"""

_ARCH_CANON = {"x86-64", "aarch64", "arm", "e2k"}
_LICENSE_TYPES = {"renewal", "support", "addon", "subscription"}
_VERSION_RE = re.compile(r"^(?:19|20)\d{2}$|^\d+\.\d+(?:\.\d+)?$")
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I | re.M)


def _extract_json_object(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели, отбрасывая markdown-обёртку."""
    text = (text or "").strip()
    if not text:
        return {}
    cleaned = _FENCE_RE.sub("", text).strip()
    for candidate in (cleaned, text):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {}


def _canonical_os(value) -> str | None:
    """Приводит ОС из ответа модели к каноническому названию правила."""
    if not isinstance(value, str) or not value.strip():
        return None
    low = normalize_text(value)
    for rule in COMPAT_RULES:
        if rule["kind"] != "os":
            continue
        if normalize_text(rule["product"]) in low or any(
                alias in low for alias in rule["aliases"]):
            return rule["product"]
    return None


def _clean_model_requirements(raw) -> dict:
    """Серверная валидация условий из ответа модели.

    Всё, что не прошло проверку по допустимым значениям, отбрасывается:
    модель не может расширить набор условий произвольными строками.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    os_name = _canonical_os(raw.get("os"))
    if os_name:
        out["os"] = os_name
    versions = []
    for value in raw.get("version") or []:
        if isinstance(value, str) and _VERSION_RE.match(value.strip()):
            versions.append(value.strip())
    if versions:
        out["version"] = versions
    arch = raw.get("arch")
    if isinstance(arch, str) and arch.strip().lower() in _ARCH_CANON:
        out["arch"] = arch.strip().lower()
    license_type = raw.get("license_type")
    if isinstance(license_type, str) and license_type.strip().lower() in _LICENSE_TYPES:
        out["license_type"] = license_type.strip().lower()
    if isinstance(raw.get("wants_os"), bool):
        out["wants_os"] = raw["wants_os"]
    return out


# ── тип вопроса и граница задачи ──────────────────────────────────
#
# Покупатель задаёт разные вопросы об одном и том же товаре. Для них
# нужен разный ответ, а не одинаковый повторный поиск:
#   select     — подберите товар;
#   compat     — совместимо ли;
#   unsuitable — что не подойдёт и почему;
#   license    — какую лицензию брать;
#   extras     — что ещё потребуется;
#   compare    — сравнить варианты;
#   cost       — во сколько обойдётся.
#
# Уточняющие вопросы отвечают по уже найденному набору (last_top4), без
# повторного запуска того же поиска.

_QUESTION_TYPES = (
    ("unsuitable", re.compile(
        r"не\s+подойд|неподход|не\s+подход|что\s+не\s+подойд|"
        r"каки\w*\s+.*\bне\s+(?:подойд|подход)|исключ|почему\s+не\s+подойд|"
        r"причин\w*\s+исключ", re.I)),
    ("license", re.compile(
        r"как\w*\s+лицензи|какую\s+лицензи|тип\s+лицензи|"
        r"выбрать\s+лицензи|лицензи\w*\s+(?:выбрать|брать|нужн)|"
        r"какая\s+лицензи", re.I)),
    ("extras", re.compile(
        r"что\s+ещё|что\s+еще|дополнительн|дополнени|что\s+потребу|"
        r"что\s+нужно\s+(?:ещё|еще)|докупить|сопутств", re.I)),
    ("compare", re.compile(
        r"сравн|разниц|чем\s+отлич|что\s+лучше|что\s+выбрать|"
        r"какой\s+лучше", re.I)),
    ("cost", re.compile(
        r"во\s+сколько|сколько\s+стои|стоимост|обойд|цена\s+решени|"
        r"итогов\w*\s+стои|расчёт|расчет|бюджет|сколько\s+будет", re.I)),
    ("compat", re.compile(
        r"совместим|подойдёт\s+ли|подойдет\s+ли|подходит\s+ли|"
        r"работает\s+ли|поддержива", re.I)),
)

# Типы вопросов, которые отвечают по уже найденному набору.
FOLLOWUP_TYPES = {"unsuitable", "license", "extras", "compare", "cost"}


def classify_question(text: str) -> str:
    """Тип вопроса покупателя (select по умолчанию)."""
    low = normalize_text(text or "")
    for name, pattern in _QUESTION_TYPES:
        if pattern.search(low):
            return name
    return "select"


# Явные признаки новой независимой задачи в той же сессии.
_NEW_TASK_RE = re.compile(
    r"а\s+теперь|теперь\s+(?:мне\s+)?(?:нужн|надо|подбери|ищи)|"
    r"друг\w*\s+(?:задач|продукт|вопрос)|нов\w*\s+(?:задач|вопрос)|"
    r"переключ|следующ\w*\s+(?:задач|вопрос)|ещё\s+одн\w*\s+задач", re.I)
# Короткие ответы на уточняющий вопрос — это продолжение, а не новая задача.
# Названий продуктов здесь нет: короткая реплика покрыта общим правилом
# (три слова и меньше).
_CONTINUATION_RE = re.compile(
    r"^(?:весь|всего|отдельн|файл|каталог|образ|полн|системн|"
    r"да|нет|пожалуйста|нужн|конечно|верно)", re.I)


def _is_new_task(message: str, prev_classes, cur_classes,
                 prev_query: str = "") -> bool:
    """Новая независимая задача или продолжение текущей."""
    text = (message or "").strip()
    if _NEW_TASK_RE.search(text):
        return True
    if _CONTINUATION_RE.search(text) or len(text.split()) <= 3:
        return False
    # Общие слова с прошлым запросом — это продолжение той же задачи
    # («сервер» → «резервное копирование сервера»).
    if prev_query:
        try:
            if set(query_tokens(message)) & set(query_tokens(prev_query)):
                return False
        except Exception:
            pass
    prev, cur = set(prev_classes or []), set(cur_classes or [])
    return bool(prev) and bool(cur) and prev.isdisjoint(cur)


def _merge_requirements(server: dict, model: dict, previous: dict | None,
                        new_task: bool = False) -> dict:
    """Объединяет локальные, модельные и унаследованные условия.

    Локальные (regex) условия приоритетнее модельных. Версии из прошлой
    реплики переносятся только для той же ОС — так «Astra Linux 1.7»
    не теряется при уточнении задачи.

    Роль, объём восстановления, «сервер уже есть» и версия ALD переносятся
    из прошлой реплики, ТОЛЬКО если это продолжение той же задачи. При
    новой независимой задаче (`new_task=True`) унаследованные ограничения
    сбрасываются: иначе антивирус после бэкапа получит чужую ОС, версию,
    тип лицензии и объём восстановления.
    """
    if new_task:
        previous = {}
    previous = previous or {}
    reqs = {
        "os": (server.get("os") or model.get("os")
               or previous.get("os")),
        "os_rule": None,
        "version": [],
        "arch": server.get("arch") or model.get("arch"),
        "license_type": server.get("license_type") or model.get("license_type"),
        "exclude": list(server.get("exclude") or []),
        "wants_os": bool(server.get("wants_os") or model.get("wants_os")),
        "role": (server.get("role") or model.get("role")
                 or previous.get("role")),
        "restore_scope": (server.get("restore_scope")
                          or model.get("restore_scope")
                          or previous.get("restore_scope")),
        "server_owned": bool(server.get("server_owned")
                             or previous.get("server_owned")),
        # Платформа как условие («антивирус для Windows») переносится в
        # пределах той же задачи и сбрасывается при новой.
        "platform_family": (server.get("platform_family")
                            or previous.get("platform_family")),
        "ald_version": (server.get("ald_version") or model.get("ald_version")
                        or previous.get("ald_version")),
        "clarify_asked": bool(previous.get("clarify_asked")),
    }
    for rule in COMPAT_RULES:
        if rule["kind"] == "os" and reqs["os"] == rule["product"]:
            reqs["os_rule"] = rule["id"]
            break

    versions: list[str] = []
    prev_versions = (previous.get("version") or []
                     if previous.get("os") == reqs["os"] else [])
    for source in (server.get("version"), model.get("version"), prev_versions):
        for version in source or []:
            if version not in versions:
                versions.append(version)
    reqs["version"] = versions
    return reqs


# ── сохранение исходного намерения ────────────────────────────────
#
# Модель может сузить общий запрос до конкретного бренда: на фразу
# «нужно резервное копирование» ответить query «кибер бэкап». Если
# пользователь бренд не называл, искать нужно по категории и синонимам
# среди разных производителей. Правило опирается на данные каталога:
# токен считается брендовым, если он встречается как вендор в индексе.

_BRAND_STOP_TOKENS = {
    "лиценз", "подписк", "сервер", "серверн", "программ", "обеспечен",
    "резервн", "копирован", "бэкап", "backup", "восстановлен", "защит",
    "антивирус", "почт", "файл", "данн", "систем", "платформ", "облачн",
    "редакц", "верси", "пакет", "комплект", "для", "windows", "linux",
    "astra", "астра", "unix", "macos",
}

_BRAND_CACHE: dict[int, frozenset] = {}


def _vendor_tokens(conn) -> frozenset:
    """Токены вендоров из индекса (кеш на соединение)."""
    cached = _BRAND_CACHE.get(id(conn))
    if cached is not None:
        return cached
    tokens: set[str] = set()
    for (vendor,) in conn.execute("SELECT DISTINCT vendor FROM offers"):
        for token in normalize_text(vendor or "").split():
            cleaned = re.sub(r"[^0-9a-zа-яё]", "", token)
            if len(cleaned) >= 3:
                tokens.add(cleaned)
    data = frozenset(tokens)
    _BRAND_CACHE[id(conn)] = data
    return data


def _introduced_brand(query: str, message: str, conn) -> str:
    """Бренд, который модель добавила в запрос, но пользователь не называл.

    Возвращает найденный брендовый токен или пустую строку. Сравнение идёт
    по основам: «кибер бэкап» из ответа модели против «резервное
    копирование» в реплике пользователя.
    """
    user_tokens = set(query_tokens(message)) | set(normalize_text(message).split())
    brand_tokens = _vendor_tokens(conn)
    for token in query_tokens(query):
        if token in _BRAND_STOP_TOKENS or token in user_tokens:
            continue
        if any(token in brand or brand in token
               for brand in brand_tokens if len(brand) >= 3):
            return token
    return ""


def _named_brand(query: str, message: str, conn) -> bool:
    """Пользователь сам назвал бренд в реплике?"""
    q_tokens = set(query_tokens(query))
    if not q_tokens:
        return False
    msg = normalize_text(message)
    brand_tokens = _vendor_tokens(conn)
    for token in q_tokens:
        if token in _BRAND_STOP_TOKENS:
            continue
        # Вендор в индексе может быть длиннее токена: «кибер» → «киберпротект».
        if any(token in brand or brand in token
               for brand in brand_tokens if len(brand) >= 3) \
                and token in msg:
            return True
    return False


def _intent_query(message: str) -> str:
    """Запрос по исходному намерению без бренда и уточнений.

    Берёт из реплики только слова задачи: «сервер уже есть, на нём Astra
    Linux 1.7, нужно резервное копирование» → «резервное копирование».
    """
    queries = _intent_queries(message)
    return queries[0] if queries else ""


def _intent_queries(message: str) -> list[str]:
    """Синонимы запроса по намерению: категория и другие названия.

    «Резервное копирование», «бэкап» и «backup» — одна задача, поэтому
    поиск идёт по всем формулировкам сразу: так в выдачу попадают разные
    производители, а не только тот, чьё название совпало буквально.
    """
    low = normalize_text(message)
    if any(word in low for word in ("резервн", "копирован", "бэкап", "backup",
                                    "восстановлен")):
        return ["резервное копирование", "бэкап", "backup"]
    if "антивирус" in low or "защит" in low:
        return ["антивирус"]
    return []


def _drop_conditions(text: str, reqs: dict) -> str:
    """Убирает из query токены ОС/версии/арх, которые уже учтены в reqs.

    Модель иногда подмешивает «Astra Linux 1.7» в поисковый запрос —
    это дублирует обязательные условия, которые уже извлечены в reqs.
    """
    drop: set[str] = set()
    os_val = (reqs.get("os") or "").lower().replace("ё", "е")
    if os_val:
        drop |= set(os_val.split())
        drop |= set(tokenize(os_val))
    for v in reqs.get("version") or []:
        s = str(v).lower()
        drop.add(s)
        drop |= set(s.split("."))
    arch = (reqs.get("arch") or "").lower().replace("ё", "е")
    if arch:
        drop |= set(arch.split())
    words: list[str] = []
    for w in (text or "").split():
        low = w.lower().replace("ё", "е").strip(".,;:()!? ")
        if low in drop:
            continue
        if tokenize(low) and tokenize(low) <= drop:
            continue
        words.append(w)
    return " ".join(words) or text


def _keep_user_tokens(base: str, query: str, message: str, reqs: dict) -> str:
    """К категории-намерению добавляет токены из query, которые пользователь
    произнёс сам (бренды, артикулы, архитектуры), но не условия ОС."""
    drop: set[str] = set()
    for part in (reqs.get("os"), reqs.get("arch")):
        if part:
            drop |= set(tokenize((part or "").lower().replace("ё", "e")))
    for v in reqs.get("version") or []:
        drop.add(str(v).lower())
    extra: list[str] = []
    for w in query.split():
        low = w.lower().replace("ё", "e").strip(".,;:()!? ")
        if not low or low in base.lower():
            continue
        if low in drop:
            continue
        if low not in (message or "").lower():
            continue
        if low in STOPWORDS or len(low) < 2:
            continue
        extra.append(w)
    return (base + " " + " ".join(extra)).strip() if extra else base


_BUCKET_PRIORITY = {"direct": 0, "for_os": 1, "related": 2, "alternative": 3}


def _merge_search_results(results: list[dict]) -> dict:
    """Объединяет выдачу нескольких синонимичных запросов без дублей."""
    if len(results) == 1:
        return results[0]
    by_key: dict[str, dict] = {}
    for result in results:
        for group in result.get("groups") or []:
            key = group.get("key") or group["best"]["id"]
            prev = by_key.get(key)
            if prev is None:
                by_key[key] = group
                continue
            prev_rank = (_BUCKET_PRIORITY.get(prev.get("bucket"), 9),
                         -prev.get("text_score", 0))
            new_rank = (_BUCKET_PRIORITY.get(group.get("bucket"), 9),
                        -group.get("text_score", 0))
            if new_rank < prev_rank:
                by_key[key] = group

    groups = sorted(by_key.values(),
                    key=lambda g: (-g.get("text_score", 0), -g.get("score", 0)))
    buckets: dict[str, list] = {}
    for group in groups:
        buckets.setdefault(group["bucket"], []).append(group)

    tokens: list[str] = []
    for result in results:
        for token in result.get("tokens") or []:
            if token not in tokens:
                tokens.append(token)

    merged = dict(results[0])
    merged.update({"groups": groups, "buckets": buckets, "tokens": tokens,
                   "total": len(groups), "shortfall": len(groups) < 4,
                   "sections": None, "bucket_titles": {}})
    return merged


class GigaChatProvider:
    """GigaChat для физлиц: один вызов модели на реплику.

    Модель только выделяет поисковый запрос и обязательные условия и
    возвращает строгий JSON. Сервер проверяет ответ и подбирает товары
    по локальному индексу. TLS-проверка не отключается: сертификат НУЦ
    Минцифры подключается через GIGACHAT_CA_BUNDLE_FILE.
    """

    name = "gigachat"

    def __init__(self):
        self.credentials = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
        self.base_url = os.getenv("GIGACHAT_BASE_URL",
                                  DEFAULT_GIGACHAT_BASE_URL).rstrip("/")
        self.model = (os.getenv("GIGACHAT_MODEL", "").strip()
                      or DEFAULT_GIGACHAT_MODEL)
        self.scope = (os.getenv("GIGACHAT_SCOPE", "").strip()
                      or DEFAULT_GIGACHAT_SCOPE)
        self.ca_bundle_file = (os.getenv("GIGACHAT_CA_BUNDLE_FILE", "").strip()
                               or None)

    @property
    def ready(self) -> bool:
        return bool(self.credentials)

    def _client(self):
        from gigachat import GigaChat

        kwargs = {
            "base_url": self.base_url,
            "credentials": self.credentials,
            "scope": self.scope,
            "model": self.model,
            "timeout": TIMEOUT_SECONDS,
        }
        if self.ca_bundle_file:
            kwargs["ca_bundle_file"] = self.ca_bundle_file
        # verify_ssl_certs намеренно не передаём: проверка сертификата включена.
        return GigaChat(**kwargs)

    def extract(self, message: str, context: dict | None = None) -> dict:
        from gigachat.exceptions import (AuthenticationError, ForbiddenError,
                                         GigaChatException, RateLimitError)

        if not self.ready:
            raise ProviderError("нет ключа GIGACHAT_CREDENTIALS")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": json.dumps(
                    {"message": message, "context": context or {}},
                    ensure_ascii=False)},
            ],
            "model_options": {"temperature": 0.0, "max_tokens": MAX_TOKENS},
        }
        try:
            with self._client() as client:
                reply = client.chat.create(payload)
        except RateLimitError as exc:
            raise ProviderError(
                "исчерпана квота или превышен лимит запросов GigaChat") from exc
        except (AuthenticationError, ForbiddenError) as exc:
            raise ProviderError(f"доступ к GigaChat запрещён: {exc}") from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderError(f"тайм-аут {TIMEOUT_SECONDS:.0f} с") from exc
        except GigaChatException as exc:
            raise ProviderError(f"GigaChat: {exc}") from exc
        except Exception as exc:  # сеть, TLS, неожиданный ответ SDK
            raise ProviderError(f"сеть/клиент GigaChat: {exc}") from exc

        message_obj = (reply.messages or [None])[0]
        content = ""
        if message_obj is not None and message_obj.content:
            if isinstance(message_obj.content, str):
                content = message_obj.content
            else:
                content = "".join(getattr(part, "text", "") or ""
                                  for part in message_obj.content)
        data = _extract_json_object(content)
        query = data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ProviderError("невалидный ответ модели: нет поискового запроса")

        usage = reply.usage
        api_usage = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        }
        return {
            "query": query.strip()[:200],
            "requirements": _clean_model_requirements(data.get("requirements")),
            "used_model": True,
            "model": self.model,
            "usage": {"prompt": api_usage["input_tokens"],
                      "completion": api_usage["output_tokens"]},
            "api_usage": api_usage,
            "raw_content": content[:1000],
        }


class YandexProvider:
    """YandexGPT Pro 5.1 через OpenAI-совместимый REST API."""

    name = "yandexgpt"

    def __init__(self):
        self.api_key = os.getenv("YANDEX_API_KEY", "").strip()
        self.folder = os.getenv("YANDEX_FOLDER_ID", "").strip()
        self.base_url = os.getenv("YANDEXGPT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        model = os.getenv("YANDEXGPT_MODEL", "").strip()
        self.model = model or DEFAULT_MODEL.format(folder=self.folder or "<folder>")

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.folder)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        if not self.ready:
            raise ProviderError("нет ключа YANDEX_API_KEY/YANDEX_FOLDER_ID")
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": MAX_TOKENS,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.post(f"{self.base_url}/chat/completions",
                                       json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderError(f"тайм-аут {TIMEOUT_SECONDS:.0f} с") from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"HTTP {exc.response.status_code}: "
                f"{exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"сеть: {exc}") from exc

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        return {
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
            "usage": {
                "prompt": int(usage.get("prompt_tokens") or 0),
                "completion": int(usage.get("completion_tokens") or 0),
            },
        }


class MockProvider:
    """Имитация провайдера без ключа.

    Протокол тот же, что у YandexGPT: сначала вызов функции, затем ответ.
    Для режима GigaChat доступен метод extract(): он возвращает ту же
    структуру, что и живой провайдер, но решение принимает локальная
    логика. Ответ всегда помечается как полученный без модели.
    """

    name = "mock"

    def __init__(self, reason: str = "ключ провайдера не задан"):
        self.reason = reason
        self.model = "mock"

    @property
    def ready(self) -> bool:
        return True

    def extract(self, message: str, context: dict | None = None) -> dict:
        """Локальный аналог разбора реплики: запрос и обязательные условия."""
        query, clarification = _mock_query(message)
        server = extract_requirements(message)
        model_reqs = {
            "os": server.get("os"),
            "version": list(server.get("version") or []),
            "arch": server.get("arch"),
            "license_type": server.get("license_type"),
            "wants_os": bool(server.get("wants_os")),
        }
        return {
            "query": query,
            "requirements": _clean_model_requirements(model_reqs),
            "used_model": False,
            "model": self.model,
            "usage": {"prompt": 0, "completion": 0},
            "api_usage": {},
            "clarification": clarification,
            "raw_content": "",
        }

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        user_text = ""
        tool_payload = None
        for message in reversed(messages):
            if message.get("role") == "user":
                user_text = message.get("content") or ""
                break
        for message in reversed(messages):
            if message.get("role") == "tool":
                try:
                    tool_payload = json.loads(message.get("content") or "{}")
                except json.JSONDecodeError:
                    tool_payload = None
                break

        if tool_payload is None:
            query, clarification = _mock_query(user_text)
            if clarification:
                return {"content": clarification, "tool_calls": [],
                        "usage": {"prompt": 0, "completion": 0}}
            call = {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": "search_catalog",
                             "arguments": json.dumps({"query": query,
                                                      "limit": 6},
                                                     ensure_ascii=False)},
            }
            return {"content": "", "tool_calls": [call],
                    "usage": {"prompt": 0, "completion": 0}}

        return {"content": _mock_answer(user_text, tool_payload),
                "tool_calls": [], "usage": {"prompt": 0, "completion": 0}}


_OS_RE = re.compile(r"(astra\s*linux(?:\s*(?:special\s*edition|se))?(?:\s*[\d.]+)?)",
                    re.I)


def _os_name(text: str) -> str:
    match = _OS_RE.search(text or "")
    if not match:
        return ""
    return " ".join(match.group(1).split()).rstrip(".")


_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")
_GENERIC_LATIN = {"server", "backup", "software", "hardware"}


def _keep_latin(base: str, text: str) -> str:
    """Дополняет категорию латинскими названиями и артикулами из запроса.

    «сервер Dell» не должен превращаться в «сервер»: распознанные названия
    (бренды, линейки, артикулы) сохраняются рядом с категорией.
    """
    extra: list[str] = []
    have = {w.lower() for w in _LATIN_WORD_RE.findall(base)}
    for word in _LATIN_WORD_RE.findall(text or ""):
        low = word.lower()
        if len(low) < 2 or low in _GENERIC_LATIN or low in STOPWORDS:
            continue
        if low in have:
            continue
        have.add(low)
        extra.append(word)
    return " ".join([base, *extra]).strip() if extra else base


_BACKUP_INTENT_RE = re.compile(r"резервн|бэкап|backup|восстановлен", re.I)

# Один уточняющий вопрос, если задача — восстановление, но не назван объект.
CLARIFY_RESTORE_SCOPE = ("Что нужно восстанавливать — весь сервер, отдельные "
                         "файлы или объекты службы каталогов ALD Pro?")


def _mock_query(text: str) -> tuple[str, str]:
    """Запрос к каталогу и, если нужно, один короткий вопрос.

    Бренд не подставляется: «нужно резервное копирование» ищется как
    «резервное копирование», а не как «кибер бэкап». Бренд используется,
    только если пользователь назвал его сам.
    """
    low = (text or "").lower().replace("ё", "е")
    # Восстановление объектов каталога (ALD Pro / Active Directory) —
    # отдельная задача: ищем средство восстановления, а не лицензию
    # на сам каталог.
    if "ald" in low or "active directory" in low or (
            "объект" in low and "каталог" in low):
        return "резервное копирование объектов каталога ALD Pro", ""
    if "резервн" in low or "бэкап" in low or "backup" in low:
        if "кибер" in low:
            return "кибер бэкап", ""
        return "резервное копирование", ""
    if "сервер" in low and ("по" in low.split() or "программ" in low):
        return "ПО для сервера", ""
    if "сервер" in low:
        return _keep_latin("сервер", text), ""
    if "anydesk" in low:
        return "anydesk", ""
    if "кибер" in low:
        return "кибер бэкап", ""
    if "astra" in low or "астра" in low:
        return "astra linux", ""
    if len(low.split()) >= 2:
        return text.strip(), ""
    return "", ("Уточните, пожалуйста, что нужно: например, «резервное "
                "копирование», «сервер» или конкретный продукт.")


def _mock_answer(user_text: str, payload: dict) -> str:
    cards = payload.get("cards") or []
    query = payload.get("query") or ""
    if not cards:
        return (f"По запросу «{query}» в каталоге ничего не нашлось. "
                f"Попробуйте назвать продукт или категорию иначе.")

    lines = [f"По запросу «{query}» в каталоге есть такие предложения:"]
    for i, card in enumerate(cards[:4], 1):
        lines.append(
            f"{i}. {card['name']} — {card['type_label']}, "
            f"{card['price_text']}, {card['available_text']}.")
    lines.append("")
    lines.append("Основание выбора: тип предложения, цена и наличие взяты "
                 "из карточки каталога; для новой покупки выбраны базовые "
                 "лицензии и подписки, продление и сопровождение помечены "
                 "отдельно.")

    os_name = _os_name(user_text)
    if os_name:
        pub = VENDOR_PUBLICATIONS.get("кибер бэкап")
        if pub and any("кибер" in (c.get("name") or "").lower() for c in cards):
            lines.append(
                f"Совместимость с {os_name}: по фиду версию подтвердить "
                f"нельзя, вендор заявляет поддержку "
                f"{', '.join(pub['supports'])} — {pub['url']}. "
                f"Применимость к конкретному предложению не подтверждена по фиду.")

    if payload.get("shortfall"):
        lines.append(f"В каталоге по этому запросу меньше 4 товаров "
                     f"(найдено {payload.get('total')}), добирать дублями "
                     f"лицензий не стал.")
    return "\n".join(lines)


def build_provider():
    """Выбор провайдера по AI_PROVIDER с безопасным откатом в заглушку."""
    choice = os.getenv("AI_PROVIDER", "").strip().lower()
    if choice == "gigachat":
        provider = GigaChatProvider()
        return provider if provider.ready else MockProvider(
            "нет ключа GIGACHAT_CREDENTIALS")
    yandex = YandexProvider()
    if choice in ("", "yandexgpt"):
        return yandex if yandex.ready else MockProvider(
            "нет ключа YANDEX_API_KEY/YANDEX_FOLDER_ID")
    return MockProvider(f"неизвестный AI_PROVIDER={choice}")


# ── консультант ────────────────────────────────────────────────────

class Consultant:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.sessions: dict[str, Session] = {}
        self.provider = build_provider()

    # сессии
    def new_session(self) -> Session:
        session = Session(id=uuid.uuid4().hex[:12])
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            session = Session(id=session_id or uuid.uuid4().hex[:12])
            self.sessions[session.id] = session
        return session

    # вызов функций
    def _run_tool(self, name: str, args: dict, session: Session) -> dict:
        if name == "search_catalog":
            query = str(args.get("query") or "")
            limit = int(args.get("limit") or 6)
            payload = self.catalog.search(query, limit,
                                          reqs=session.requirements or None)
            session.intent = payload["intent"]
            if payload["cards"]:
                session.product = payload["cards"][0]
            return payload
        if name == "get_related":
            payload = self.catalog.related(str(args.get("offer_id") or ""),
                                           int(args.get("limit") or 4))
            return payload
        return {"error": f"неизвестная функция: {name}"}

    def ask(self, session_id: str, message: str) -> dict:
        session = self.get_session(session_id)
        started = time.perf_counter()
        original = (message or "").strip()
        if not original:
            return {"answer": "Введите вопрос.", "cards": [],
                    "provider": self.provider.name, "session": session.id}

        # Общая с обычным поиском нормализация раскладки — до вызова модели,
        # чтобы модель не разбирала «cthdth» как бренд или опечатку.
        message, layout_fixes = osr.fix_layout(self.catalog.conn, original)
        session.messages.append({"role": "user", "content": message})
        if hasattr(self.provider, "extract"):
            result = self._ask_extract(session, message, started)
        else:
            result = self._ask_tools(session, message, started)
        result["layout_fixes"] = layout_fixes
        if layout_fixes:
            result["original_message"] = original
            note = layout_note(layout_fixes)
            result["answer"] = note + "\n" + (result.get("answer") or "")
        return result

    def _ask_extract(self, session: Session, message: str,
                     started: float) -> dict:
        """Один вызов модели: разбор запроса + локальная подборка товаров."""
        conn = self.catalog.conn
        # Условия извлекаются с индексом: класс продукта и платформа
        # («антивирус для Windows») определяются по категориям каталога.
        server_reqs = extract_requirements(message, conn)
        previous = dict(session.requirements or {})
        question_type = classify_question(message)
        session.question_type = question_type

        # Уточняющий вопрос по уже найденному набору: не запускаем тот же
        # поиск заново, а отвечаем по прошлому топ-4 (лицензия, дополнения,
        # сравнение, стоимость, что не подойдёт).
        if (question_type in FOLLOWUP_TYPES and session.last_top4
                and session.last_query and session.requirements):
            return self._answer_from_session(session, message, question_type,
                                             started)

        scope = product_scope.resolve(conn, message)
        cur_classes = scope.get("product_classes") or []
        new_task = _is_new_task(message,
                                previous.get("product_classes") or [],
                                cur_classes, session.last_query)

        extracted: dict = {}
        used_model = False
        fallback_reason = ""
        calls = 0
        api_usage: dict = {}

        if self.provider.name == "mock":
            extracted = self.provider.extract(message, session.context_summary())
        elif session.calls >= MAX_CALLS_PER_SESSION:
            fallback_reason = ("достигнут лимит обращений к модели в сессии "
                               f"({MAX_CALLS_PER_SESSION})")
        else:
            session.calls += 1
            calls = 1
            try:
                extracted = self.provider.extract(message,
                                                  session.context_summary())
                used_model = bool(extracted.get("used_model"))
                api_usage = extracted.get("api_usage") or {}
                usage = extracted.get("usage") or {}
                session.usage["prompt"] += int(usage.get("prompt") or 0)
                session.usage["completion"] += int(usage.get("completion") or 0)
            except ProviderError as exc:
                fallback_reason = str(exc)

        query = (extracted.get("query") or "").strip()
        if not query:
            local_query, _ = _mock_query(message)
            query = local_query or message

        reqs = _merge_requirements(server_reqs,
                                   extracted.get("requirements") or {},
                                   previous, new_task=new_task)
        # Класс продукта сохраняем в сессии, чтобы следующая реплика
        # понимала, о какой задаче речь. При новой задаче он берётся из
        # текущей реплики, при продолжении — из уже накопленных условий.
        if cur_classes or new_task or question_type == "select":
            reqs = product_scope.refine_requirements(conn, reqs, message)
            # Короткое уточнение той же задачи («объекты каталога») не
            # называет класс словами — он остаётся тем, что уже назван.
            if not cur_classes and not new_task and not reqs.get("product_classes"):
                reqs["product_classes"] = previous.get("product_classes") or []
                reqs["product_is_os"] = previous.get("product_is_os", False)
                reqs["os_is_constraint"] = previous.get("os_is_constraint",
                                                        False)
        else:
            reqs.setdefault("product_classes",
                            previous.get("product_classes") or [])
            reqs.setdefault("product_is_os",
                            previous.get("product_is_os", False))
            reqs.setdefault("os_is_constraint",
                            previous.get("os_is_constraint", False))

        # Один уточняющий вопрос: задача — восстановление, а объект не назван.
        # ОС, версия и «сервер уже есть» уже лежат в reqs и сохраняются в
        # сессии, поэтому следующая реплика («весь сервер» / «ALD Pro»)
        # продолжит подбор, а не начнёт его заново. Не задаём вопрос, если
        # пользователь сам назвал конкретный продукт: «Нужен Кибер Бэкап».
        if (_BACKUP_INTENT_RE.search(message)
                and not reqs.get("restore_scope")
                and not previous.get("restore_scope")
                and not previous.get("clarify_asked")
                and not _named_brand(query, message, self.catalog.conn)):
            reqs["clarify_asked"] = True
            session.requirements = reqs
            session.messages.append({"role": "assistant",
                                     "content": CLARIFY_RESTORE_SCOPE})
            elapsed = round((time.perf_counter() - started) * 1000)
            return {
                "answer": CLARIFY_RESTORE_SCOPE,
                "cards": [],
                "top4": {"items": [], "count": 0, "verified_count": 0,
                         "size": 4, "strict_os": bool(reqs.get("os")),
                         "needs_check": [], "needs_check_note": "",
                         "no_confirmed_message": "", "shortfall": True,
                         "shortfall_reason": "нужно уточнить объект "
                                             "восстановления",
                         "requirements": {
                             "os": reqs.get("os"),
                             "version": reqs.get("version") or [],
                             "arch": reqs.get("arch"),
                             "license_type": reqs.get("license_type"),
                             "wants_os": reqs.get("wants_os", False),
                             "summary": ""}},
                "clarification": True,
                "provider": self.provider.name,
                "model": getattr(self.provider, "model", ""),
                "live": used_model,
                "used_model": used_model,
                "fallback_reason": fallback_reason,
                "session": session.id,
                "context": session.context_summary(),
                "query": "",
                "requirements": {key: reqs.get(key) for key in
                                 ("os", "version", "arch", "license_type",
                                  "wants_os", "role", "restore_scope",
                                  "server_owned")},
                "verified_count": 0,
                "needs_check_count": 0,
                "no_confirmed_message": "",
                "calls": calls,
                "latency_ms": elapsed,
                "usage": dict(session.usage),
                "api_usage": api_usage,
                "cost": None,
                "cost_rub": None,
            }

        # Ответ на уточняющий вопрос: объект восстановления назван, поэтому
        # ищем средство под эту задачу, а не «сервер»/«ALD Pro» как товар.
        if previous.get("clarify_asked") and reqs.get("restore_scope"):
            if reqs["restore_scope"] == "directory":
                query = "резервное копирование объектов каталога ALD Pro"
            else:
                query = "резервное копирование"

        # Что искать: намерение пользователя важнее формулировки модели.
        intent_q = _intent_query(message)
        brand = _introduced_brand(query, message, self.catalog.conn)
        user_named = _named_brand(query, message, self.catalog.conn)

        if brand:
            # Модель сузила общий запрос до бренда, которого пользователь
            # не называл, — откат к намерению, поиск по категории.
            local_q, _ = _mock_query(message)
            base_query = intent_q or local_q or message
        elif user_named:
            # Бренд произнёс сам пользователь — он и задаёт поиск.
            base_query = query
        elif intent_q:
            # Намерение есть, бренд не назван: база — намерение, из запроса
            # модели оставляем только произнесённые пользователем названия и
            # артикулы. Условия ОС уже учтены в reqs и в query не нужны.
            base_query = _keep_user_tokens(
                intent_q, _drop_conditions(query, reqs), message, reqs)
        else:
            base_query = query

        search_queries = [base_query]
        if not user_named:
            # Без явного бренда ищем по категории и синонимам: «резервное
            # копирование», «бэкап» и «backup» — одна задача, а разные
            # формулировки дают разных производителей.
            for synonym in _intent_queries(message):
                if synonym not in search_queries:
                    search_queries.append(synonym)
        query = search_queries[0]

        results = [osr.search(self.catalog.conn, q, limit=12, reqs=reqs)
                   for q in search_queries]
        result = _merge_search_results(results)
        top4 = build_top4(result, reqs)
        confirmed = top4.get("items") or []
        needs_check = top4.get("needs_check") or []
        # В карточки попадают подтверждённые, затем — требующие проверки.
        cards = confirmed + needs_check

        answer = _compose_answer(query, top4, reqs, used_model, fallback_reason,
                                 self.provider.name,
                                 getattr(self.provider, "model", ""),
                                 question_type)

        session.intent = result["intent"]["name"]
        session.requirements = reqs
        if cards:
            session.product = cards[0]
        session.cards = cards
        session.last_query = query
        session.last_top4 = top4
        session.messages.append({"role": "assistant", "content": answer})

        elapsed = round((time.perf_counter() - started) * 1000)
        tokens = session.usage["prompt"] + session.usage["completion"]
        model_name = getattr(self.provider, "model", "")
        tariff = (verified_tariff(model_name)
                  if self.provider.name == "gigachat" else None)
        api_total = int(api_usage.get("total_tokens") or 0)
        cost = None
        if tariff is not None and api_total > 0:
            cost = {"amount_rub": round(api_total / 1000 * tariff, 4),
                    "price_per_1k_rub": tariff, "currency": "RUB",
                    "estimated": True}
        return {
            "answer": answer,
            "cards": cards,
            "top4": top4,
            "clarification": False,
            "provider": self.provider.name,
            "model": model_name,
            "live": used_model,
            "used_model": used_model,
            "fallback_reason": fallback_reason,
            "session": session.id,
            "context": session.context_summary(),
            "query": query,
            "requirements": {key: reqs.get(key) for key in
                             ("os", "version", "arch", "license_type",
                              "wants_os", "role", "restore_scope",
                              "server_owned", "product_classes",
                              "platform_family")},
            "verified_count": top4.get("verified_count", len(confirmed)),
            "needs_check_count": len(needs_check),
            "no_confirmed_message": top4.get("no_confirmed_message") or "",
            "calls": calls,
            "latency_ms": elapsed,
            "usage": dict(session.usage),
            "api_usage": api_usage,
            "cost": cost,
            "cost_rub": cost["amount_rub"] if cost else None,
            "question_type": question_type,
            "diagnostics": _diagnostics(self.provider.name, model_name,
                                        used_model, calls, elapsed,
                                        dict(session.usage), api_usage),
        }

    def _answer_from_session(self, session: Session, message: str,
                             question_type: str, started: float) -> dict:
        """Ответ на уточняющий вопрос по уже найденному набору.

        Повторный поиск не запускается: тип вопроса меняет только
        формулировку ответа (лицензия, дополнения, сравнение, стоимость,
        что не подойдёт), а товары берутся из прошлого топ-4.
        """
        reqs = dict(session.requirements or {})
        top4 = session.last_top4 or {"items": [], "needs_check": [],
                                     "verified_count": 0,
                                     "requirements": {}}
        answer = _compose_answer(session.last_query, top4, reqs, False, "",
                                 self.provider.name,
                                 getattr(self.provider, "model", ""),
                                 question_type)
        cards = (top4.get("items") or []) + (top4.get("needs_check") or [])
        session.messages.append({"role": "assistant", "content": answer})
        session.question_type = question_type
        elapsed = round((time.perf_counter() - started) * 1000)
        model_name = getattr(self.provider, "model", "")
        return {
            "answer": answer,
            "cards": cards,
            "top4": top4,
            "clarification": False,
            "provider": self.provider.name,
            "model": model_name,
            "live": False,
            "used_model": False,
            "fallback_reason": "",
            "session": session.id,
            "context": session.context_summary(),
            "query": session.last_query,
            "requirements": {key: reqs.get(key) for key in
                             ("os", "version", "arch", "license_type",
                              "wants_os", "role", "restore_scope",
                              "server_owned", "product_classes",
                              "platform_family")},
            "verified_count": top4.get("verified_count",
                                       len(top4.get("items") or [])),
            "needs_check_count": len(top4.get("needs_check") or []),
            "no_confirmed_message": top4.get("no_confirmed_message") or "",
            "calls": 0,
            "latency_ms": elapsed,
            "usage": dict(session.usage),
            "api_usage": {},
            "cost": None,
            "cost_rub": None,
            "question_type": question_type,
            "followup": True,
            "diagnostics": _diagnostics(self.provider.name, model_name,
                                        False, 0, elapsed,
                                        dict(session.usage), {}),
        }

    def _ask_tools(self, session: Session, message: str,
                   started: float) -> dict:
        """Старый путь YandexGPT: модель вызывает функции каталога."""
        os_name = _os_name(message)
        if os_name:
            session.requirements["os"] = os_name

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(session.messages[-12:])

        cards: list[dict] = []
        calls = 0
        answer = ""
        error = ""
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                if session.calls >= MAX_CALLS_PER_SESSION:
                    error = ("достигнут лимит обращений к модели в сессии "
                             f"({MAX_CALLS_PER_SESSION})")
                    break
                session.calls += 1
                calls += 1
                reply = self.provider.chat(messages, TOOLS)
                usage = reply.get("usage") or {}
                session.usage["prompt"] += int(usage.get("prompt") or 0)
                session.usage["completion"] += int(usage.get("completion") or 0)

                if not reply.get("tool_calls"):
                    answer = reply.get("content") or ""
                    break

                assistant = {"role": "assistant", "content": "",
                             "tool_calls": reply["tool_calls"]}
                messages.append(assistant)
                for call in reply["tool_calls"]:
                    function = call.get("function") or {}
                    name = function.get("name") or ""
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    payload = self._run_tool(name, args, session)
                    for card in payload.get("cards") or []:
                        if all(c["id"] != card["id"] for c in cards):
                            cards.append(card)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "content": json.dumps(payload, ensure_ascii=False),
                    })
        except ProviderError as exc:
            error = str(exc)

        if not answer:
            # Без ИИ показываем обычные результаты и понятное сообщение.
            fallback = self.catalog.search(message, 6,
                                           reqs=session.requirements or None)
            for card in fallback["cards"]:
                if all(c["id"] != card["id"] for c in cards):
                    cards.append(card)
            answer = ("ИИ-консультант сейчас недоступен"
                      + (f" ({error})" if error else "")
                      + ". Ниже — обычные результаты поиска по каталогу; "
                        "они получены локально и не зависят от ИИ.")

        session.messages.append({"role": "assistant", "content": answer})
        session.cards = cards

        elapsed = round((time.perf_counter() - started) * 1000)
        tokens = session.usage["prompt"] + session.usage["completion"]
        return {
            "answer": answer,
            "cards": cards,
            "clarification": bool(answer.rstrip().endswith("?") and not cards),
            "provider": self.provider.name,
            "live": self.provider.name == "yandexgpt",
            "used_model": self.provider.name == "yandexgpt",
            "fallback_reason": error,
            "session": session.id,
            "context": session.context_summary(),
            "calls": calls,
            "latency_ms": elapsed,
            "usage": dict(session.usage),
            "api_usage": {},
            "cost": None,
            "cost_rub": None,
        }


def _compose_answer(query: str, top4: dict, reqs: dict, used_model: bool,
                    fallback_reason: str, provider_name: str,
                    model: str,
                    question_type: str = "select") -> str:
    """Текст ответа без домыслов модели: факты только из индекса и правил.

    Начинает с полезного покупателю ответа; техническая информация о модели,
    токенах и времени работы передаётся отдельно в поле `diagnostics`.
    """
    items = top4.get("items") or []
    needs_check = top4.get("needs_check") or []
    no_confirmed = top4.get("no_confirmed_message") or ""
    shortfall = top4.get("shortfall") or False
    shortfall_reason = top4.get("shortfall_reason") or ""
    summary = ((top4.get("requirements") or {}).get("summary") or "")

    lines: list[str] = []

    # ── ответ покупателю (по типу вопроса) ──────────────────────
    if question_type == "cost":
        lines.extend(_cost_breakdown(items, needs_check))
    elif question_type == "unsuitable":
        lines.extend(_unsuitable_answer(top4, needs_check, items))
    elif question_type == "license":
        lines.extend(_license_answer(items))
    elif question_type == "extras":
        lines.extend(_extras_answer(items, needs_check))
    elif question_type == "compare":
        lines.extend(_compare_answer(items))
    elif question_type == "compat":
        lines.extend(_compat_answer(items, needs_check, no_confirmed, summary))
    else:
        lines.extend(_default_answer(query, top4, items, needs_check,
                                     no_confirmed, summary, shortfall,
                                     shortfall_reason))

    if not used_model:
        lines.append("(Ответ сформирован локально, без модели: показана "
                     "подборка по индексу каталога.)")

    return "\n".join(line for line in lines if line)


def _diagnostics(provider_name: str, model: str, used_model: bool, calls: int,
                 elapsed_ms: int, usage: dict, api_usage: dict) -> dict:
    """Технические сведения отдельно от ответа покупателю."""
    return {
        "provider": provider_name,
        "model": model,
        "used_model": bool(used_model),
        "calls": int(calls),
        "latency_ms": int(elapsed_ms),
        "tokens": {
            "prompt": int((usage or {}).get("prompt") or 0),
            "completion": int((usage or {}).get("completion") or 0),
            "total": int((usage or {}).get("prompt") or 0)
            + int((usage or {}).get("completion") or 0),
        },
        "api_total_tokens": int((api_usage or {}).get("total_tokens") or 0),
    }


# ── блоки ответа по типу вопроса ────────────────────────────────

def _default_answer(query: str, top4: dict, items: list, needs_check: list,
                    no_confirmed: str, summary: str,
                    shortfall: bool, shortfall_reason: str) -> list[str]:
    lines: list[str] = []
    if summary:
        lines.append(f"Обязательные условия: {summary}.")
    if no_confirmed:
        lines.append(no_confirmed + ".")
    if items:
        lines.append(f"Подтверждённых результатов: {len(items)}.")
        for index, item in enumerate(items, 1):
            lines.append(
                f"{index}. {item['name']} — {item['type_label']}, "
                f"{item['price_text']}, {item['available_text']} "
                f"({item['role']}).")
            if item.get("facts"):
                lines.append(f"   Основание: {'; '.join(item['facts'])}.")
            if item.get("limitation"):
                lines.append(f"   Ограничение: {item['limitation']}.")
            if item.get("problems"):
                lines.append(f"   Не подтверждено: {'; '.join(item['problems'])}.")
    elif not no_confirmed:
        lines.append(f"По запросу «{query}» в каталоге ничего не нашлось.")
    if needs_check:
        note = top4.get("needs_check_note") or "Совместимость требует проверки"
        lines.append(f"{note} (в подтверждённые результаты не входят):")
        for index, item in enumerate(needs_check, 1):
            lines.append(
                f"{index}. {item['name']} — {item['type_label']}, "
                f"{item['price_text']}, {item['available_text']}.")
            if item.get("limitation"):
                lines.append(f"   Ограничение: {item['limitation']}.")
            if item.get("problems"):
                lines.append(f"   Причина: {'; '.join(item['problems'])}.")
    if shortfall and items:
        lines.append(shortfall_reason)
    if top4.get("reserve_note"):
        lines.append(top4["reserve_note"])
    return lines


def _cost_breakdown(items: list, needs_check: list | None = None) -> list[str]:
    """Расчёт стоимости: один обязательный продукт + необязательные дополнения.

    Несколько прямых совпадений — это альтернативы одного и того же
    продукта (разные вендоры под одну задачу), а не компоненты комплекта.
    В итог берётся одна позиция, остальные показываются как варианты
    выбора: сумма альтернатив не выдаётся за стоимость решения.

    Если подтверждённого решения нет, окончательная стоимость не
    рассчитывается, а неподтверждённые позиции показываются как
    альтернативные варианты, требующие проверки совместимости.
    """
    lines: list[str] = []
    pending = list(needs_check or [])
    if not items:
        lines.append("Подтверждённого решения нет — окончательная стоимость "
                     "не рассчитана.")
        if pending:
            lines.append("Альтернативные варианты (требуют проверки "
                         "совместимости, в расчёт не входят):")
            for it in pending:
                period = it.get("duration") or ""
                period_text = f" ({period})" if period else ""
                price = (it.get("price_text") or "цена не указана")
                lines.append(f"- {it['name']}: {price} за 1 шт.{period_text}")
            lines.append("Стоимость можно рассчитать после подтверждения "
                         "совместимости с обязательными условиями.")
        return lines
    direct = [it for it in items if it.get("role") == "прямое совпадение"]
    optional = [it for it in items if it.get("role") != "прямое совпадение"]
    chosen, alternatives = (direct[:1], direct[1:]) if direct else ([], [])

    def price_line(it: dict) -> str:
        period = it.get("duration") or ""
        period_text = f" ({period})" if period else ""
        if (it.get("price") or 0) > 0:
            return f"- {it['name']}: {it['price_text']} за 1 шт.{period_text}"
        return f"- {it['name']}: цена не указана"

    if chosen:
        lines.append("Обязательный компонент (выбранный товар):")
        for it in chosen:
            lines.append(price_line(it))
        total = sum((it.get("price") or 0) for it in chosen)
        lines.append(f"Итого за обязательный компонент: {total:,.0f} ₽".replace(",", " "))
        if any((it.get("price") or 0) <= 0 for it in chosen):
            lines.append("Цена выбранной позиции не указана — уточните "
                         "количество и период у менеджера.")
        else:
            lines.append("Расчёт на 1 лицензию: уточните количество "
                         "пользователей или серверов и период.")
    else:
        lines.append("Подтверждённых прямых совпадений нет — "
                     "обязательный компонент не выбран.")

    if alternatives:
        lines.append("Альтернативы тому же компоненту (в расчёт не входят, "
                     "выбирается одна позиция):")
        for it in alternatives:
            lines.append(price_line(it))
    if optional:
        lines.append("Дополнительные (необязательные):")
        for it in optional:
            lines.append(price_line(it))
    return lines


def _unsuitable_answer(top4: dict, needs_check: list, items: list) -> list[str]:
    """Что не подойдёт и почему."""
    lines: list[str] = []
    rejected = top4.get("rejected") or []
    if not items and not needs_check and not rejected:
        lines.append("Нет товаров для анализа совместимости.")
        return lines
    if items:
        lines.append("Подходящие варианты:")
        for it in items:
            lines.append(f"- {it['name']}: {it['type_label']}, {it['price_text']}")
    if rejected:
        lines.append("Не подходят:")
        for it in rejected:
            problems = "; ".join(it.get("problems") or [])
            lines.append(f"- {it['name']}: {problems or 'не подходит'}")
    if needs_check:
        lines.append("Требуют проверки:")
        for it in needs_check:
            problems = "; ".join(it.get("problems") or [])
            lines.append(f"- {it['name']}: {problems or 'совместимость не подтверждена'}")
    return lines


def _license_answer(items: list) -> list[str]:
    """Какую лицензию выбрать."""
    lines: list[str] = []
    if not items:
        lines.append("Нет товаров для анализа.")
        return lines
    base = [it for it in items if it.get("offer_type") in ("base", "subscription")]
    support = [it for it in items if it.get("offer_type") == "support"]
    renewal = [it for it in items if it.get("offer_type") == "renewal"]
    if base:
        lines.append("Базовая лицензия или подписка (новая покупка):")
        for it in base:
            lines.append(f"- {it['name']}: {it['type_label']}, {it['price_text']}")
    if renewal:
        lines.append("Продление (нужно, если лицензия уже есть):")
        for it in renewal:
            lines.append(f"- {it['name']}: {it['type_label']}, {it['price_text']}")
    if support:
        lines.append("Сопровождение/техподдержка:")
        for it in support:
            lines.append(f"- {it['name']}: {it['type_label']}, {it['price_text']}")
    return lines


def _extras_answer(items: list, needs_check: list) -> list[str]:
    """Что ещё потребуется."""
    lines: list[str] = []
    if not items:
        lines.append("Нет подтверждённых товаров для подбора дополнений.")
        return lines
    related = [it for it in items if it.get("role") == "сопутствующий товар"]
    if related:
        lines.append("Сопутствующие товары:")
        for it in related:
            lines.append(f"- {it['name']}: {it['type_label']}, {it['price_text']}")
    if needs_check:
        lines.append("Требуют проверки:")
        for it in needs_check:
            lines.append(f"- {it['name']}: {', '.join(it.get('problems') or ['совместимость не подтверждена'])}")
    return lines


def _compare_answer(items: list) -> list[str]:
    """Сравнение вариантов."""
    lines: list[str] = []
    if not items:
        lines.append("Нет товаров для сравнения.")
        return lines
    lines.append("Сравнение вариантов:")
    for index, it in enumerate(items, 1):
        vendor = it.get("vendor") or "—"
        platform = it.get("platform") or "—"
        duration = it.get("duration") or "—"
        lines.append(
            f"{index}. {it['name']} ({vendor}) — {it['price_text']}, "
            f"тип: {it['type_label']}, платформа: {platform}, "
            f"период: {duration}")
    return lines


def _compat_answer(items: list, needs_check: list,
                   no_confirmed: str, summary: str) -> list[str]:
    """Совместимость: что совместимо, что нет."""
    lines: list[str] = []
    if summary:
        lines.append(f"Проверяем совместимость с: {summary}.")
    if items:
        lines.append(f"Совместимые ({len(items)}):")
        for it in items:
            facts = "; ".join(it.get("facts") or [])
            lines.append(f"- {it['name']}: {facts}")
    if needs_check:
        lines.append(f"Требуют проверки ({len(needs_check)}):")
        for it in needs_check:
            lines.append(f"- {it['name']}: {'; '.join(it.get('problems') or ['совместимость не подтверждена'])}")
    if no_confirmed:
        lines.append(no_confirmed)
    return lines

