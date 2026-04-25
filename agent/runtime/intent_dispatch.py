"""Detección de intención del chat (keywords, sin LLM). Usado por ``runtime.app``."""
from __future__ import annotations

import os
import re
import unicodedata

_MORNING_SHOT_KW = re.compile(
    r"\b(morning[\s\-]?shot|mshot|disparo)\b"
    r"|\bmorning\b"
    r"|\bshot\b",
    re.I,
)
_NEWS_KW = re.compile(
    r"\b(resumen|noticias|news|headlines?|summary)\b", re.I
)
_WSJ_KW = re.compile(r"\bwsj\b|wall street journal", re.I)
_NYT_KW = re.compile(r"\bnyt\b|new york times|nytimes", re.I)
_QUOTE_KW = re.compile(
    r"(?:cotizaci[oó]n|precio|quote|ticker)\s+(?:de\s+)?([A-Za-z\.\^]{1,10})", re.I
)
_CHART_LINE = re.compile(
    r"\b(gráfic[oa]|grafic[oa]|chart|historial\s+de\s+precios?|evoluci[oó]n(?:\s+del\s+precio)?|curva\s+de\s+precio|desempe[nñ]o)\b"
    r"|(?:necesito|quiero|quieres|puedo)\s+ver\s+(?:un\s+|el\s+|la\s+)?(?:gráfic[oa]|grafic[oa]|chart)\b"
    r"|(?:muestr(?:a|ame)|ens[eé]n(?:a|ame)|dame|trae(?:me)?)\s+(?:por\s+favor\s+)?(?:el\s+|la\s+)?(?:gráfic[oa]|grafic[oa]|chart)\b",
    re.I,
)
_ASSET_DETAIL_LINE = re.compile(
    r"\b(detalles?|informaci[oó]n|info|ficha)\s+(?:de|del|sobre)\b"
    r"|\b(?:c[oó]mo\s+va|a\s+c[oó]mo\s+est[aá])\b",
    re.I,
)

_NOT_ASSET_TICKERS = frozenset(
    {
        "EL",
        "LA",
        "LOS",
        "LAS",
        "UN",
        "UNA",
        "DE",
        "DEL",
        "AL",
        "Y",
        "O",
        "EN",
        "CON",
        "POR",
        "PARA",
        "QUE",
        "COMO",
        "CÓMO",
        "MI",
        "TU",
        "SU",
        "LO",
        "LE",
        "DA",
        "DO",
        "SE",
        "ES",
        "TE",
        "ME",
        "MERCADO",
        "MERCADOS",
        "BOLSA",
        "ACCIONES",
        "ACTIVOS",
        "NOTICIAS",
        "PRECIOS",
        "DÍA",
        "DIA",
        "HOY",
    }
)


def _clean_asset_ticker(raw: str) -> str | None:
    t = raw.strip().upper()
    if not t or len(t) > 10 or not re.match(r"^[A-Z][A-Z0-9.\^]*$", t):
        return None
    if t in _NOT_ASSET_TICKERS:
        return None
    return t


def _normalize_text_unaccent(s: str) -> str:
    nk = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nk if not unicodedata.combining(c))


_NEWS_SOURCE_PHRASES: list[tuple[str, str]] = sorted(
    [
        ("bloomberg linea", "bloomberglinea"),
        ("bloomberglinea", "bloomberglinea"),
        ("axis negocios", "axisnegocios"),
        ("el financiero", "elfinanciero"),
        ("el economista", "eleconomista"),
        ("la jornada", "jornada"),
        ("heraldo de mexico", "heraldo"),
        ("heraldo", "heraldo"),
        ("excelsior", "excelsior"),
        ("eleconomista", "eleconomista"),
        ("elfinanciero", "elfinanciero"),
        ("axisnegocios", "axisnegocios"),
        ("expansion", "expansion"),
        ("reuters", "reuters"),
        ("financiero", "elfinanciero"),
        ("economista", "eleconomista"),
        ("jornada", "jornada"),
        ("axis", "axisnegocios"),
        ("cnbc", "cnbc"),
        ("cnn", "cnn"),
    ],
    key=lambda x: -len(x[0]),
)


