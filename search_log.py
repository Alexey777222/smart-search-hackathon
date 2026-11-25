"""search_log.py — журнал пользовательских поисков (JSONL).

Каждый пользовательский поиск — одно событие, независимо от числа
внутренних проходов движка. Контрольные прогоны оценки (evaluate*.py)
вызывают search_v3 напрямую и в журнал НЕ пишут.

Формат записи (одна строка JSONL):
  ts            — метка времени
  raw           — исходный запрос пользователя
  norm          — нормализованный запрос
  ctx           — человекочитаемое описание контекста
  context       — {interest, viewed, source} — использованный контекст
  results       — показанные позиции:
                  {position, id, name, score, unconfirmed, gaps, name_confirmed}
  correction    — строка применённых исправлений (для совместимости)
  unconfirmed   — строка неподтверждённых атрибутов (для совместимости)
  transforms    — структурированное объяснение изменений запроса:
                  brand, brand_source, typo_fixes, abbreviations,
                  other_transforms, stopwords, rewritten
  requested     — явно запрошенные атрибуты

Старые записи (без transforms/context/gaps) читаются: отчёт использует
поля correction/unconfirmed как fallback.

Путь журнала можно переопределить переменной окружения SEARCH_JOURNAL_PATH
или параметром journal_path — это даёт демо писать во временный файл,
не трогая накопленный data/search_journal.jsonl.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_JOURNAL_PATH = DATA_DIR / "search_journal.jsonl"


def resolve_journal_path(journal_path=None) -> Path:
    """Фактический путь журнала: явный параметр → env → путь по умолчанию."""
    if journal_path:
        return Path(journal_path)
    env = os.environ.get("SEARCH_JOURNAL_PATH")
    if env:
        return Path(env)
    return DEFAULT_JOURNAL_PATH


def _fmt_transforms(tr: list[dict]) -> str:
    return "; ".join(f"{t['src']} -> {t['dst']}" for t in tr)


def _unconfirmed_line(results: list[dict]) -> str:
    """Строка неподтверждённых атрибутов по всем позициям (fallback-поле)."""
    attrs: list[str] = []
    for r in results:
        for gap in r.get("gaps", []):
            line = f"{gap['field'].lower()} не подтверждена"
            if line not in attrs:
                attrs.append(line)
        m = r.get("method", "")
        if "НЕ ПОДТВЕРЖДЕНО: " in m:
            part = m.split("НЕ ПОДТВЕРЖДЕНО: ")[1].rstrip("]")
            for attr in part.split("; "):
                attr = attr.strip()
                if attr and attr not in attrs:
                    attrs.append(attr)
    return "; ".join(attrs)


def _correction_line(analysis: dict) -> str:
    """Строка фактических исправлений (без распознавания бренда и no-op)."""
    parts = []
    for t in analysis.get("typo_fixes", []):
        parts.append(f"опечатка: {t['src']} -> {t['dst']}")
    for t in analysis.get("abbreviations", []):
        parts.append(f"сокращение: {t['src']} -> {t['dst']}")
    for t in analysis.get("other_transforms", []):
        kind = "термин" if t["kind"] == "term" else "транслит"
        parts.append(f"{kind}: {t['src']} -> {t['dst']}")
    return "; ".join(parts)


def record_search(
    raw_query: str,
    normalized_query: str,
    context: str = "",
    results: list[dict] | None = None,
    correction: str = "",
    unconfirmed: str = "",
    analysis: dict | None = None,
    context_info: dict | None = None,
    journal_path=None,
    commerce_info: dict | None = None,
    session_id: str = "",
    ts: str = "",
) -> Path:
    """Записывает ОДНО событие поиска. Возвращает путь файла журнала.

    session_id — идентификатор пользовательской сессии (пусто = нет сессии).
    ts — явная метка времени (ISO); если пусто — текущее время UTC.
    """
    results = results or []
    analysis = analysis or {}

    result_entries = []
    for pos, r in enumerate(results, 1):
        result_entries.append({
            "position": pos,
            "id": r["item"]["id"],
            "name": r["item"]["name"],
            "score": r["score"],
            "unconfirmed": _item_unconfirmed(r),
            "gaps": r.get("gaps", []),
            "name_confirmed": r.get("name_confirmed", True),
            "context_signals": r.get("context_signals", []),
            "commerce_note": r.get("commerce_note", ""),
            "metrics": r.get("metrics"),
        })

    entry = {
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "raw": raw_query,
        "norm": normalized_query,
        "ctx": context,
        "session_id": session_id,
        "context": context_info or {"interest": "", "viewed": [], "source": "none"},
        "results": result_entries,
        "correction": correction,
        "unconfirmed": unconfirmed,
        "transforms": {
            "brand": analysis.get("brand"),
            "brand_source": analysis.get("brand_source", "none"),
            "typo_fixes": analysis.get("typo_fixes", []),
            "abbreviations": analysis.get("abbreviations", []),
            "other_transforms": analysis.get("other_transforms", []),
            "stopwords": analysis.get("stopwords", []),
            "rewritten": analysis.get("rewritten", False),
        },
        "requested": analysis.get("requested", {}),
        "commerce": commerce_info or {"enabled": False},
    }

    path = resolve_journal_path(journal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def _item_unconfirmed(result: dict) -> str:
    """Неподтверждённые атрибуты одной позиции (из строки method)."""
    m = result.get("method", "")
    if "НЕ ПОДТВЕРЖДЕНО: " in m:
        return m.split("НЕ ПОДТВЕРЖДЕНО: ")[1].rstrip("]")
    return ""


def read_journal(journal_path=None) -> list[dict]:
    """Читает все события. Старые записи возвращаются как есть."""
    path = resolve_journal_path(journal_path)
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


# ── Журнал заказов ──────────────────────────────────────────────────

ORDERS_PATH = DATA_DIR / "orders_demo.jsonl"


def record_order(
    session_id: str,
    item_id: str,
    item_name: str = "",
    journal_path: str = "",
    ts: str = "",
) -> Path:
    """Записывает событие заказа. Возвращает путь файла журнала."""
    entry = {
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "item_id": item_id,
        "item_name": item_name,
        "source": "учебные данные",
    }
    path = Path(journal_path) if journal_path else ORDERS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def read_orders(journal_path: str | None = None) -> list[dict]:
    """Читает события заказов. Возвращает список записей."""
    path = Path(journal_path) if journal_path else ORDERS_PATH
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries
