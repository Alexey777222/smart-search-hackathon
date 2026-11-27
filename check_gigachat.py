"""check_gigachat.py — регрессия ИИ-консультанта GigaChat.

Проверяет две реплики одного диалога и обычный локальный поиск:
  1. «Нужен сервер».
  2. «Сервер уже есть, на нём Astra Linux 1.7, нужно резервное копирование».

Дополнительно — регрессия дефекта «брендовое сужение + Windows-only SKU»:
сохранение версии 1.7, исключение SKU только для Windows, отсутствие
дублей семейства, точная формулировка при отсутствии подтверждённых
совпадений, резервный ответ без модели и одинаковые правила подбора для
голосового и текстового ввода.

Ещё одна регрессия — автоисправление раскладки клавиатуры (ЙЦУКЕН ↔ QWERTY):
«cthdth» → «сервер», «cthdth Dell» → «сервер Dell», сохранение названий,
артикулов, версий и архитектур, «astra linuc» → «astra linux» и неизменность
нераспознанных слов. Проверяется и обычный поиск, и консультант.

Запуск без ключа (заглушка, локальная подборка):
  python check_gigachat.py

Живой тест (нужен ключ GIGACHAT_CREDENTIALS и сертификат НУЦ Минцифры):
  python check_gigachat.py --live

Скрипт печатает, сохранилась ли версия ОС, какие карточки вернулись,
сколько было вызовов модели и какие токены указал API. В режиме --live
хотя бы одна реплика в резервном режиме (без модели) — это FAIL.
Код возврата 1, если хотя бы одна проверка не прошла.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import organizer_search as osr
from ai_consultant import (Catalog, Consultant, GigaChatProvider,
                           MockProvider, ProviderError)
from selection import (NEEDS_CHECK_NOTE, build_top4, extract_requirements,
                       family_id, platform_conflict, sku_conflict)
from query_normalize import layout_note



class _FakePart:
    def __init__(self, text: str):
        self.text = text


class _FakeMessage:
    def __init__(self, content: str):
        self.content = [_FakePart(content)]


class _FakeUsage:
    input_tokens = 123
    output_tokens = 45
    total_tokens = 168


class _FakeReply:
    def __init__(self, content: str):
        self.messages = [_FakeMessage(content)]
        self.usage = _FakeUsage()


class _FakeChat:
    def __init__(self, content: str):
        self._content = content

    def create(self, payload):
        return _FakeReply(self._content)


class _FakeClient:
    """Заглушка SDK: тот же интерфейс, что у GigaChat."""

    def __init__(self, content: str):
        self.chat = _FakeChat(content)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _adapter_stub_checks() -> list[dict]:
    """Проверяет адаптер GigaChat без ключа: парсинг, валидацию, токены."""
    checks: list[dict] = []
    provider = GigaChatProvider()
    provider.credentials = "stub"
    provider.model = "GigaChat-2"

    provider._client = lambda: _FakeClient(json.dumps({
        "query": "кибер бэкап",
        "requirements": {"os": "Astra Linux", "version": ["1.7"],
                         "arch": None, "license_type": None,
                         "wants_os": False},
    }, ensure_ascii=False))
    result = provider.extract("Сервер уже есть, на нём Astra Linux 1.7", {})
    checks.append(_pass("адаптер: used_model=True", result["used_model"]))
    checks.append(_pass("адаптер: ОС канонизирована",
                        result["requirements"].get("os")
                        == "Astra Linux Special Edition"))
    checks.append(_pass("адаптер: версия 1.7 сохранена",
                        "1.7" in result["requirements"].get("version", [])))
    checks.append(_pass("адаптер: токены API разобраны",
                        result["api_usage"] == {"input_tokens": 123,
                                                "output_tokens": 45,
                                                "total_tokens": 168}))

    provider._client = lambda: _FakeClient("это не JSON")
    try:
        provider.extract("Нужен сервер", {})
        checks.append(_pass("адаптер: невалидный ответ отклонён", False))
    except ProviderError:
        checks.append(_pass("адаптер: невалидный ответ отклонён", True))
    return checks


def _pass(label: str, ok: bool) -> dict:
    return {"label": label, "ok": bool(ok)}


def _print_cards(cards: list[dict]) -> None:
    for index, card in enumerate(cards, 1):
        print(f"    {index}. [{card.get('id')}] {card.get('name')}")
        print(f"       тип: {card.get('type_label')} · цена: "
              f"{card.get('price_text')} · наличие: {card.get('available_text')}")
        print(f"       роль: {card.get('role')} · ссылка: {card.get('url')}")
        if card.get("facts"):
            print(f"       основание: {'; '.join(card['facts'])}")
        if card.get("problems"):
            print(f"       не подтверждено: {'; '.join(card['problems'])}")


def _windows_only(platform: str) -> bool:
    """Поле платформы явно описывает только Windows (без Linux-семейств)."""
    text = (platform or "").lower()
    has_windows = "windows" in text or "виндовс" in text
    has_linux = any(alias in text for alias in
                    ("linux", "астра", "astralinux", "unix", "redos",
                     "альт", "рос"))
    return has_windows and not has_linux


def _model_answer_checks(catalog: Catalog) -> list[dict]:
    """Поисковый запрос не зависит от формулировки, которую вернула модель.

    Проверяются три варианта ответа GigaChat на одну и ту же реплику:
    точное намерение, намерение с подмешанной ОС и брендовое сужение.
    Во всех случаях искать нужно по намерению «резервное копирование»,
    а явно названный пользователем бренд — сохраняться.
    """
    checks: list[dict] = []
    cases = [
        ("намерение как есть",
         "У меня уже есть сервер на Astra Linux 1.7. Нужно резервное "
         "копирование всего сервера",
         "резервное копирование",
         {"os": "Astra Linux", "version": ["1.7"]},
         "резервное копирование"),
        ("модель подмешала ОС в запрос",
         "У меня уже есть сервер на Astra Linux 1.7. Нужно резервное "
         "копирование всего сервера",
         "резервное копирование Astra Linux 1.7",
         {"os": "Astra Linux", "version": ["1.7"]},
         "резервное копирование"),
        ("модель сузила до бренда",
         "У меня уже есть сервер на Astra Linux 1.7. Нужно резервное "
         "копирование всего сервера",
         "кибер бэкап",
         {"os": "Astra Linux", "version": ["1.7"]},
         "резервное копирование"),
        ("бренд назван пользователем",
         "Нужен Кибер Бэкап", "кибер бэкап", {}, "кибер бэкап"),
    ]
    baseline: list[str] | None = None
    for label, message, model_query, requirements, expected in cases:
        provider = GigaChatProvider()
        provider.credentials = "stub"
        provider.model = "GigaChat-2"
        payload = json.dumps({"query": model_query,
                             "requirements": {
                                 "os": requirements.get("os"),
                                 "version": requirements.get("version", []),
                                 "arch": None, "license_type": None,
                                 "wants_os": False}}, ensure_ascii=False)
        provider._client = lambda: _FakeClient(payload)
        consultant = Consultant(catalog)
        consultant.provider = provider
        reply = consultant.ask(consultant.new_session().id, message)
        vendors = sorted({(card.get("vendor") or "").lower()
                          for card in reply["cards"]})
        print(f"  {label}: запрос модели «{model_query}» → «{reply['query']}» · "
              f"производители: {', '.join(vendors) or '—'}")
        checks.append(_pass(f"{label}: поисковый запрос «{expected}»",
                            reply["query"].lower() == expected))
        if "бренд назван" in label:
            continue
        families = {card.get("family_id") for card in reply["cards"]}
        checks.append(_pass(f"{label}: без дублей семейства",
                            len(families) == len(reply["cards"])))
        checks.append(_pass(f"{label}: выдача из разных семейств "
                            f"({len(vendors)} производителя)",
                            len(vendors) >= 2))
        if baseline is None:
            baseline = [card["id"] for card in reply["cards"]]
        else:
            checks.append(_pass(f"{label}: выдача совпадает с базовой",
                                [card["id"] for card in reply["cards"]]
                                == baseline))
    return checks


def _scenario_checks(second: dict, catalog: Catalog, message: str) -> list[dict]:
    """Регрессия дефекта: бренд-сужение, Windows-only SKU, дубли семейств.

    Проверяется фактический ответ консультанта: намерение «резервное
    копирование» сохранено, версия ОС 1.7 не потеряна, явные Windows-only
    SKU исключены, редакции одного продукта не дублируются, а при
    отсутствии подтверждённой совместимости звучит точная формулировка.
    """
    checks: list[dict] = []
    cards = second.get("cards") or []
    ids = [card.get("id") for card in cards]
    reqs = extract_requirements(message)
    label = "Astra Linux 1.7"
    expected = ("В каталоге не найдено предложений с подтверждённой "
                f"совместимостью с {label}")
    answer = second.get("answer") or ""

    # Намерение и версия ОС.
    checks.append(_pass("намерение «резервное копирование» сохранено",
                        "резерв" in second["query"].lower()
                        or "бэкап" in second["query"].lower()))
    checks.append(_pass("версия 1.7 сохранена в условиях",
                        "1.7" in [str(v) for v in
                                  (second["requirements"].get("version") or [])]))
    checks.append(_pass("поиск не сужен до одного бренда",
                        "кибер" not in second["query"].lower()))

    # Windows-only SKU не показываются при обязательной Astra Linux.
    win_only = [card for card in cards
                if _windows_only(card.get("platform"))]
    checks.append(_pass("Windows-only SKU исключены из выдачи",
                        not win_only))
    row = catalog.conn.execute(
        "SELECT name, platform FROM offers WHERE id = ?",
        ("499533",)).fetchone()
    if row:
        checks.append(_pass("SKU 499533 (Windows-only) не показан",
                            "499533" not in ids))
    checks.append(_pass("SKU 530606 (рабочая станция) не показан",
                        "530606" not in ids))
    checks.append(_pass("положительный тест: 530607 остаётся кандидатом",
                        "530607" in ids))
    checks.append(_pass("Windows-only SKU даёт конфликт, а не «неизвестно»",
                        bool(platform_conflict(
                            {"name": row[0] if row else "Кибер Бэкап",
                             "platform": "Windows"},
                            reqs))))

    # Семейства: одна позиция на продуктовое семейство.
    families = [card.get("family_id") or family_id(card) for card in cards]
    checks.append(_pass("нет дублей семейства в выдаче",
                        len(set(families)) == len(families)))
    vendors = {(card.get("vendor") or "").lower() for card in cards}
    # ТЗ: «ищи по категории и синонимам среди разных производителей».
    # Минимум два разных производителя — достаточное подтверждение, что поиск
    # не схлопнулся к одному бренду. Три и более — хорошо, но не обязательно.
    checks.append(_pass("выдача не схлопнута к одному производителю",
                        len(vendors) >= 2))
    checks.append(_pass("не более четырёх позиций", len(cards) <= 4))
    # Релевантность: аналоги должны быть из категории резервного копирования,
    # а не случайные товары, совпавшие одним словом запроса.
    relevant = [card for card in cards
                if any(mark in (card.get("category_path") or "").lower()
                       for mark in ("резервн", "восстановлен", "backup"))]
    checks.append(_pass("аналоги из категории резервного копирования",
                        len(relevant) == len(cards)))

    # Формулировка при отсутствии подтверждённых совпадений.
    checks.append(_pass("verified_count = 0", second["verified_count"] == 0))
    checks.append(_pass("неподтверждённые не в счётчике подтверждённых",
                        len(cards) == second["needs_check_count"]
                        and all(card.get("needs_check") for card in cards)))
    checks.append(_pass("точная формулировка no_confirmed",
                        second["no_confirmed_message"] == expected))
    checks.append(_pass("формулировка есть в тексте ответа", expected in answer))
    checks.append(_pass("нет ложного «Подтверждённых результатов»",
                        "Подтверждённых результатов" not in answer))
    checks.append(_pass("аналоги помечены «Совместимость требует проверки»",
                        NEEDS_CHECK_NOTE in answer
                        and all(card.get("verification_note") == NEEDS_CHECK_NOTE
                                for card in cards)))
    checks.append(_pass("выдача не добита до четырёх неподходящими",
                        len(cards) <= 4
                        and second["top4"].get("count") == 0))

    # Поисковый запрос не должен зависеть от формулировки модели.
    print("  варианты ответа модели:")
    model_checks = _model_answer_checks(catalog)
    checks.extend(model_checks)
    for check in model_checks:
        status = "[PASS]" if check["ok"] else "[FAIL]"
        print(f"    {status} {check['label']}")
    return checks


def _sku_limit_checks(catalog: Catalog) -> list[dict]:
    """Ограничение конкретного артикула: 530606/499533 не выдать серверу.

    Проверяется положительный и отрицательный случай в одном семействе:
    530606 («для рабочей станции Windows») отбраковывается, а 530607
    («для физического сервера») остаётся кандидатом для серверной задачи.
    """
    checks: list[dict] = []
    reqs = extract_requirements(
        "Нужно резервное копирование всего сервера на Astra Linux 1.7")
    rows = catalog.conn.execute(
        "SELECT id, name, platform FROM offers WHERE id IN ('530606','530607','499533')"
    ).fetchall()
    by_id = {row["id"]: dict(row) for row in rows}
    if "530606" in by_id:
        checks.append(_pass(
            "530606 (рабочая станция Windows) конфликтует с серверной задачей",
            bool(sku_conflict(by_id["530606"], reqs))))
    if "530607" in by_id:
        checks.append(_pass(
            "530607 (физический сервер) не конфликтует с серверной задачей",
            not sku_conflict(by_id["530607"], reqs)))
    if "499533" in by_id:
        checks.append(_pass(
            "499533 (Windows-only) конфликтует с Astra Linux",
            bool(sku_conflict(by_id["499533"], reqs))))
    return checks


def _clarification_checks(catalog: Catalog) -> list[dict]:
    """Один уточняющий вопрос и две ветки: весь сервер / объекты ALD Pro."""
    checks: list[dict] = []
    consultant = Consultant(catalog)

    session = consultant.new_session()
    first = consultant.ask(
        session.id,
        "Сервер уже есть, на нём Astra Linux 1.7, нужно резервное копирование")
    checks.append(_pass("уточняющий вопрос задан один раз",
                        first.get("clarification") is True))
    checks.append(_pass(
        "вопрос про объект восстановления",
        "весь сервер" in (first.get("answer") or "").lower()
        and "ald pro" in (first.get("answer") or "").lower()))
    reqs = first.get("requirements") or {}
    checks.append(_pass("ОС сохранена при уточнении",
                        (reqs.get("os") or "").startswith("Astra")))
    checks.append(_pass("версия 1.7 сохранена при уточнении",
                        "1.7" in [str(v) for v in reqs.get("version") or []]))

    second = consultant.ask(session.id, "весь сервер")
    checks.append(_pass("ветка «весь сервер»: поиск средства резервного "
                        "копирования",
                        "резерв" in (second.get("query") or "").lower()))
    checks.append(_pass("ветка «весь сервер»: объём восстановления сохранён",
                        (second.get("requirements") or {}).get("restore_scope")
                        == "full_server"))

    session2 = consultant.new_session()
    ask = consultant.ask(session2.id, "Нужно резервное копирование")
    branch = consultant.ask(session2.id, "объекты каталога ALD Pro")
    checks.append(_pass("ветка «ALD Pro»: объём восстановления — объекты "
                        "каталога",
                        (branch.get("requirements") or {}).get("restore_scope")
                        == "directory"))
    checks.append(_pass("ветка «ALD Pro»: показано средство восстановления "
                        "каталога",
                        any(card.get("role") == "прямое совпадение"
                            for card in branch.get("cards") or [])))
    checks.append(_pass("ветка «ALD Pro»: версия 1.7 не перенесена",
                        "1.7" not in [str(v) for v in
                                      (branch.get("requirements") or {})
                                      .get("version") or []]))
    return checks


def _input_parity_checks() -> list[dict]:
    """Голос и текст идут через один эндпоинт и одни правила подбора."""
    root = Path(__file__).resolve().parent
    web = (root / "webapp.py").read_text(encoding="utf-8")
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    return [
        _pass("один эндпоинт консультанта для текста и голоса",
              web.count('"/api/consult"') == 1),
        _pass("голосовой ввод отправляет через sendChat()",
              "rec.onresult" in html and "sendChat();" in html),
        _pass("страница вызывает /api/consult один раз (общий путь)",
              html.count('fetch("/api/consult"') == 1),
    ]


def _layout_checks(catalog: Catalog, consultant: Consultant,
                   live: bool) -> list[dict]:
    """Регрессия автоисправления раскладки: поиск и консультант."""
    conn = catalog.conn
    checks: list[dict] = []

    def groups_of(result: dict) -> list[str]:
        return [g["best"]["id"] for g in result["groups"]]

    def top4_of(result: dict) -> list[str]:
        top4 = build_top4(result, extract_requirements(
            result.get("normalized") or result.get("effective_query") or ""))
        return [item["id"] for item in top4["items"]]

    print("\n[6] Автоисправление раскладки ЙЦУКЕН ↔ QWERTY")

    # 1. «cthdth» даёт ту же выдачу, что «сервер».
    fixed, notes = osr.fix_layout(conn, "cthdth")
    print(f"  «cthdth» → «{fixed}» · {layout_note(notes)}")
    checks.append(_pass("раскладка: cthdth → сервер", fixed == "сервер"))
    checks.append(_pass("подпись: «Исправлена раскладка: cthdth → сервер»",
                        layout_note(notes)
                        == "Исправлена раскладка: cthdth → сервер"))
    typo = osr.search(conn, "cthdth", limit=12)
    server = osr.search(conn, "сервер", limit=12)
    print(f"  карточек: {len(typo['groups'])} · "
          f"первая: {typo['groups'][0]['name'] if typo['groups'] else '—'}")
    checks.append(_pass("cthdth: выдача совпадает с «сервер»",
                        groups_of(typo) == groups_of(server)
                        and top4_of(typo) == top4_of(server)))
    checks.append(_pass("cthdth: исходный ввод сохранён в ответе",
                        typo["query"] == "cthdth"
                        and typo["effective_query"] == "сервер"))
    checks.append(_pass("cthdth: исправление помечено в ответе",
                        bool(typo.get("layout_fixes"))))

    # 2. Смешанный запрос: «cthdth Dell» = «сервер Dell».
    mixed = osr.search(conn, "cthdth Dell", limit=12)
    mixed_ref = osr.search(conn, "сервер Dell", limit=12)
    print(f"  «cthdth Dell» → «{mixed['effective_query']}»")
    checks.append(_pass("cthdth Dell → «сервер Dell»",
                        mixed["effective_query"] == "сервер Dell"))
    checks.append(_pass("cthdth Dell: выдача как у «сервер Dell»",
                        groups_of(mixed) == groups_of(mixed_ref)
                        and top4_of(mixed) == top4_of(mixed_ref)))

    # 3. Распознанные названия, артикулы, версии и архитектуры сохраняются.
    for phrase in ("Astra Linux 1.7", "x86-64", "epcscnl-530606",
                   "КриптоПро CSP"):
        kept, keep_notes = osr.fix_layout(conn, phrase)
        checks.append(_pass(f"сохранено без правок: {phrase}",
                            kept == phrase and not keep_notes))

    # 4. Опечатка «astra linuc» исправляется как раньше.
    linuc = osr.search(conn, "astra linuc", limit=12)
    checks.append(_pass("astra linuc: раскладка не тронута",
                        not linuc.get("layout_fixes")))
    checks.append(_pass("astra linuc → astra linux",
                        any(c["to"] == "linux" for c in linuc["corrections"])))

    # 5. Нераспознанное слово без подтверждённого варианта остаётся как введено.
    for junk in ("qwfpgjl", "asdfgh", "zzzz"):
        kept, keep_notes = osr.fix_layout(conn, junk)
        checks.append(_pass(f"нераспознанное осталось: {junk}",
                            kept == junk and not keep_notes))

    # Консультант: та же нормализация до передачи запроса модели.
    session = consultant.new_session()
    fixed_answer = consultant.ask(session.id, "cthdth")
    print(f"  консультант: запрос «{fixed_answer['query']}» · "
          f"{layout_note(fixed_answer.get('layout_fixes') or [])}")
    checks.append(_pass("консультант: раскладка исправлена до вызова модели",
                        bool(fixed_answer.get("layout_fixes"))
                        and "сервер" in fixed_answer["query"].lower()))
    checks.append(_pass("консультант: подпись об исправленной раскладке",
                        "Исправлена раскладка: cthdth → сервер"
                        in fixed_answer["answer"]))
    checks.append(_pass("консультант: исходный ввод сохранён",
                        fixed_answer.get("original_message") == "cthdth"))
    if not live:
        plain_answer = consultant.ask(consultant.new_session().id, "сервер")
        checks.append(_pass("консультант: выдача как у «сервер»",
                            [c["id"] for c in fixed_answer["cards"]]
                            == [c["id"] for c in plain_answer["cards"]]))
    # «cthdth Dell»: проверяем, что в модель уходит уже исправленный текст,
    # а Dell остаётся в поисковом запросе.
    sent: list[str] = []
    original_extract = consultant.provider.extract

    def spy_extract(message: str, context: dict | None = None) -> dict:
        sent.append(message)
        return original_extract(message, context)

    consultant.provider.extract = spy_extract
    try:
        dell_answer = consultant.ask(consultant.new_session().id, "cthdth Dell")
    finally:
        consultant.provider.extract = original_extract
    model_input = sent[-1] if sent else ""
    print(f"  консультант: в модель ушло «{model_input}»")
    print(f"  консультант: поисковый запрос «{dell_answer['query']}»")
    checks.append(_pass("cthdth Dell: в модель ушло «сервер Dell», а не cthdth",
                        "cthdth" not in model_input.lower()
                        and "сервер" in model_input.lower()
                        and "dell" in model_input.lower()))
    checks.append(_pass("cthdth Dell: Dell сохранён в поисковом запросе",
                        "dell" in dell_answer["query"].lower()))
    checks.append(_pass("cthdth Dell: исправление помечено в ответе",
                        "Исправлена раскладка: cthdth → сервер"
                        in dell_answer["answer"]))
    checks.append(_pass("cthdth Dell: исходный ввод сохранён",
                        dell_answer.get("original_message") == "cthdth Dell"))
    return checks


def run(live: bool) -> int:
    catalog = Catalog()
    consultant = Consultant(catalog)
    if live:
        provider = GigaChatProvider()
        if not provider.ready:
            print("ОШИБКА: для --live нужен GIGACHAT_CREDENTIALS.")
            return 2
        consultant.provider = provider
    elif not isinstance(consultant.provider, MockProvider):
        consultant.provider = MockProvider("заглушка check_gigachat.py")

    print(f"Провайдер: {consultant.provider.name} · модель: "
          f"{getattr(consultant.provider, 'model', '')} · "
          f"live={live}")

    session = consultant.new_session()
    checks: list[dict] = []

    # ── реплика 1 ────────────────────────────────────────────────
    print("\n[1] «Нужен сервер»")
    first = consultant.ask(session.id, "Нужен сервер")
    print(f"  запрос: {first['query']}")
    print(f"  условия: {json.dumps(first['requirements'], ensure_ascii=False)}")
    print(f"  вызовов модели: {first['calls']} · токены API: "
          f"{json.dumps(first['api_usage'], ensure_ascii=False)} · "
          f"latency: {first['latency_ms']} мс")
    mode1 = ("живой ответ модели" if first["used_model"]
             else "РЕЗЕРВНЫЙ РЕЖИМ (без модели)")
    print(f"  режим: {mode1} (причина: {first['fallback_reason'] or '—'})")
    print(f"  карточек: {len(first['cards'])}")
    _print_cards(first["cards"])
    print("  ответ:")
    for line in first["answer"].splitlines()[:6]:
        print(f"    {line}")

    checks.append(_pass("реплика 1 вернула карточки", len(first["cards"]) > 0))
    checks.append(_pass("реплика 1: запрос про сервер",
                        "сервер" in first["query"].lower()))
    if live:
        checks.append(_pass("реплика 1: живой ответ модели (не резерв)",
                            first["used_model"]))
    else:
        checks.append(_pass("заглушка помечена ответ без модели",
                            not first["used_model"]
                            and "без модели" in first["answer"].lower()))

    # ── реплика 2 ────────────────────────────────────────────────
    message2 = ("Сервер уже есть, на нём Astra Linux 1.7, "
                "нужно резервное копирование всего сервера")
    print(f"\n[2] «{message2}»")
    second = consultant.ask(session.id, message2)
    reqs = second["requirements"]
    print(f"  запрос: {second['query']}")
    print(f"  условия: {json.dumps(reqs, ensure_ascii=False)}")
    print(f"  вызовов модели: {second['calls']} · токены API: "
          f"{json.dumps(second['api_usage'], ensure_ascii=False)} · "
          f"latency: {second['latency_ms']} мс")
    mode2 = ("живой ответ модели" if second["used_model"]
             else "РЕЗЕРВНЫЙ РЕЖИМ (без модели)")
    print(f"  режим: {mode2} (причина: {second['fallback_reason'] or '—'})")
    print(f"  карточек: {len(second['cards'])}")
    _print_cards(second["cards"])
    print("  ответ:")
    for line in second["answer"].splitlines()[:8]:
        print(f"    {line}")

    os_kept = bool(reqs.get("os")) and "astra" in reqs["os"].lower()
    version_kept = "1.7" in [str(v) for v in (reqs.get("version") or [])]
    checks.append(_pass("сохранилась ОС Astra Linux", os_kept))
    checks.append(_pass("сохранилась версия ОС 1.7", version_kept))
    checks.append(_pass("реплика 2 вернула карточки",
                        len(second["cards"]) > 0))
    checks.append(_pass("реплика 2: запрос про резервное копирование",
                        "бэкап" in second["query"].lower()
                        or "резерв" in second["query"].lower()))
    if live:
        checks.append(_pass("реплика 2: живой ответ модели (не резерв)",
                            second["used_model"]))
        model_ok = (second["model"]
                    == getattr(consultant.provider, "model", ""))
        checks.append(_pass("подпись: фактическая модель из ответа API",
                            model_ok))
        checks.append(_pass("стоимость только как расчётная или не показана",
                            second["cost"] is None
                            or second["cost"].get("estimated") is True))
    else:
        checks.append(_pass("реплика 2 помечена ответ без модели",
                            not second["used_model"]
                            and "без модели" in second["answer"].lower()))
    # Подпись под ответом: «Ответ без модели» для резерва, иначе
    # фактический провайдер и модель из данных ответа.
    signature = ("Ответ без модели" if not second["used_model"]
                 else f"провайдер: {second['provider']} · "
                      f"модель: {second['model'] or 'не указана'}")
    print(f"  подпись: {signature}")
    checks.append(_pass("подпись соответствует режиму ответа",
                        signature == "Ответ без модели"
                        if not second["used_model"]
                        else "модель:" in signature))
    if not second["used_model"]:
        checks.append(_pass("резерв: нет токенов API и стоимости",
                            not second["api_usage"]
                            and second["cost"] is None))
    else:
        checks.append(_pass("живой ответ: токены API фактически получены",
                            bool(second["api_usage"].get("total_tokens"))))

    # ── регрессия дефекта: Astra Linux 1.7 + резервное копирование ─
    print("\n[3] Регрессия: бренд-сужение, Windows-only, дубли семейств")
    scenario = _scenario_checks(second, catalog, message2)
    checks.extend(scenario)
    for check in scenario:
        status = "[PASS]" if check["ok"] else "[FAIL]"
        print(f"  {status} {check['label']}")

    # ── обычный поиск ────────────────────────────────────────────
    print("\n[4] Обычный локальный поиск «astra linuc»")
    result = osr.search(catalog.conn, "astra linuc", limit=12)
    top4 = build_top4(result, extract_requirements(
        result.get("normalized") or result.get("effective_query") or "astra linuc"))
    print(f"  effective_query: {result.get('effective_query')}")
    print(f"  исправления: {result.get('corrections')}")
    print(f"  топ-4: {top4['count']}")
    _print_cards(top4["items"])
    first_name = (top4["items"][0]["name"] if top4["items"] else "")
    checks.append(_pass("astra linuc исправлен до astra linux",
                        "astra" in (result.get("effective_query") or "").lower()
                        and bool(result.get("corrections"))))
    checks.append(_pass("astra linuc: топ-4 найден", top4["count"] == 4))
    checks.append(_pass("astra linuc: первый результат — Astra",
                        "astra" in first_name.lower()))

    # ── голос и текст: одинаковые правила подбора ────────────────
    print("\n[5] Голосовой и текстовый ввод — общий путь")
    parity = _input_parity_checks()
    checks.extend(parity)
    for check in parity:
        status = "[PASS]" if check["ok"] else "[FAIL]"
        print(f"  {status} {check['label']}")

    # ── автоисправление раскладки клавиатуры ─────────────────────
    layout_checks = _layout_checks(catalog, consultant, live)
    checks.extend(layout_checks)
    for check in layout_checks:
        status = "[PASS]" if check["ok"] else "[FAIL]"
        print(f"  {status} {check['label']}")

    # ── адаптер на заглушке SDK ──────────────────────────────────
    print("\n[7] Адаптер GigaChat с заглушкой SDK")
    adapter_checks = _adapter_stub_checks()
    checks.extend(adapter_checks)
    for check in adapter_checks:
        status = "[PASS]" if check["ok"] else "[FAIL]"
        print(f"  {status} {check['label']}")

    # ── ограничение конкретного артикула ─────────────────────────
    print("\n[8] Ограничение артикула: рабочая станция vs сервер")
    sku_checks = _sku_limit_checks(catalog)
    checks.extend(sku_checks)
    for check in sku_checks:
        status = "[PASS]" if check["ok"] else "[FAIL]"
        print(f"  {status} {check['label']}")

    # ── уточняющий вопрос и ветки восстановления ─────────────────
    print("\n[9] Уточняющий вопрос: весь сервер / объекты ALD Pro")
    clarify_checks = _clarification_checks(catalog)
    checks.extend(clarify_checks)
    for check in clarify_checks:
        status = "[PASS]" if check["ok"] else "[FAIL]"
        print(f"  {status} {check['label']}")

    # ── итог ─────────────────────────────────────────────────────
    print("\nПроверки:")
    failed = 0
    for check in checks:
        status = "[PASS]" if check["ok"] else "[FAIL]"
        print(f"  {status} {check['label']}")
        failed += 0 if check["ok"] else 1
    print(f"\nИтог: {'OK' if not failed else f'FAIL ({failed})'}")
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="живой вызов GigaChat (нужен GIGACHAT_CREDENTIALS)")
    args = parser.parse_args()
    if args.live:
        os.environ.setdefault("AI_PROVIDER", "gigachat")
    return run(args.live)


if __name__ == "__main__":
    sys.exit(main())
