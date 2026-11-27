"""webapp.py — минимальный веб-стенд поверх локального поиска.

Запуск:
  python -m uvicorn webapp:app --host 127.0.0.1 --port 8000

Индекс каталога открывается один раз на процесс. Обычный поиск работает
локально и не зависит от внешних сервисов. Кнопка «Помочь с выбором»
вызывает ИИ-консультанта (YandexGPT Pro 5.1) через серверный API; без
ключа работает имитация провайдера.

CLI и учебное демо (app.py, search_v3.py) не изменяются.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import organizer_search as osr
from ai_consultant import (Catalog, Consultant, GIGACHAT_PRICE_PER_1K_TOKENS_RUB,
                           MAX_CALLS_PER_SESSION, PRICE_PER_1K_TOKENS_RUB,
                           make_card)
from selection import build_top4, extract_requirements

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

app = FastAPI(title="Умный поиск Softline — стенд", version="1.0")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# Один экземпляр на процесс: индекс не перечитывается на каждый запрос.
# SQLite-соединение одно, поэтому доступ к нему сериализуется блокировкой.
CATALOG = Catalog()
CONSULTANT = Consultant(CATALOG)
DB_LOCK = threading.Lock()


def _git_commit() -> dict:
    """Коммит, из которого запущен стенд: чтобы сверять VDS с origin/main."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=5)
        sha = (out.stdout or "").strip()
        if not sha:
            return {"commit": None}
        when = subprocess.run(
            ["git", "log", "-1", "--format=%cI"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=5)
        return {"commit": sha, "commit_date": (when.stdout or "").strip()}
    except Exception:
        return {"commit": None}


BUILD_INFO = _git_commit()


def _cards_from_result(result: dict, limit: int = 12) -> dict:
    # Карточки собираются по секциям: иначе верхние группы с полным
    # покрытием вытеснили бы из ответа альтернативы и сопутствующие.
    buckets: dict[str, list] = {"direct": [], "for_os": [], "alternative": [],
                                "related": []}
    source = result.get("buckets") or {}
    for bucket in buckets:
        for group in (source.get(bucket) or [])[:limit]:
            card = make_card(group["best"], group["reason"], group["bucket"])
            card["group_name"] = group["name"]
            card["variant_count"] = group["variant_count"]
            card["variants"] = [
                {"id": v["id"], "name": v["name"], "type_label": v["type_label"],
                 "price": v["price"], "available": v["available"], "url": v["url"]}
                for v in group["variants"][:4]
            ]
            buckets.setdefault(group["bucket"], []).append(card)

    sections = None
    if result.get("sections"):
        sections = {}
        for kind, groups in result["sections"].items():
            sections[kind] = [
                {**make_card(g["best"], g["reason"], g["bucket"]),
                 "group_name": g["name"]}
                for g in groups
            ]
    return {"buckets": {k: v for k, v in buckets.items() if v},
            "bucket_titles": result.get("bucket_titles") or {},
            "sections": sections}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    stats = CATALOG.stats()
    provider = CONSULTANT.provider
    return {
        "ok": True,
        "index": stats,
        **BUILD_INFO,
        "ai_provider": provider.name,
        "ai_provider_requested": os.getenv("AI_PROVIDER", "").strip().lower(),
        "ai_model": getattr(provider, "model", ""),
        "ai_scope": getattr(provider, "scope", ""),
        "ai_live": provider.name in ("yandexgpt", "gigachat")
                   and bool(getattr(provider, "ready", False)),
        "ai_stub_reason": getattr(provider, "reason", ""),
        "ai_max_calls_per_session": MAX_CALLS_PER_SESSION,
        "price_per_1k_tokens_rub": (
            GIGACHAT_PRICE_PER_1K_TOKENS_RUB if provider.name == "gigachat"
            else PRICE_PER_1K_TOKENS_RUB),
    }


@app.get("/api/search")
def api_search(q: str = "", limit: int = 12):
    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="пустой запрос")
    limit = max(4, min(int(limit or 12), 24))
    with DB_LOCK:
        result = osr.search(CATALOG.conn, query, limit=limit)
    cards = _cards_from_result(result, limit)
    # Явный топ-4: обязательные условия извлекаются из исправленного
    # запроса, чтобы «astra linuc» проверялся как «astra linux».
    top4 = build_top4(
        result, extract_requirements(
            result.get("normalized") or result.get("effective_query") or query))
    return JSONResponse({
        "query": query,
        "effective_query": result.get("effective_query", query),
        "intent": result["intent"]["name"],
        "tokens": result["tokens"],
        "corrections": result.get("corrections") or [],
        "layout_fixes": result.get("layout_fixes") or [],
        "total": result["total"],
        "shortfall": result["shortfall"],
        "software_only": result.get("software_only", False),
        "candidates": result["candidates"],
        "top4": top4,
        **cards,
    })


class ConsultRequest(BaseModel):
    session_id: str = ""
    message: str = ""


@app.post("/api/session")
def api_session():
    session = CONSULTANT.new_session()
    provider = CONSULTANT.provider
    return {"session_id": session.id,
            "provider": provider.name,
            "model": getattr(provider, "model", ""),
            "live": provider.name in ("yandexgpt", "gigachat")
                    and bool(getattr(provider, "ready", False))}


@app.post("/api/consult")
def api_consult(request: ConsultRequest):
    message = (request.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="пустое сообщение")
    session_id = request.session_id or CONSULTANT.new_session().id
    with DB_LOCK:
        return JSONResponse(CONSULTANT.ask(session_id, message))


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port)
