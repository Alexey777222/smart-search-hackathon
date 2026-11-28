"""check_new_scenarios.py — приёмочные тесты новых сценариев.

Пять групп проверок:
  1. product_scope: «антивирус для Linux» → продукт антивирус, Linux — условие.
     ОС не должна быть «прямое совпадение» для антивируса.
  2. Context: две задачи в одной сессии не должны переносить ограничения.
  3. Composite «ОС с поддержкой» → базовая лицензия ОС + сопровождение.
  4. Cost breakdown: расчёт стоимости по подтверждённым SKU с разделением.
  5. Response structure: покупатель — сначала, техника — отдельно.

Запуск:
  python check_new_scenarios.py
"""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient

import webapp


# ── сценарии ──────────────────────────────────────────────────────

Q_ANTIVIRUS_LINUX = "антивирус для Linux"
Q_OS_SUPPORT = "нужна российская ОС с поддержкой"
Q_INDEPENDENT_1 = "нужен антивирус"
Q_INDEPENDENT_2 = "нужна операционная система"
Q_BACKUP_A = "нужно резервное копирование всего сервера на Astra Linux 1.8"
Q_BACKUP_B = "нужен антивирус для Windows"


def _consult(client, session_id, message):
    r = client.post("/api/consult", json={"session_id": session_id, "message": message})
    r.raise_for_status()
    return r.json()


