"""Локальный прототип умного поиска по каталогу ПО.

Запуск:  python search.py "текст запроса"

Два режима:
  baseline — точное совпадение после нормализации регистра/пробелов;
  smart    — точное совпадение -> раскрытие сокращений -> RapidFuzz
             с порогом и фильтром версий/редакций.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

from rapidfuzz import fuzz, process

import config

DATA_DIR = Path(__file__).resolve().parent / "data"
CATALOG_PATH = DATA_DIR / "catalog.csv"

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def normalize(text: str) -> str:
    """Нижний регистр, все последовательности пробелов -> один пробел."""
    return " ".join(text.lower().split())


def load_catalog(path: Path = CATALOG_PATH) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _tokens(text: str) -> list[str]:
    return normalize(text).split()


def _query_editions(query: str) -> set[str]:
    return {config.EDITION_TOKENS[t] for t in _tokens(query)
            if t in config.EDITION_TOKENS}


def _query_years(query: str) -> set[str]:
    return {t for t in _tokens(query) if _YEAR_RE.match(t)}


def _edition_ok(name: str, required: set[str]) -> bool:
    """False, если запрос явно назвал редакцию, а товар ей не соответствует."""
    if not required:
        return True
    return bool(required & _query_editions(name))


def _year_ok(name: str, required: set[str]) -> bool:
    """False, если запрос явно назвал версию (год), а у товара другой год."""
    if not required:
        return True
    years = _query_years(name)
    return not years or bool(years & required)


def expand_abbreviations(query: str) -> tuple[str, list[str]]:
    """Заменяет известные сокращения; возвращает (новый запрос, список замен)."""
    replaced = []
    out = []
    for tok in _tokens(query):
        repl = config.ABBREVIATIONS.get(tok)
        if repl:
            out.append(repl)
            replaced.append(f"{tok} -> {repl}")
        else:
            out.append(tok)
    return " ".join(out), replaced


def baseline_search(query: str, catalog: list[dict]) -> list[dict]:
    nq = normalize(query)
    hits = [item for item in catalog if normalize(item["name"]) == nq]
    return [{"item": h, "method": "точное совпадение", "score": None}
            for h in hits]


def smart_search(query: str, catalog: list[dict]) -> list[dict]:
    nq = normalize(query)

    # 1. Точное совпадение.
    exact = [item for item in catalog if normalize(item["name"]) == nq]
    if exact:
        return _as_results(exact, "точное совпадение")

    # 2. Раскрытие сокращений и повторный поиск точного совпадения.
    expanded, replaced = expand_abbreviations(nq)
    if expanded != nq:
        exact = [item for item in catalog
                 if normalize(item["name"]) == expanded]
        if exact:
            return _as_results(
                exact, "точное совпадение после раскрытия сокращений")

    # 3. RapidFuzz по кандидатам, прошедшим фильтр версий/редакций.
    req_editions = _query_editions(expanded)
    req_years = _query_years(expanded)
    candidates = [item for item in catalog
                  if _edition_ok(item["name"], req_editions)
                  and _year_ok(item["name"], req_years)]

    if not candidates:
        return []

    names = [item["name"] for item in candidates]
    scored = process.extract(
        expanded if replaced else nq, names,
        scorer=fuzz.WRatio, limit=len(names))

    results = []
    for name, score, idx in scored:
        if score < config.THRESHOLD:
            break
        results.append(_as_results([candidates[idx]],
                                   "нечёткое совпадение (RapidFuzz)", score)[0])
        if len(results) == 5:
            break

    if replaced and results:
        for r in results:
            r["method"] += ", раскрыты сокращения: " + ", ".join(replaced)
    return results


def _as_results(items: list[dict], method: str,
                score: float | None = None) -> list[dict]:
    return [{"item": it, "method": method, "score": score} for it in items]


def print_results(title: str, results: list[dict]) -> None:
    print(f"\n--- {title} ---")
    if not results:
        print("  ничего не найдено")
        return
    for pos, r in enumerate(results, 1):
        score = (f", мера сходства: {r['score']:.0f}/100"
                 if r["score"] is not None else "")
        print(f"  {pos}. {r['item']['name']} [ID: {r['item']['id']}] "
              f"— {r['method']}{score}")
    if any(r["score"] is not None for r in results):
        print("  (мера сходства RapidFuzz — похожесть строк, а не уверенность в выборе)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Умный поиск по каталогу ПО (baseline и smart)")
    parser.add_argument("query", help="текст запроса")
    args = parser.parse_args(argv)

    catalog = load_catalog()
    print(f"Запрос: {args.query}")
    print_results("baseline", baseline_search(args.query, catalog))
    print_results("smart", smart_search(args.query, catalog))
    return 0


if __name__ == "__main__":
    sys.exit(main())