def _parse_news_source_ids_from_message(text: str) -> list[str] | None:
    """Si el usuario nombra uno o más medios, devuelve sus ids; si no, None (todas las fuentes)."""
    n = _normalize_text_unaccent(text).lower()
    n = re.sub(r"\s+", " ", n).strip()
    found: list[str] = []
    for phrase, sid in _NEWS_SOURCE_PHRASES:
        if phrase in n and sid not in found:
            found.append(sid)
    return found if found else None


def _parse_chart_period(text: str) -> str:
    t = _normalize_text_unaccent(text).lower()
    if re.search(r"\b1\s*a[nñ]o\b|\b1y\b|\b12\s*mes(es)?\b", t):
        return "1y"
    if re.search(r"\b6\s*mes(es)?\b|\b6m\b", t):
        return "6mo"
    if re.search(r"\b1\s*mes\b|\b1m\b|\b30\s*d[ií]as?\b", t):
        return "1mo"
    if re.search(r"\b5\s*d[ií]as?\b|\b5d\b|\b1\s*semana\b", t):
        return "5d"
    return os.getenv("PRICE_CHART_DEFAULT_PERIOD", "3mo").strip() or "3mo"


def _extract_ticker_for_chart(text: str) -> str | None:
    ticker = r"([A-Za-z][A-Za-z0-9.\^]{0,9})\b"
    patterns = [
        r"(?:gráfic[oa]|grafic[oa]|chart)\s+del\s+ticker\s+" + ticker,
        r"\bdel\s+ticker\s+" + ticker,
        r"(?:gráfic[oa]|grafic[oa]|chart)\b[\s\w,.]{0,48}?\b(?:de|del)\s+" + ticker,
        r"(?:gráfic[oa]|grafic[oa]|chart|historial\s+de\s+precios?|evoluci[oó]n(?:\s+del\s+precio)?)\s+(?:de\s+|del\s+)"
        + ticker,
        r"(?:gráfic[oa]|grafic[oa]|chart)\s+" + ticker,
        r"\b" + ticker + r"\s+(?:gráfic[oa]|grafic[oa]|chart)\b",
        r"precio\s+de\s+" + ticker,
        r"c[oó]mo\s+va\s+" + ticker,
        r"(?:va|anda|cerr[oó])\s+" + ticker,
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            c = _clean_asset_ticker(m.group(1))
            if c:
                return c
    return None


def _extract_ticker_for_detail(text: str) -> str | None:
    patterns = [
        r"(?:detalles?|informaci[oó]n|info|ficha)\s+(?:de|del|sobre)\s+(?:el\s+|la\s+)?(?:ticker\s+)?"
        + r"([A-Za-z][A-Za-z0-9.\^]{0,9})\b",
        r"(?:c[oó]mo\s+va|a\s+c[oó]mo\s+est[aá])\s+(?:el\s+|la\s+)?(?:ticker\s+)?" + r"([A-Za-z][A-Za-z0-9.\^]{0,9})\b",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            c = _clean_asset_ticker(m.group(1))
            if c:
                return c
    return None


def dispatch_intent(text: str) -> tuple[str, dict]:
    """Devuelve (herramienta, params). Herramientas: morning_shot | news_summary | quote | price_chart | none."""
    t = _normalize_text_unaccent(text.strip()).lower()

    is_news = bool(_NEWS_KW.search(t)) and not bool(_MORNING_SHOT_KW.search(t))
    is_morning = bool(_MORNING_SHOT_KW.search(t))

    if is_morning:
        sources: list[str] = []
        if _WSJ_KW.search(t):
            sources = ["wsj"]
        elif _NYT_KW.search(t):
            sources = ["nyt"]
        return "morning_shot", {"sources": sources or ["wsj", "nyt"]}

    if _CHART_LINE.search(t):
        ct = _extract_ticker_for_chart(t)
        if ct:
            return "price_chart", {"ticker": ct, "period": _parse_chart_period(t)}

    if _ASSET_DETAIL_LINE.search(t):
        dt = _extract_ticker_for_chart(t) or _extract_ticker_for_detail(t)
        if dt:
            return "price_chart", {"ticker": dt, "period": _parse_chart_period(t)}

    if is_news:
        src = _parse_news_source_ids_from_message(t)
        return "news_summary", {"source_ids": src}

    m = _QUOTE_KW.search(t)
    if m:
        return "quote", {"ticker": m.group(1).upper()}

    return "none", {}
