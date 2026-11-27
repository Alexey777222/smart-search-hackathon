"""check_acceptance.py — сквозной приёмочный тест через /api/consult.

Две независимые сессии, два запроса:
  А. «Нужно резервное копирование всего сервера на Astra Linux 1.7».
  Б. «Нужно восстанавливать отдельные объекты каталога ALD Pro».

Проверяется весь ответ, который видит покупатель: текст, карточки, роли
товаров и основания рекомендаций. Тест идёт через HTTP-эндпоинт веб-стенда
(FastAPI TestClient), а не напрямую через модули.

Запуск без ключа (локальные правила, заглушка провайдера):
  python check_acceptance.py

Живой прогон с GigaChat:
  $env:AI_PROVIDER="gigachat"
  $env:GIGACHAT_CREDENTIALS="<ключ>"
  python check_acceptance.py

Код возврата 1, если хотя бы одна проверка не прошла.
"""

from __future__ import annotations

import os
import sys

from fastapi.testclient import TestClient

import webapp

SCENARIO_A = "Нужно резервное копирование всего сервера на Astra Linux 1.7"
SCENARIO_B = "Нужно восстанавливать отдельные объекты каталога ALD Pro"
POSITIVE_PAIR_QUERY = "astra linux"

# Артикулы, которые не должны выдаваться за средство восстановления
# объектов каталога ALD Pro.
NON_RESTORE_IDS = {
    "502486": "лицензия на сам каталог ALD Pro",
    "527450": "просмотрщик Granulex Viewer",
    "527314": "сертификат техподдержки FreeIPA/ALD Pro",
    "527323": "сертификат техподдержки Premium",
}


def _pass(label: str, ok: bool) -> dict:
    return {"label": label, "ok": bool(ok)}


def _card_ids(cards: list[dict]) -> list[str]:
    return [str(card.get("id")) for card in cards]


def _basis(card: dict) -> str:
    parts: list[str] = []
    if card.get("facts"):
        parts.append("основание: " + "; ".join(card["facts"]))
    rule = card.get("rule") or {}
    if rule.get("doc"):
        title = rule.get("doc_title") or "документация вендора"
        parts.append(f"источник: {title} {rule['doc']}")
    if card.get("limitation"):
        parts.append("ограничение: " + card["limitation"])
    if card.get("problems"):
        parts.append("не подтверждено: " + "; ".join(card["problems"]))
    if not parts:
        parts.append(card.get("reason") or "причина не указана")
    return " | ".join(parts)


def _consult(client: TestClient, session_id: str, message: str) -> dict:
    response = client.post("/api/consult",
                           json={"session_id": session_id, "message": message})
    response.raise_for_status()
    return response.json()


def _all_cards(reply: dict) -> list[dict]:
    """Все карточки, которые видит покупатель: подтверждённые и требующие проверки.

    После JSON-сериализации одни и те же карточки приходят и в `cards`, и в
    `top4`, поэтому дубли убираются по артикулу, а не по идентичности объекта.
    """
    out: list[dict] = []
    seen: set[str] = set()
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


def _print_scenario(title: str, message: str, reply: dict) -> None:
    print("=" * 78)
    print(title, "|", message)
    print(f"  провайдер: {reply.get('provider')} · live: {reply.get('live')}")
    print(f"  запрос: {reply.get('query')}")
    print(f"  условия: {reply.get('requirements')}")
    print(f"  подтверждено: {reply.get('verified_count')} · "
          f"требует проверки: {reply.get('needs_check_count')}")
    print("  ответ:")
    for line in (reply.get("answer") or "").splitlines():
        print(f"    {line}")
    print("  карточки:")
    for card in _all_cards(reply):
        print(f"    - {card.get('id')} | {card.get('name')}")
        print(f"      роль: {card.get('role')} · тип: {card.get('type_label')} · "
              f"цена: {card.get('price_text')} · {card.get('available_text')}")
        print(f"      основание: {_basis(card)}")
    print()


