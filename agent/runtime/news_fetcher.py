"""Fetch público (httpx) de 11 fuentes de noticias MX/internacionales."""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

NEWS_SOURCES: dict[str, dict[str, str]] = {
    "axisnegocios":  {"url": "https://www.axisnegocios.com/index.phtml",             "host": "axisnegocios.com",      "base_url": "https://www.axisnegocios.com",         "region": "mx",   "label": "Axis Negocios"},
    "elfinanciero":  {"url": "https://www.elfinanciero.com.mx/",                      "host": "elfinanciero.com.mx",   "base_url": "https://www.elfinanciero.com.mx",      "region": "mx",   "label": "El Financiero"},
    "heraldo":       {"url": "https://heraldodemexico.com.mx/",                       "host": "heraldodemexico.com.mx","base_url": "https://heraldodemexico.com.mx",       "region": "mx",   "label": "El Heraldo"},
    "eleconomista":  {"url": "https://www.eleconomista.com.mx/",                      "host": "eleconomista.com.mx",   "base_url": "https://www.eleconomista.com.mx",      "region": "mx",   "label": "El Economista"},
    "expansion":     {"url": "https://expansion.mx/",                                  "host": "expansion.mx",          "base_url": "https://expansion.mx",                 "region": "mx",   "label": "Expansión"},
    "jornada":       {"url": "https://www.jornada.com.mx/",                           "host": "jornada.com.mx",        "base_url": "https://www.jornada.com.mx",           "region": "mx",   "label": "La Jornada"},
    "excelsior":     {"url": "https://www.excelsior.com.mx/",                         "host": "excelsior.com.mx",      "base_url": "https://www.excelsior.com.mx",         "region": "mx",   "label": "Excélsior"},
    "reuters":       {"url": "https://www.reuters.com/",                              "host": "reuters.com",           "base_url": "https://www.reuters.com",              "region": "intl", "label": "Reuters"},
    "cnbc":          {"url": "https://www.cnbc.com/",                                 "host": "cnbc.com",              "base_url": "https://www.cnbc.com",                 "region": "intl", "label": "CNBC"},
    "bloomberglinea":{"url": "https://www.bloomberglinea.com/latinoamerica/mexico/",  "host": "bloomberglinea.com",    "base_url": "https://www.bloomberglinea.com",       "region": "mx",   "label": "Bloomberg Línea MX"},
    "cnn":           {"url": "https://edition.cnn.com/",                              "host": "cnn.com",               "base_url": "https://edition.cnn.com",              "region": "intl", "label": "CNN"},
}


def _headers() -> dict[str, str]:
    return {
        "User-Agent": os.getenv(
            "NEWS_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    }


async def _fetch_one(client: httpx.AsyncClient, source_id: str, cfg: dict[str, str]) -> dict[str, Any]:
    try:
        r = await client.get(cfg["url"], headers=_headers())
        return {
            "source_id": source_id,
            "label": cfg["label"],
            "region": cfg["region"],
            "ok": r.status_code < 400,
            "status_code": r.status_code,
            "html": r.text if r.status_code < 400 else "",
            "error": None if r.status_code < 400 else f"HTTP {r.status_code}",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "source_id": source_id,
            "label": cfg["label"],
            "region": cfg["region"],
            "ok": False,
            "status_code": 0,
            "html": "",
            "error": str(e),
        }


async def fetch_news_sources(
    *,
    timeout: float = 15.0,
    source_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Fetch paralelo. Si ``source_ids`` es None o vacío, usa las 11 fuentes.
    Si se pasan ids, solo esas claves válidas en ``NEWS_SOURCES``.
    """
    if source_ids:
        ids = [s.strip() for s in source_ids if s and s.strip() in NEWS_SOURCES]
    else:
        ids = list(NEWS_SOURCES.keys())
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        tasks = [_fetch_one(client, sid, NEWS_SOURCES[sid]) for sid in ids]
        results = await asyncio.gather(*tasks, return_exceptions=False)
    return {r["source_id"]: r for r in results}


async def fetch_all_news_sources(*, timeout: float = 15.0) -> dict[str, dict[str, Any]]:
    """Fetch en paralelo las 11 fuentes. Retorna dict[source_id, result]."""
    return await fetch_news_sources(timeout=timeout, source_ids=None)
