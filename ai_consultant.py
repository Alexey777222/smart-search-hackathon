"""ai_consultant.py — ИИ-консультант поверх локального индекса каталога.

Провайдер: YandexGPT Pro 5.1 через OpenAI-совместимый REST API Яндекса
(https://llm.api.cloud.yandex.net/v1, заголовок Authorization: Api-Key).
Ключ и folder_id берутся из окружения, имена переменных — в .env.example.

Модель получает ровно две функции только для чтения:
  * search_catalog(query, limit)  — поиск по локальному индексу;
  * get_related(offer_id, limit)  — соседние предложения с основанием.

Карточки товаров собираются на сервере из действительных ID индекса:
модель не может придумать цену, ссылку или предложение, которого нет.

Без ключа работает имитация провайдера: тот же протокол (system prompt,
tools, результаты функций), но решение принимает локальная логика.
Это позволяет показать диалог до подключения ключа; живой вызов ещё
предстоит проверить.
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
from organizer_index import OFFER_TYPE_LABELS, connect, get_offer, get_meta

DEFAULT_BASE_URL = "https://llm.api.cloud.yandex.net/v1"
DEFAULT_MODEL = "gpt://{folder}/yandexgpt/latest"
# Тариф YandexGPT Pro 5.1 (синхронный вызов), ₽ за 1000 токенов.
# Источник: открытые тарифы Yandex AI Studio; значение расчётное и
# подлежит сверке с договором.
PRICE_PER_1K_TOKENS_RUB = 0.8

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

    def search(self, query: str, limit: int = 6) -> dict:
        result = osr.search(self.conn, query, limit=max(limit, 4))
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

    def context_summary(self) -> dict:
        return {"intent": self.intent,
                "product": dict(self.product) if self.product else None,
                "requirements": dict(self.requirements)}


# ── провайдеры ─────────────────────────────────────────────────────

class ProviderError(Exception):
    pass


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
    Решение принимает локальная логика по тексту пользователя.
    """

    name = "mock"

    @property
    def ready(self) -> bool:
        return True

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


def _mock_query(text: str) -> tuple[str, str]:
    """Запрос к каталогу и, если нужно, один короткий вопрос."""
    low = (text or "").lower().replace("ё", "е")
    if "резервн" in low or "бэкап" in low or "backup" in low:
        return "кибер бэкап", ""
    if "сервер" in low and ("по" in low.split() or "программ" in low):
        return "ПО для сервера", ""
    if "сервер" in low:
        return "сервер", ""
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
    provider = YandexProvider()
    return provider if provider.ready else MockProvider()


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
            payload = self.catalog.search(query, limit)
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
        message = (message or "").strip()
        if not message:
            return {"answer": "Введите вопрос.", "cards": [],
                    "provider": self.provider.name, "session": session.id}

        os_name = _os_name(message)
        if os_name:
            session.requirements["os"] = os_name

        session.messages.append({"role": "user", "content": message})
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
            fallback = self.catalog.search(message, 6)
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
            "session": session.id,
            "context": session.context_summary(),
            "calls": calls,
            "latency_ms": elapsed,
            "usage": dict(session.usage),
            "cost_rub": round(tokens / 1000 * PRICE_PER_1K_TOKENS_RUB, 4),
        }