def _check_scenario_a(reply: dict) -> list[dict]:
    checks: list[dict] = []
    cards = _all_cards(reply)
    ids = _card_ids(cards)
    answer = reply.get("answer") or ""

    checks.append(_pass("А: 530606 отсутствует во всех секциях",
                        "530606" not in ids and "530606" not in answer))
    checks.append(_pass("А: 499533 отсутствует во всех секциях",
                        "499533" not in ids and "499533" not in answer))

    # Прямое совпадение обязано иметь данные и о резервном копировании
    # всего сервера, и о поддержке требуемой ОС и версии.
    direct = [card for card in cards if card.get("role") == "прямое совпадение"]
    bad_direct = []
    for card in direct:
        facts = " ".join(card.get("facts") or []).lower()
        has_scope = "весь сервер" in facts or "всего сервера" in facts
        has_os = "astra linux" in facts
        has_version = "1.7" in facts or "версия" in facts
        if not (has_scope and has_os and has_version):
            bad_direct.append(card.get("id"))
    checks.append(_pass(
        "А: каждое прямое совпадение подтверждено объёмом и ОС/версией",
        not bad_direct))

    # Если показан 530607, у него должно быть ограничение по типу покупателя.
    card_530607 = next((c for c in cards if str(c.get("id")) == "530607"), None)
    if card_530607:
        limitation = (card_530607.get("limitation") or "").lower()
        checks.append(_pass(
            "А: 530607 помечен «для образовательных организаций»",
            "образовательн" in limitation))
        checks.append(_pass(
            "А: применимость 530607 требует уточнения типа покупателя",
            "уточн" in limitation))
    else:
        checks.append(_pass("А: 530607 либо не показан, либо с ограничением",
                            True))

    # Общая метка Linux/Windows не подтверждает Astra Linux 1.7.
    if card_530607:
        checks.append(_pass(
            "А: Linux/Windows не выдано за подтверждение Astra Linux 1.7",
            "совместимость" in " ".join(card_530607.get("problems") or []).lower()
            or "не подтвержд" in _basis(card_530607).lower()))
    return checks


def _check_scenario_b(reply: dict) -> list[dict]:
    checks: list[dict] = []
    cards = _all_cards(reply)
    ids = _card_ids(cards)
    answer = reply.get("answer") or ""
    reqs = reply.get("requirements") or {}

    checks.append(_pass("Б: объём восстановления — объекты каталога",
                        reqs.get("restore_scope") == "directory"))
    checks.append(_pass("Б: версия Astra Linux 1.7 не перенесена из А",
                        "1.7" not in [str(v) for v in
                                      (reqs.get("version") or [])]))

    granulex = [card for card in cards
                if "granulex" in (card.get("name") or "").lower()
                and "recovery" in (card.get("name") or "").lower()]
    checks.append(_pass("Б: показана лицензия Granulex Recovery", bool(granulex)))
    if granulex:
        basis = " ".join(_basis(card) for card in granulex).lower()
        checks.append(_pass(
            "Б: Granulex подтверждён полным описанием артикула "
            "(резервное копирование + объекты каталога)",
            "объект" in basis and "каталог" in basis
            and ("резервн" in basis or "восстановлен" in basis)))

    # Лицензия на каталог, просмотрщик и сертификат поддержки не должны
    # выдаваться за средство восстановления.
    bad = [oid for oid in NON_RESTORE_IDS if oid in ids]
    checks.append(_pass(
        "Б: лицензия на каталог, просмотрщик и сертификат поддержки "
        "не выданы за средство восстановления",
        not bad))
    checks.append(_pass(
        "Б: упоминание ALD Pro само по себе не считается восстановлением",
        all(not (str(card.get("id")) == "502486"
                 and card.get("role") == "прямое совпадение")
            for card in cards)))

    # Если версия ALD Pro не названа, это должно быть видно покупателю.
    checks.append(_pass(
        "Б: отмечено, что версию ALD Pro нужно уточнить",
        "ald pro" in answer.lower() and "уточн" in answer.lower()))
    return checks


