"""check_gigachat.py — регрессия ИИ-консультанта GigaChat.

Проверяет две реплики одного диалога и обычный локальный поиск:
  1. «Нужен сервер».
  2. «Сервер уже есть, на нём Astra Linux 1.7, нужно резервное копирование».

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

import organizer_search as osr
from ai_consultant import (Catalog, Consultant, GigaChatProvider,
                           MockProvider, ProviderError)
from selection import build_top4, extract_requirements


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
                "нужно резервное копирование")
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
    else:
        checks.append(_pass("реплика 2 помечена ответ без модели",
                            not second["used_model"]
                            and "без модели" in second["answer"].lower()))

    # ── обычный поиск ────────────────────────────────────────────
    print("\n[3] Обычный локальный поиск «astra linuc»")
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

    # ── адаптер на заглушке SDK ──────────────────────────────────
    print("\n[4] Адаптер GigaChat с заглушкой SDK")
    adapter_checks = _adapter_stub_checks()
    checks.extend(adapter_checks)
    for check in adapter_checks:
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