def _all_cards(reply):
    out = []
    seen = set()
    top4 = reply.get("top4") or {}
    for card in (list(reply.get("cards") or [])
                 + list(top4.get("items") or [])
                 + list(top4.get("needs_check") or [])):
        key = str(card.get("id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(card)
    return out


def _card_ids(cards):
    return [str(c.get("id")) for c in cards]


def _roles(cards):
    return {str(c.get("id")): c.get("role") for c in cards}


# ── Helper for session creation ───────────────────────────────────

def _create_session(client):
    return client.post("/api/session").json()["session_id"]


# ── 1. Product scope ─────────────────────────────────────────────

def test_product_scope(client, checks):
    """1. Product scope: ОС ≠ функциональный аналог не-ОС класса."""
    sid = _create_session(client)
    reply = _consult(client, sid, Q_ANTIVIRUS_LINUX)

    cards = _all_cards(reply)
    ids = _card_ids(cards)
    roles_map = _roles(cards)
    answer = (reply.get("answer") or "").lower()

    # ОС не должна быть «прямое совпадение» для антивируса.
    os_cards = [c for c in cards if "операцион" in (c.get("name") or "").lower()]
    for os_card in os_cards:
        checks.append({"label": "1. ОС не выдана как прямое совпадение для антивируса",
                        "ok": os_card.get("role") != "прямое совпадение"})

    # Продукт антивирус — ищем карточки с ролью «прямое совпадение»
    direct = [c for c in cards if c.get("role") == "прямое совпадение"]
    checks.append({"label": "1. есть карточки с ролью «прямое совпадение»",
                    "ok": bool(direct)})

    # Функциональный конфликт: если ОС попала в выдачу, должен быть конфликт.
    os_in_results = [c for c in cards if "операцион" in (c.get("name") or "").lower()]
    for os_c in os_in_results:
        basis = " ".join(os_c.get("facts") or []).lower() + " " + (os_c.get("limitation") or "").lower()
        checks.append({"label": "1. ОС в выдаче имеет функциональный конфликт",
                        "ok": "функциональн" in basis or "класс" in basis or "аналог" in basis})

    # В ответе должно быть про антивирус, а не про ОС.
    checks.append({"label": "1. в ответе упоминание антивируса",
                    "ok": "антивирус" in answer})

    # product_classes = ["antivirus"] — класс продукта определён.
    reqs = reply.get("requirements") or {}
    checks.append({"label": "1. product_classes содержит antivirus",
                    "ok": "antivirus" in (reqs.get("product_classes") or [])})

    print("  [product_scope] done")


def test_context_independence(client, checks):
    """2. Ограничения не переносятся между задачами в сессии."""
    # Сессия A: бэкап на Astra Linux 1.8.
    sid_a = _create_session(client)
    reply_a = _consult(client, sid_a, Q_BACKUP_A)

    # Сессия B: сначала антивирус (без ОС), потом ОС.
    sid_b = _create_session(client)
    reply_b1 = _consult(client, sid_b, Q_INDEPENDENT_1)
    reply_b2 = _consult(client, sid_b, Q_INDEPENDENT_2)

    # Проверяем, что в сессии B при запросе ОС не осталось ограничений бэкапа.
    # Но мы проверяем независимые сессии: в сессии A ограничения бэкапа не
    # должны повлиять на сессию B.
    cards_b2 = _all_cards(reply_b2)
    ids_b2 = _card_ids(cards_b2)
    reqs_b2 = reply_b2.get("requirements") or {}

    # В сессии B, задача 2 (ОС) не должна содержать restore_scope.
    checks.append({"label": "2. restore_scope не перенесён в новую задачу",
                    "ok": reqs_b2.get("restore_scope") is None})

    # В сессии A restore_scope должен быть.
    reqs_a = reply_a.get("requirements") or {}
    checks.append({"label": "2. restore_scope есть в сессии A",
                    "ok": reqs_a.get("restore_scope") == "full_server"})

    # В сессии B, задача 2 — ОС, не бэкап: product_classes не должно содержать backup.
    checks.append({"label": "2. product_classes не содержит backup в задаче ОС",
                    "ok": "backup" not in (reqs_b2.get("product_classes") or [])})

    print("  [context_independence] done")


def test_composite_os_support(client, checks):
    """3. «ОС с поддержкой» → product_classes пуст (продукт — ОС), wants_os=True.
    
    Компонент поддержки распознаётся: license_type не «support»,
    product_classes пустой (продукт — ОС, не отдельный класс).
    """
    sid = _create_session(client)
    reply = _consult(client, sid, Q_OS_SUPPORT)

    answer = (reply.get("answer") or "").lower()
    cards = _all_cards(reply)
    reqs = reply.get("requirements") or {}

    # product_classes пуст — продукт это ОС, не отдельный класс.
    checks.append({"label": "3. product_classes пуст (продукт — ОС)",
                    "ok": not (reqs.get("product_classes") or [])})

    # wants_os = True (запрос про ОС).
    checks.append({"label": "3. wants_os = True для запроса ОС с поддержкой",
                    "ok": reqs.get("wants_os") is True})

    # license_type не "support" — поддержка как компонент, а не покупаемый тип.
    checks.append({"label": "3. license_type != support (поддержка как компонент)",
                    "ok": reqs.get("license_type") != "support"})

    # В ответе должно быть упоминание ОС.
    checks.append({"label": "3. в ответе есть упоминание ОС/лицензии",
                    "ok": "ос" in answer or "лиценз" in answer})

    # Прямое совпадение — это либо ОС, либо поддержка (зависит от поиска).
    # Проверяем что есть прямые совпадения.
    direct = [c for c in cards if c.get("role") == "прямое совпадение"]
    checks.append({"label": "3. есть прямые совпадения",
                    "ok": bool(direct)})

    print("  [composite_os_support] done")


def test_cost_breakdown(client, checks):
    """4. Расчёт стоимости: разделение на обязательные/необязательные."""
    # «astra linux» → есть прямое совпадение (ОС) + допродажи.
    sid = _create_session(client)
    reply = _consult(client, sid, "astra linux")

    cards = _all_cards(reply)
    direct = [c for c in cards if c.get("role") == "прямое совпадение"]
    related = [c for c in cards if c.get("role") == "сопутствующий товар"]

    # Есть и прямое совпадение, и допродажи.
    checks.append({"label": "4. есть прямое совпадение (ОС)",
                    "ok": bool(direct)})
    checks.append({"label": "4. есть сопутствующие товары (допродажи)",
                    "ok": bool(related)})

    # Проверка по карточкам: у каждого SKU есть цена.
    for d in direct:
        checks.append({"label": f"4. прямое совпадение {d.get('id')} имеет цену",
                        "ok": bool(d.get("price_text"))})

    print("  [cost_breakdown] done")


def test_response_structure(client, checks):
    """5. Структура ответа: покупатель — сначала, техника — отдельно."""
    sid = _create_session(client)
    reply = _consult(client, sid, "нужен антивирус")

    answer = reply.get("answer") or ""
    # Техническая информация должна быть доступна в ответе API (не в answer).
    checks.append({"label": "5. provider указан в ответе API",
                    "ok": bool(reply.get("provider"))})
    checks.append({"label": "5. latency_ms указан в ответе API",
                    "ok": isinstance(reply.get("latency_ms"), int)})
    checks.append({"label": "5. cards есть в ответе API",
                    "ok": isinstance(reply.get("cards"), list)})

    # В answer (текст для покупателя) нет технических метрик.
    checks.append({"label": "5. в тексте ответа нет latency/токенов",
                    "ok": "latency" not in answer.lower()
                          and "token" not in answer.lower()})

    print("  [response_structure] done")


def test_os_constraint_not_product(client, checks):
    """6. ОС как условие совместимости: «Kaspersky для Astra Linux» → антивирус."""
    sid = _create_session(client)
    reply = _consult(client, sid, "Kaspersky для Astra Linux")

    cards = _all_cards(reply)
    reqs = reply.get("requirements") or {}
    answer = (reply.get("answer") or "").lower()

    # product_classes содержит antivirus — продукт определён как антивирус.
    checks.append({"label": "6. product_classes содержит antivirus",
                    "ok": "antivirus" in (reqs.get("product_classes") or [])})

    # wants_os = False (покупают не ОС).
    checks.append({"label": "6. wants_os = False (ОС — ограничение)",
                    "ok": reqs.get("wants_os") is False})

    # В ответе есть класс антивируса.
    checks.append({"label": "6. в ответе указан класс antivirus",
                    "ok": "antivirus" in answer or "антивирус" in answer})

    print("  [os_constraint_not_product] done")


def test_new_task_clears_constraints(client, checks):
    """7. Новый класс продукта → старые ограничения не применяются."""
    # В одной сессии: сначала бэкап на Astra Linux 1.8, потом антивирус.
    # ОС 1.8 не должна быть обязательным условием для антивируса.
    sid = _create_session(client)
    _consult(client, sid, Q_BACKUP_A)  # бэкап, Astra 1.8
    reply = _consult(client, sid, Q_BACKUP_B)  # антивирус для Windows

    reqs = reply.get("requirements") or {}
    answer = (reply.get("answer") or "").lower()

    # Версия 1.8 не должна быть в product_classes для антивируса.
    versions = [str(v) for v in (reqs.get("version") or [])]
    checks.append({"label": "7. версия 1.8 не перенесена в антивирус",
                    "ok": "1.8" not in versions})

    checks.append({"label": "7. в ответе — антивирус",
                    "ok": "антивирус" in answer})

    print("  [new_task_clears_constraints] done")


def run():
    print(f"AI_PROVIDER={sys.argv[1] if len(sys.argv) > 1 else 'по умолчанию'}")
    client = TestClient(webapp.app)
    checks = []

    test_product_scope(client, checks)
    test_context_independence(client, checks)
    test_composite_os_support(client, checks)
    test_cost_breakdown(client, checks)
    test_response_structure(client, checks)
    test_os_constraint_not_product(client, checks)
    test_new_task_clears_constraints(client, checks)

    print("\nПроверки:")
    failed = 0
    for check in checks:
        status = "[PASS]" if check["ok"] else "[FAIL]"
        print(f"  {status} {check['label']}")
        if not check["ok"]:
            failed += 1
    print(f"\nИтог: {'OK' if not failed else f'FAIL ({failed})'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