def _check_positive_pair(reply: dict) -> list[dict]:
    """Реальная положительная пара «основной SKU + подтверждённая допродажа»."""
    checks: list[dict] = []
    cards = _all_cards(reply)
    direct = [card for card in cards if card.get("role") == "прямое совпадение"]
    related = [card for card in cards if card.get("role") == "сопутствующий товар"]
    checks.append(_pass("допродажа: есть основной SKU", bool(direct)))
    checks.append(_pass("допродажа: есть подтверждённая допродажа",
                        bool(related)))

    main = direct[0] if direct else None
    for upsell in related:
        label = f"допродажа {upsell.get('id')}"
        checks.append(_pass(f"{label}: у карточки есть основание из каталога",
                            bool(upsell.get("facts"))))
        checks.append(_pass(f"{label}: карточка не из списка «требует проверки»",
                            not upsell.get("needs_check")))
        checks.append(_pass(
            f"{label}: есть артикул, цена и ссылка",
            bool(upsell.get("id")) and bool(upsell.get("price_text"))
            and bool(upsell.get("url"))))
        if main:
            checks.append(_pass(
                f"{label}: понятно, к какому основному SKU относится",
                str(main.get("id")) != str(upsell.get("id"))))
        print(f"  положительная пара: основной SKU "
              f"{main.get('id') if main else '—'} "
              f"({main.get('name') if main else '—'}) → допродажа "
              f"{upsell.get('id')} ({upsell.get('name')})")
        print(f"    задача: {upsell.get('reason') or '—'}")
        print(f"    основание: {_basis(upsell)}")
    return checks


def _print_table(a_reply: dict, b_reply: dict) -> None:
    def rows(reply: dict) -> list[tuple[str, str, str]]:
        out = []
        for card in _all_cards(reply):
            out.append((f"{card.get('id')} · {card.get('name')}",
                        card.get("role") or "—",
                        _basis(card)))
        return out

    a_rows, b_rows = rows(a_reply), rows(b_reply)
    width = 46
    print("=" * 78)
    print("ИТОГОВАЯ ТАБЛИЦА")
    print(f"{'Сценарий А — SKU, роль, основание':<{width}} | "
          f"{'Сценарий Б — SKU, роль, основание'}")
    print("-" * 78)
    for index in range(max(len(a_rows), len(b_rows))):
        left = a_rows[index] if index < len(a_rows) else ("", "", "")
        right = b_rows[index] if index < len(b_rows) else ("", "", "")
        left_text = f"{left[0]} | {left[1]} | {left[2]}"
        right_text = f"{right[0]} | {right[1]} | {right[2]}"
        print(f"{left_text:<{width}} | {right_text}")
    print()


def _print_concrete_explanation(a_reply: dict, b_reply: dict) -> None:
    a_ids = {str(c.get("id")) for c in _all_cards(a_reply)}
    b_ids = {str(c.get("id")) for c in _all_cards(b_reply)}
    print("КОНКРЕТНЫЙ АРТИКУЛ")
    print("  530607 «Кибер Бэкап … для физического сервера»:")
    print(f"    - в сценарии А: {'показан' if '530607' in a_ids else 'не показан'}"
          " как кандидат для сервера, но совместимость с Astra Linux 1.7 "
          "не подтверждена и лицензия образовательная — нужен тип покупателя;")
    print(f"    - в сценарии Б: {'показан' if '530607' in b_ids else 'исключён'}"
          " — это не средство восстановления объектов каталога ALD Pro.")
    print()


def run() -> int:
    print(f"AI_PROVIDER={os.getenv('AI_PROVIDER', '') or 'по умолчанию'}")
    client = TestClient(webapp.app)

    session_a = client.post("/api/session").json()["session_id"]
    session_b = client.post("/api/session").json()["session_id"]
    print(f"независимые сессии: А={session_a} · Б={session_b}\n")

    reply_a = _consult(client, session_a, SCENARIO_A)
    reply_b = _consult(client, session_b, SCENARIO_B)

    _print_scenario("СЦЕНАРИЙ А", SCENARIO_A, reply_a)
    _print_scenario("СЦЕНАРИЙ Б", SCENARIO_B, reply_b)

    checks: list[dict] = []
    checks.extend(_check_scenario_a(reply_a))
    checks.extend(_check_scenario_b(reply_b))

    # Положительная пара из предыдущего задания: astra linux → допродажа.
    session_pair = client.post("/api/session").json()["session_id"]
    reply_pair = _consult(client, session_pair, POSITIVE_PAIR_QUERY)
    _print_scenario("ПОЛОЖИТЕЛЬНАЯ ПАРА", POSITIVE_PAIR_QUERY, reply_pair)
    checks.extend(_check_positive_pair(reply_pair))

    _print_table(reply_a, reply_b)
    _print_concrete_explanation(reply_a, reply_b)

    print("Проверки:")
    failed = 0
    for check in checks:
        status = "[PASS]" if check["ok"] else "[FAIL]"
        print(f"  {status} {check['label']}")
        failed += 0 if check["ok"] else 1
    print(f"\nИтог: {'OK' if not failed else f'FAIL ({failed})'}")
    return 0 if not failed else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
