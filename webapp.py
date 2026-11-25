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
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import organizer_search as osr
from ai_consultant import (Catalog, Consultant, MAX_CALLS_PER_SESSION,
                           PRICE_PER_1K_TOKENS_RUB, make_card)

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

app = FastAPI(title="Умный поиск Softline — стенд", version="1.0")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# Один экземпляр на процесс: индекс не перечитывается на каждый запрос.
# SQLite-соединение одно, поэтому доступ к нему сериализуется блокировкой.
CATALOG = Catalog()
CONSULTANT = Consultant(CATALOG)
DB_LOCK = threading.Lock()


def _cards_from_result(result: dict, limit: int = 12) -> dict:
    buckets: dict[str, list] = {"direct": [], "alternative": [], "related": []}
    for group in result["groups"][:limit]:
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
            "sections": sections}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    stats = CATALOG.stats()
    return {
        "ok": True,
        "index": stats,
        "ai_provider": CONSULTANT.provider.name,
        "ai_live": CONSULTANT.provider.name == "yandexgpt",
        "ai_max_calls_per_session": MAX_CALLS_PER_SESSION,
        "price_per_1k_tokens_rub": PRICE_PER_1K_TOKENS_RUB,
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
    return JSONResponse({
        "query": query,
        "intent": result["intent"]["name"],
        "tokens": result["tokens"],
        "total": result["total"],
        "shortfall": result["shortfall"],
        "software_only": result.get("software_only", False),
        "candidates": result["candidates"],
        **cards,
    })


class ConsultRequest(BaseModel):
    session_id: str = ""
    message: str = ""


@app.post("/api/session")
def api_session():
    session = CONSULTANT.new_session()
    return {"session_id": session.id,
            "provider": CONSULTANT.provider.name,
            "live": CONSULTANT.provider.name == "yandexgpt"}


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
