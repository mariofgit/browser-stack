"""Finance agent — WSJ morning shot via Browserbase scraping (no NAP audit, no SDR/CRM)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncGenerator
from html import unescape
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yfinance as yf
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from runtime.browserbase_wsj import (
    REQUIRES_AUTH_STATE,
    fetch_multi_site_pages_via_browserbase,
    fetch_wsj_pages_via_browserbase,
)
from runtime.intent_dispatch import dispatch_intent as _dispatch_intent
from runtime.news_fetcher import NEWS_SOURCES, fetch_news_sources


def _load_local_env() -> None:
    """Load agent/.env in local dev without overriding exported env vars."""
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[1] / ".env",
    ]
    for path in candidates:
        if not path.exists():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return


_load_local_env()

app = FastAPI(title="Finance Agent")

WSJ_SUMMARY_DISCLAIMER = (
    "Informational summary based on sources named in the facts pack; "
    "it is not investment advice or a recommendation to buy or sell."
)

MIN_WSJ_EQUITIES = 3

_ALLOWED_SECTION_IDS = frozenset(
    {"macro_markets", "local_market", "regulatory", "market_integrity", "systemic", "agenda"}
)

_WSJ_TICKER_NOISE = frozenset(
    {
        "THE", "AND", "FOR", "ARE", "NYSE", "NASDAQ", "INDEX", "STOCK",
        "MARKET", "TRUMP", "BIDEN", "IPO", "CEO", "CFO", "USA", "ESG",
        "PDF", "XML", "API", "FAQ",
    }
)


class WsjMorningShotRequest(BaseModel):
    snapshot_label: str = Field(default="wsj-morning-shot")
    sections: list[str] | None = Field(
        default=None,
        description="Sections to fetch: market_snapshot, top_headlines, economy_policy, wsj_finance",
    )
    browserbase_session_id: str | None = Field(
        default=None,
        description="Resume an open Browserbase session after REQUIRES_AUTH (Live View login).",
    )


# ---------------------------------------------------------------------------
# Helpers: HTML fetching
# ---------------------------------------------------------------------------

def _mask_secret(secret: str) -> str:
    if len(secret) <= 8:
        return "********"
    return f"{secret[:4]}...{secret[-4:]}"


def _wsj_headers(*, session_cookie: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": os.getenv(
            "WSJ_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    cookie = (session_cookie or "").strip() or os.getenv("WSJ_SESSION_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers


async def _fetch_wsj_page(
    client: httpx.AsyncClient, base: str, path: str, *, session_cookie: str | None = None
) -> dict:
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    response = await client.get(url, headers=_wsj_headers(session_cookie=session_cookie))
    html = response.text
    requires_login = response.status_code in (401, 403) or "subscribe" in html.lower()[:5000]
    return {
        "url": url,
        "status_code": response.status_code,
        "ok": response.status_code < 400,
        "requires_login": requires_login,
        "html": html,
    }


# ---------------------------------------------------------------------------
# Helpers: HTML parsing
# ---------------------------------------------------------------------------

def _clean_text(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _extract_links(
    html: str,
    *,
    limit: int = 15,
    allowed_host: str = "wsj.com",
    base_url: str = "https://www.wsj.com",
) -> list[dict]:
    links: list[dict] = []
    seen: set[str] = set()
    for match in re.finditer(r"<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", html, re.IGNORECASE | re.DOTALL):
        href = match.group(1).strip()
        title = _clean_text(match.group(2))
        if not title or len(title) < 20:
            continue
        if href.startswith("/"):
            href = f"{base_url.rstrip('/')}{href}"
        if allowed_host not in href:
            continue
        key = f"{title}|{href}"
        if key in seen:
            continue
        seen.add(key)
        links.append({"title": title, "url": href})
        if len(links) >= limit:
            break
    return links


def _extract_market_snapshot(html: str) -> list[dict]:
    snapshot: list[dict] = []
    text = _clean_text(html)
    patterns = [
        ("S&P 500", r"S&P 500[^0-9\-+]*([0-9][0-9,\.]*)"),
        ("Dow Jones", r"Dow Jones[^0-9\-+]*([0-9][0-9,\.]*)"),
        ("Nasdaq", r"Nasdaq[^0-9\-+]*([0-9][0-9,\.]*)"),
    ]
    for instrument, pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            snapshot.append(
                {
                    "instrument": instrument,
                    "value": m.group(1),
                    "change": None,
                    "as_of": datetime.now(timezone.utc).isoformat(),
                }
            )
    return snapshot


# ---------------------------------------------------------------------------
# Helpers: equity selection + Yahoo quotes
# ---------------------------------------------------------------------------

def _extract_wsj_equity_candidates(html: str, *, limit: int = 24) -> list[str]:
    patterns = [
        re.compile(r"/market-data/stocks/([A-Za-z]{1,6}(?:\.[A-Za-z])?)/"),
        re.compile(r"/market-data/quotes/([A-Za-z]{1,6}(?:\.[A-Za-z])?)(?:[/?\"'>\s]|$)"),
        re.compile(r"/quotes/US/([A-Za-z]{1,6}(?:\.[A-Za-z])?)/"),
    ]
    hits: list[tuple[int, str]] = []
    for pat in patterns:
        for m in pat.finditer(html):
            sym = m.group(1).upper()
            if sym in _WSJ_TICKER_NOISE or len(sym) < 1:
                continue
            if not re.fullmatch(r"[A-Z][A-Z0-9]{0,5}(?:\.[A-Z])?", sym):
                continue
            hits.append((m.start(), sym))
    hits.sort(key=lambda x: x[0])
    out: list[str] = []
    seen: set[str] = set()
    for _, sym in hits:
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= limit:
            break
    return out


def _parse_env_ticker_csv(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def _yahoo_most_active_symbols(*, count: int = 15) -> list[str]:
    scr_id = os.getenv("WSJ_EQUITY_SCREENER_ID", "most_actives").strip() or "most_actives"
    block = yf.screen(scr_id, count=count)
    quotes = block.get("quotes")
    if not isinstance(quotes, list):
        return []
    out: list[str] = []
    for q in quotes:
        if isinstance(q, dict):
            s = q.get("symbol")
            if isinstance(s, str) and s.strip():
                out.append(s.strip().upper())
    return out


def _select_equities_for_wsj(market_html: str) -> tuple[list[dict[str, str]], list[str]]:
    errs: list[str] = []
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(sym: str, source: str) -> None:
        sym = sym.strip().upper().replace("-", ".")
        if not re.fullmatch(r"[A-Z][A-Z0-9]{0,5}(?:\.[A-Z])?", sym):
            return
        if sym.startswith("^"):
            return
        if sym in seen:
            return
        seen.add(sym)
        ordered.append({"ticker": sym, "source": source})

    for sym in _extract_wsj_equity_candidates(market_html):
        add(sym, "wsj_html")

    if len(ordered) < MIN_WSJ_EQUITIES:
        try:
            for sym in _yahoo_most_active_symbols(count=20):
                add(sym, "yahoo_most_actives")
                if len(ordered) >= MIN_WSJ_EQUITIES + 2:
                    break
        except Exception as e:  # noqa: BLE001
            errs.append(f"yahoo_most_actives: {e}")

    if len(ordered) < MIN_WSJ_EQUITIES:
        fall = os.getenv("WSJ_EQUITY_FALLBACK_TICKERS", "NVDA,TSLA,AAPL")
        for sym in _parse_env_ticker_csv(fall):
            add(sym, "env_fallback")
            if len(ordered) >= MIN_WSJ_EQUITIES:
                break

    if len(ordered) < MIN_WSJ_EQUITIES:
        errs.append(
            f"equity_selection: only {len(ordered)} tickers after wsj+yahoo+fallback (min {MIN_WSJ_EQUITIES})"
        )

    try:
        cap = max(MIN_WSJ_EQUITIES, int(os.getenv("WSJ_EQUITY_QUOTE_MAX", "5")))
    except ValueError:
        cap = 5
    return ordered[:cap], errs


def _select_mx_equities() -> list[dict[str, str]]:
    """MX equities vía env var (sin scraping)."""
    raw = os.getenv("MX_EQUITY_TICKERS", "CEMEX.MX,GFNORTEO.MX,AMXL.MX,FEMSAUBD.MX,WALMEX.MX")
    out: list[dict[str, str]] = []
    for sym in _parse_env_ticker_csv(raw):
        s = sym.strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9]{0,5}(?:\.[A-Z]{2,3})?", s):
            out.append({"ticker": s, "source": "mx_env"})
    return out


def _quote_one(symbol: str) -> dict:
    t = yf.Ticker(symbol)
    hist = t.history(period="5d")
    if hist.empty or len(hist) < 1:
        return {"Ticker": symbol, "ok": False, "error": "no_history"}
    last_close = float(hist["Close"].iloc[-1])
    prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else last_close
    change_pct = ((last_close - prev_close) / prev_close * 100.0) if prev_close else 0.0
    idx = hist.index[-1]
    as_of = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
    fast_info = getattr(t, "fast_info", {}) or {}
    currency = fast_info.get("currency") if isinstance(fast_info, dict) else None
    info_name = None
    if not currency or not info_name:
        try:
            info = t.info if isinstance(t.info, dict) else {}
            currency = currency or info.get("currency")
            info_name = info.get("longName") or info.get("shortName")
        except Exception:  # noqa: BLE001
            info_name = None

    def format_currency(value: float, code: str | None) -> str:
        symbol_map = {"USD": "$", "EUR": "EUR ", "GBP": "GBP ", "JPY": "JPY "}
        if code and code in symbol_map:
            return f"{symbol_map[code]}{value:,.2f}"
        if code:
            return f"{value:,.2f} {code}"
        return f"{value:,.2f}"

    return {
        "Ticker": symbol,
        "ok": True,
        "name": info_name or symbol,
        "last_close": format_currency(last_close, currency),
        "prev_close": format_currency(prev_close, currency),
        "change_pct": f"{change_pct:+.2f}%",
        "last_close_raw": round(last_close, 4),
        "prev_close_raw": round(prev_close, 4),
        "change_pct_raw": round(change_pct, 4),
        "as_of": as_of,
    }


# ---------------------------------------------------------------------------
# Helpers: LLM compact + OpenAI summary
# ---------------------------------------------------------------------------

def _compact_facts_for_llm(
    market_snapshot: list[dict],
    top_headlines: list[dict],
    economy_policy: list[dict],
    wsj_finance: list[dict],
    equities: list[dict],
    macro: list[dict],
) -> dict:
    return {
        "wsj_market_snapshot": market_snapshot,
        "wsj_headlines": [{"title": h.get("title"), "url": h.get("url")} for h in top_headlines[:6]],
        "wsj_economy": [{"title": h.get("title"), "url": h.get("url")} for h in economy_policy[:6]],
        "wsj_finance": [{"title": h.get("title"), "url": h.get("url")} for h in wsj_finance[:6]],
        "equities": [
            {
                "ticker": e["ticker"],
                "source": e["source"],
                "name": (e.get("quote") or {}).get("name"),
                "last_close": (e.get("quote") or {}).get("last_close"),
                "change_pct": (e.get("quote") or {}).get("change_pct"),
                "as_of": (e.get("quote") or {}).get("as_of"),
                "ok": (e.get("quote") or {}).get("ok"),
            }
            for e in equities
        ],
        "macro_indices": [
            {
                "ticker": m["ticker"],
                "name": (m.get("quote") or {}).get("name"),
                "last_close": (m.get("quote") or {}).get("last_close"),
                "change_pct": (m.get("quote") or {}).get("change_pct"),
                "as_of": (m.get("quote") or {}).get("as_of"),
                "ok": (m.get("quote") or {}).get("ok"),
            }
            for m in macro
        ],
    }


def _normalize_structured_summary(data: dict) -> dict:
    sections_out: list[dict] = []
    for s in data.get("sections") or []:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if sid not in _ALLOWED_SECTION_IDS:
            continue
        bullets = [b for b in (s.get("bullets") or []) if isinstance(b, str) and b.strip()]
        if not bullets:
            continue
        sections_out.append({"id": sid, "title": str(s.get("title") or sid), "bullets": bullets[:14]})
    watch = [w for w in (data.get("watch_today") or []) if isinstance(w, str) and w.strip()][:12]
    return {"disclaimer": WSJ_SUMMARY_DISCLAIMER, "sections": sections_out, "watch_today": watch}


def _openai_wsj_summary_sync(compact: dict) -> tuple[dict | None, list[str]]:
    errs: list[str] = []
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        errs.append("OPENAI_API_KEY not set; structured_summary omitted")
        return None, errs
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    try:
        from openai import OpenAI
    except ImportError as e:  # pragma: no cover
        errs.append(f"openai package missing: {e}")
        return None, errs

    payload = json.dumps(compact, ensure_ascii=False)
    if len(payload) > 48000:
        payload = payload[:48000] + "\n...(truncated)"

    system = (
        "You are a financial risk analyst. Reply with only a valid UTF-8 JSON object, no Markdown. "
        "Use only facts present in the facts pack; do not invent regulation or markets not in the pack. "
        "Do not give investment advice or buy/sell recommendations."
    )
    disc = json.dumps(WSJ_SUMMARY_DISCLAIMER, ensure_ascii=False)
    user = (
        "Facts pack (JSON):\n"
        f"{payload}\n\n"
        "Return JSON with keys: disclaimer (short string), sections (array), watch_today (array of strings).\n"
        "- sections: objects {id, title, bullets} where bullets is an array of concise technical strings in English.\n"
        "- Allowed section ids only: macro_markets, local_market, regulatory, market_integrity, systemic, agenda.\n"
        "- Omit any section that lacks supporting evidence in the pack.\n"
        "- In at least one section (usually macro_markets), include factual bullets about rows in `equities`, "
        "citing ticker, percent change when present, and `source` to disambiguate.\n"
        "- watch_today: 3 to 8 short strings in English.\n"
        f"- Set disclaimer to exactly: {disc}"
    )

    client = OpenAI(api_key=api_key)
    try:
        completion = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.25,
            max_tokens=2200,
        )
    except Exception as e:  # noqa: BLE001
        errs.append(f"openai: {e}")
        return None, errs

    raw = completion.choices[0].message.content
    if not raw:
        errs.append("openai: empty response")
        return None, errs
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        errs.append(f"openai json: {e}")
        return None, errs
    if not isinstance(parsed, dict):
        errs.append("openai: response not an object")
        return None, errs
    return _normalize_structured_summary(parsed), errs


# ---------------------------------------------------------------------------
# Price chart (yfinance) — series para /chat price_chart
# ---------------------------------------------------------------------------


def _downsample_pairs(
    times: list[str], values: list[float], max_points: int = 160
) -> tuple[list[str], list[float]]:
    if len(values) <= max_points:
        return times, values
    step = len(values) / max_points
    out_t: list[str] = []
    out_v: list[float] = []
    i = 0.0
    while int(i) < len(values):
        idx = int(i)
        out_t.append(times[idx])
        out_v.append(values[idx])
        i += step
    return out_t, out_v


def _price_chart_sync(symbol: str, period: str) -> dict:
    t = yf.Ticker(symbol)
    hist = t.history(period=period, interval="1d", auto_adjust=True)
    if hist.empty or len(hist) < 1:
        return {"Ticker": symbol, "ok": False, "error": "no_history", "period": period}
    closes = [float(x) for x in hist["Close"].tolist()]
    idx_list = hist.index
    times: list[str] = []
    for idx in idx_list:
        if hasattr(idx, "isoformat"):
            times.append(idx.isoformat())  # type: ignore[union-attr]
        else:
            times.append(str(idx))
    times, closes = _downsample_pairs(times, closes)
    quote = _quote_one(symbol)
    return {
        "ok": True,
        "Ticker": symbol.upper(),
        "period": period,
        "series": [{"t": a, "c": b} for a, b in zip(times, closes, strict=True)],
        "quote": quote,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"ok": True, "agent": "finance-agent"}


@app.post("/wsj-morning-shot")
async def wsj_morning_shot(body: WsjMorningShotRequest):
    correlation_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    requested_sections = body.sections or [
        "market_snapshot",
        "top_headlines",
        "economy_policy",
        "wsj_finance",
    ]
    base = os.getenv("WSJ_BASE_URL", "https://www.wsj.com")
    timeout = float(os.getenv("WSJ_TIMEOUT_SECONDS", "20"))
    markets_path = os.getenv("WSJ_MARKETS_PATH", "/markets")
    headlines_path = os.getenv("WSJ_HEADLINES_PATH", "/news")
    economy_path = os.getenv("WSJ_ECONOMY_PATH", "/economy")
    finance_path = os.getenv("WSJ_FINANCE_PATH", "/finance")

    path_by_section = {
        "market_snapshot": markets_path,
        "top_headlines": headlines_path,
        "economy_policy": economy_path,
        "wsj_finance": finance_path,
    }

    pages: dict[str, dict] = {}
    errors: list[str] = []
    auth_mode = "browserbase"
    last_bb_session_id: str | None = None
    session_cookie = ""

    if os.getenv("BROWSERBASE_API_KEY", "").strip():
        bb_result = await fetch_wsj_pages_via_browserbase(
            client_key="browser-stack",
            requested_sections=requested_sections,
            browserbase_session_id=(body.browserbase_session_id or "").strip() or None,
            base_url=base,
            paths=path_by_section,
        )
        if bb_result.get("state") == REQUIRES_AUTH_STATE:
            return {
                "ok": False,
                "error": "REQUIRES_AUTH",
                "state": REQUIRES_AUTH_STATE,
                "browserbase_session_id": bb_result.get("browserbase_session_id"),
                "interactive_live_view_url": bb_result.get("interactive_live_view_url"),
                "message": bb_result.get("message"),
            }
        if not bb_result.get("ok"):
            return {
                "ok": False,
                "error": bb_result.get("error", "browserbase_failed"),
                "detail": bb_result.get("detail"),
                "browserbase_status": bb_result.get("browserbase_status"),
            }
        pages = bb_result.get("pages") or {}
        last_bb_session_id = bb_result.get("browserbase_session_id")
    else:
        session_cookie = os.getenv("WSJ_SESSION_COOKIE", "").strip()
        if not session_cookie:
            return {
                "ok": False,
                "error": "configure_BROWSERBASE_API_KEY_or_WSJ_SESSION_COOKIE",
                "hint": "Set BROWSERBASE_API_KEY for cloud browser + Live View, or WSJ_SESSION_COOKIE for legacy HTTP fetch.",
            }
        auth_mode = "wsj_session_cookie_env"
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for section, path in path_by_section.items():
                if section not in requested_sections:
                    continue
                try:
                    pages[section] = await _fetch_wsj_page(client, base, path, session_cookie=session_cookie)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{section}: {e}")
                    pages[section] = {"ok": False, "error": str(e)}

    market_html = str(pages.get("market_snapshot", {}).get("html", ""))
    market_snapshot = _extract_market_snapshot(market_html)
    top_headlines = _extract_links(str(pages.get("top_headlines", {}).get("html", "")), limit=8)
    economy_policy = _extract_links(str(pages.get("economy_policy", {}).get("html", "")), limit=8)
    wsj_finance_links = _extract_links(str(pages.get("wsj_finance", {}).get("html", "")), limit=8)
    login_required = any(bool(pages.get(k, {}).get("requires_login")) for k in pages)

    equity_selection, eq_sel_errs = _select_equities_for_wsj(market_html)
    errors.extend(eq_sel_errs)

    equities_facts: list[dict] = []
    for entry in equity_selection:
        sym = entry["ticker"]
        try:
            equities_facts.append({"ticker": sym, "source": entry["source"], "quote": _quote_one(sym)})
        except Exception as e:  # noqa: BLE001
            errors.append(f"quote {sym}: {e}")
            equities_facts.append(
                {"ticker": sym, "source": entry["source"], "quote": {"Ticker": sym, "ok": False, "error": str(e)}}
            )

    macro_raw = os.getenv("WSJ_RISK_YAHOO_TICKERS", "MXN=X,^MXX,^GSPC")
    macro_symbols = _parse_env_ticker_csv(macro_raw)
    macro_facts: list[dict] = []
    for sym in macro_symbols:
        try:
            macro_facts.append({"ticker": sym, "quote": _quote_one(sym)})
        except Exception as e:  # noqa: BLE001
            errors.append(f"macro {sym}: {e}")
            macro_facts.append({"ticker": sym, "quote": {"Ticker": sym, "ok": False, "error": str(e)}})

    facts_pack = {
        "generated_at": generated_at,
        "equity_selection": equity_selection,
        "wsj": {
            "market_snapshot": market_snapshot,
            "top_headlines": top_headlines,
            "economy_policy": economy_policy,
            "wsj_finance": wsj_finance_links,
        },
        "equities": equities_facts,
        "macro_indices": macro_facts,
    }

    compact = _compact_facts_for_llm(
        market_snapshot, top_headlines, economy_policy, wsj_finance_links, equities_facts, macro_facts
    )
    structured_summary: dict | None = None
    if os.getenv("OPENAI_API_KEY", "").strip():
        structured_summary, llm_errs = await asyncio.to_thread(_openai_wsj_summary_sync, compact)
        errors.extend(llm_errs)
    else:
        errors.append("OPENAI_API_KEY not set; structured_summary omitted")

    return {
        "ok": True,
        "correlation_id": correlation_id,
        "snapshot_label": body.snapshot_label,
        "generated_at": generated_at,
        "source": "wsj_browserbase" if auth_mode == "browserbase" else "wsj_cookie_env",
        "requested_sections": requested_sections,
        "market_snapshot": market_snapshot,
        "top_headlines": top_headlines,
        "economy_policy": economy_policy,
        "wsj_finance": wsj_finance_links,
        "facts_pack": facts_pack,
        "structured_summary": structured_summary,
        "login_required": login_required,
        "errors": errors,
        "meta": {
            "base_url": base,
            "auth_mode": auth_mode,
            "paths": path_by_section,
            "browserbase_configured": bool(os.getenv("BROWSERBASE_API_KEY", "").strip()),
            "browserbase_session_id": last_bb_session_id,
            "session_cookie_configured": bool(os.getenv("WSJ_SESSION_COOKIE", "").strip()),
            "session_cookie_masked": _mask_secret(session_cookie) if session_cookie else "",
            "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        },
    }


# ---------------------------------------------------------------------------
# BIVA Morning Shot: WSJ + NYT + Yahoo MX
# ---------------------------------------------------------------------------

BIVA_DISCLAIMER = (
    "Resumen informativo con base en las fuentes citadas en facts_pack; "
    "no constituye recomendación de inversión ni orden de compra/venta."
)


class MorningShotRequest(BaseModel):
    snapshot_label: str = Field(default="biva-morning-shot")
    sources: list[str] | None = Field(
        default=None,
        description="Subset of sources to use: wsj, nyt. Default: both.",
    )
    browserbase_session_id: str | None = None


def _compact_biva_for_llm(
    wsj_pages: dict,
    nyt_pages: dict,
    int_equities: list[dict],
    mx_indices: list[dict],
    mx_equities: list[dict],
) -> dict:
    return {
        "wsj_headlines": _extract_links(str(wsj_pages.get("top_headlines", {}).get("html", "")), limit=6),
        "wsj_market": str(wsj_pages.get("market_snapshot", {}).get("html", ""))[:4000],
        "wsj_economy": _extract_links(str(wsj_pages.get("economy_policy", {}).get("html", "")), limit=6),
        "nyt_world": _extract_links(
            str(nyt_pages.get("nyt_world", {}).get("html", "")),
            limit=6,
            allowed_host="nytimes.com",
            base_url="https://www.nytimes.com",
        ),
        "nyt_business": _extract_links(
            str(nyt_pages.get("nyt_business", {}).get("html", "")),
            limit=6,
            allowed_host="nytimes.com",
            base_url="https://www.nytimes.com",
        ),
        "int_equities": [
            {
                "ticker": e["ticker"],
                "source": e["source"],
                "change_pct": (e.get("quote") or {}).get("change_pct"),
                "last_close": (e.get("quote") or {}).get("last_close"),
            }
            for e in int_equities
        ],
        "mx_indices": [
            {
                "ticker": m["ticker"],
                "name": (m.get("quote") or {}).get("name"),
                "last_close": (m.get("quote") or {}).get("last_close"),
                "change_pct": (m.get("quote") or {}).get("change_pct"),
            }
            for m in mx_indices
        ],
        "mx_equities": [
            {
                "ticker": e["ticker"],
                "change_pct": (e.get("quote") or {}).get("change_pct"),
                "last_close": (e.get("quote") or {}).get("last_close"),
            }
            for e in mx_equities
        ],
    }


def _normalize_biva_morning_shot(data: dict) -> dict:
    def _list_of_items(raw) -> list[dict]:
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for it in raw:
            if not isinstance(it, dict):
                continue
            item = {
                "ticker": str(it.get("ticker", "")),
                "change_pct": str(it.get("change_pct", "")),
                "reason": str(it.get("reason", "")),
                "source": str(it.get("source", "")),
            }
            if item["ticker"]:
                out.append(item)
        return out[:6]

    internacional = data.get("internacional") or {}
    nacional = data.get("nacional") or {}

    return {
        "disclaimer": BIVA_DISCLAIMER,
        "internacional": {
            "narrative": str(internacional.get("narrative", "")).strip(),
            "bulls": _list_of_items(internacional.get("bulls")),
            "bears": _list_of_items(internacional.get("bears")),
        },
        "nacional": {
            "narrative": str(nacional.get("narrative", "")).strip(),
            "indices": [
                {
                    "name": str(i.get("name", "")),
                    "value": str(i.get("value", "")),
                    "change_pct": str(i.get("change_pct", "")),
                }
                for i in (nacional.get("indices") or [])
                if isinstance(i, dict)
            ][:6],
            "bulls": _list_of_items(nacional.get("bulls")),
            "bears": _list_of_items(nacional.get("bears")),
        },
        "dato_del_dia": str(data.get("dato_del_dia", "")).strip(),
    }


def _biva_ticker_key(ticker: str) -> str:
    return str(ticker or "").strip().upper().replace("-", ".")


def _biva_int_equity_sources_from_compact(compact: dict) -> dict[str, str]:
    """Ticker → etiqueta de fuente para bulls/bears internacionales (solo hechos del pack)."""
    out: dict[str, str] = {}
    for row in compact.get("int_equities") or []:
        if not isinstance(row, dict):
            continue
        t = _biva_ticker_key(str(row.get("ticker", "")))
        if not t:
            continue
        raw_src = str(row.get("source", "")).strip()
        out[t] = "wsj" if raw_src == "wsj_html" else "yahoo"
    return out


def _biva_mx_tickers_from_compact(compact: dict) -> set[str]:
    s: set[str] = set()
    for row in compact.get("mx_equities") or []:
        if not isinstance(row, dict):
            continue
        t = _biva_ticker_key(str(row.get("ticker", "")))
        if t:
            s.add(t)
    return s


def _apply_biva_attribution(biva: dict, compact: dict) -> dict:
    """
    Corrige atribuciones: int_equities nunca vienen de NYT en el pipeline; el modelo a veces pone nyt.
    Solo conserva filas cuyo ticker existe en el facts pack.
    """
    int_src = _biva_int_equity_sources_from_compact(compact)
    mx_ok = _biva_mx_tickers_from_compact(compact)

    def _fix_int_items(raw) -> list[dict]:
        out: list[dict] = []
        for it in raw or []:
            if not isinstance(it, dict):
                continue
            t = _biva_ticker_key(str(it.get("ticker", "")))
            if not t or t not in int_src:
                continue
            out.append({
                "ticker": t,
                "change_pct": str(it.get("change_pct", "")),
                "reason": str(it.get("reason", "")),
                "source": int_src[t],
            })
        return out[:6]

    def _fix_mx_items(raw) -> list[dict]:
        out: list[dict] = []
        for it in raw or []:
            if not isinstance(it, dict):
                continue
            t = _biva_ticker_key(str(it.get("ticker", "")))
            if not t or t not in mx_ok:
                continue
            out.append({
                "ticker": t,
                "change_pct": str(it.get("change_pct", "")),
                "reason": str(it.get("reason", "")),
                "source": "yahoo",
            })
        return out[:6]

    inter = biva.get("internacional") or {}
    nac = biva.get("nacional") or {}
    if isinstance(inter, dict):
        inter = {
            **inter,
            "bulls": _fix_int_items(inter.get("bulls")),
            "bears": _fix_int_items(inter.get("bears")),
        }
    if isinstance(nac, dict):
        nac = {
            **nac,
            "bulls": _fix_mx_items(nac.get("bulls")),
            "bears": _fix_mx_items(nac.get("bears")),
        }
    return {**biva, "internacional": inter, "nacional": nac}


def _build_biva_openai_messages(compact: dict) -> list[dict[str, str]]:
    payload = json.dumps(compact, ensure_ascii=False)
    if len(payload) > 48000:
        payload = payload[:48000] + "\n...(truncated)"

    system = (
        "Eres un analista de mercados en México que produce un Morning Shot estilo BIVA. "
        "Responde SOLO con un objeto JSON válido en UTF-8, sin Markdown. "
        "Usa ÚNICAMENTE hechos presentes en el facts pack; no inventes datos ni tickers. "
        "No des recomendaciones de compra/venta."
    )
    disc = json.dumps(BIVA_DISCLAIMER, ensure_ascii=False)
    user = (
        "Facts pack (JSON):\n"
        f"{payload}\n\n"
        "Devuelve JSON con la estructura exacta:\n"
        "{\n"
        '  "disclaimer": string,\n'
        '  "internacional": {\n'
        '    "narrative": string (párrafo breve; puedes citar contexto de NYT aquí si aplica),\n'
        '    "bulls": [{"ticker", "change_pct", "reason", "source": "wsj"|"yahoo"}],\n'
        '    "bears": [{"ticker", "change_pct", "reason", "source": "wsj"|"yahoo"}]\n'
        "  },\n"
        "  Regla: cada fila en int_equities del pack trae source. Si es wsj_html → source \"wsj\"; "
        "si es yahoo_most_actives, env_fallback u otro → source \"yahoo\". "
        "No uses \"nyt\" en bulls/bears internacionales (NYT no alimenta esas filas de precios). "
        "Solo incluye tickers que existan en int_equities.\n"
        '  "nacional": {\n'
        '    "narrative": string (párrafo breve en español sobre mercado MX),\n'
        '    "indices": [{"name", "value", "change_pct"}],\n'
        '    "bulls": [{"ticker", "change_pct", "reason", "source": "yahoo"}],\n'
        '    "bears": [{"ticker", "change_pct", "reason", "source": "yahoo"}]\n'
        "  },\n"
        '  "dato_del_dia": string (el hecho más destacado del día)\n'
        "}\n"
        f"El campo disclaimer debe ser exactamente: {disc}\n"
        "Máximo 4 bulls y 4 bears por sección. Todo en español."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _openai_biva_sync(compact: dict) -> tuple[dict | None, list[str]]:
    errs: list[str] = []
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        errs.append("OPENAI_API_KEY not set; biva_summary omitted")
        return None, errs
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    try:
        from openai import OpenAI
    except ImportError as e:  # pragma: no cover
        errs.append(f"openai package missing: {e}")
        return None, errs

    client = OpenAI(api_key=api_key)
    try:
        completion = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=_build_biva_openai_messages(compact),
            temperature=0.3,
            max_tokens=2200,
        )
    except Exception as e:  # noqa: BLE001
        errs.append(f"openai: {e}")
        return None, errs

    raw = completion.choices[0].message.content
    if not raw:
        errs.append("openai: empty response")
        return None, errs
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        errs.append(f"openai json: {e}")
        return None, errs
    if not isinstance(parsed, dict):
        errs.append("openai: response not an object")
        return None, errs
    return _apply_biva_attribution(_normalize_biva_morning_shot(parsed), compact), errs


async def _openai_biva_stream_raw(compact: dict) -> AsyncGenerator[str, None]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    stream = await client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=_build_biva_openai_messages(compact),
        temperature=0.3,
        max_tokens=2200,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


@app.post("/morning-shot")
async def morning_shot(body: MorningShotRequest):
    correlation_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    sources = [s.lower() for s in (body.sources or ["wsj", "nyt"]) if s]
    errors: list[str] = []
    auth_failures: list[str] = []
    live_view_url: str | None = None
    last_bb_session_id: str | None = None

    if not os.getenv("BROWSERBASE_API_KEY", "").strip():
        return {"ok": False, "error": "BROWSERBASE_API_KEY_required"}

    sites_cfg: list[dict] = []
    wsj_base = os.getenv("WSJ_BASE_URL", "https://www.wsj.com")
    if "wsj" in sources:
        sites_cfg.append({
            "site": "wsj",
            "base_url": wsj_base,
            "probe_url": os.getenv("WSJ_SESSION_PROBE_URL", f"{wsj_base}/signin"),
            "paths": {
                "market_snapshot": os.getenv("WSJ_MARKETS_PATH", "/markets"),
                "top_headlines": os.getenv("WSJ_HEADLINES_PATH", "/news"),
                "economy_policy": os.getenv("WSJ_ECONOMY_PATH", "/economy"),
            },
        })

    nyt_base = os.getenv("NYT_BASE_URL", "https://www.nytimes.com")
    if "nyt" in sources:
        sites_cfg.append({
            "site": "nyt",
            "base_url": nyt_base,
            "probe_url": os.getenv("NYT_SESSION_PROBE_URL", nyt_base),
            "paths": {
                "nyt_world": os.getenv("NYT_WORLD_PATH", "/section/world"),
                "nyt_business": os.getenv("NYT_BUSINESS_PATH", "/section/business"),
            },
        })

    bb_result = await fetch_multi_site_pages_via_browserbase(
        sites=sites_cfg,
        browserbase_session_id=(body.browserbase_session_id or "").strip() or None,
    )

    if not bb_result.get("ok"):
        return {
            "ok": False,
            "error": bb_result.get("error", "browserbase_failed"),
            "detail": bb_result.get("detail"),
            "browserbase_session_id": bb_result.get("browserbase_session_id"),
        }

    last_bb_session_id = bb_result.get("browserbase_session_id")
    auth_failures = list(bb_result.get("auth_failures") or [])
    live_view_url = bb_result.get("live_view_url")
    pages = bb_result.get("pages") or {}

    # Si TODOS los sitios requeridos fallan → REQUIRES_AUTH con live view
    if auth_failures and not pages:
        return {
            "ok": False,
            "error": "REQUIRES_AUTH",
            "state": REQUIRES_AUTH_STATE,
            "browserbase_session_id": last_bb_session_id,
            "interactive_live_view_url": live_view_url,
            "auth_failures": auth_failures,
            "message": f"Inicia sesión en {', '.join(s.upper() for s in auth_failures)} en Live View.",
        }

    wsj_pages = {k: v for k, v in pages.items() if v.get("site") == "wsj"}
    nyt_pages = {k: v for k, v in pages.items() if v.get("site") == "nyt"}

    # Int equities (WSJ market HTML → heuristic + Yahoo fallback)
    wsj_market_html = str(wsj_pages.get("market_snapshot", {}).get("html", ""))
    int_equity_selection, eq_errs = _select_equities_for_wsj(wsj_market_html)
    errors.extend(eq_errs)
    int_equities: list[dict] = []
    for entry in int_equity_selection:
        sym = entry["ticker"]
        try:
            int_equities.append({"ticker": sym, "source": entry["source"], "quote": _quote_one(sym)})
        except Exception as e:  # noqa: BLE001
            errors.append(f"int quote {sym}: {e}")

    # MX indices (^MXX, ^BIVA, MXN=X) + macro configurado
    macro_raw = os.getenv("MACRO_YAHOO_TICKERS", "MXN=X,^MXX,^GSPC,^DJI,^IXIC")
    mx_indices: list[dict] = []
    for sym in _parse_env_ticker_csv(macro_raw):
        try:
            mx_indices.append({"ticker": sym, "quote": _quote_one(sym)})
        except Exception as e:  # noqa: BLE001
            errors.append(f"mx index {sym}: {e}")

    # MX equities via env var
    mx_equity_selection = _select_mx_equities()
    mx_equities: list[dict] = []
    for entry in mx_equity_selection:
        sym = entry["ticker"]
        try:
            mx_equities.append({"ticker": sym, "source": entry["source"], "quote": _quote_one(sym)})
        except Exception as e:  # noqa: BLE001
            errors.append(f"mx equity {sym}: {e}")

    facts_pack = {
        "generated_at": generated_at,
        "wsj_pages_available": list(wsj_pages.keys()),
        "nyt_pages_available": list(nyt_pages.keys()),
        "int_equities": int_equities,
        "mx_indices": mx_indices,
        "mx_equities": mx_equities,
    }

    compact = _compact_biva_for_llm(wsj_pages, nyt_pages, int_equities, mx_indices, mx_equities)

    biva_summary: dict | None = None
    if os.getenv("OPENAI_API_KEY", "").strip():
        biva_summary, llm_errs = await asyncio.to_thread(_openai_biva_sync, compact)
        errors.extend(llm_errs)

    return {
        "ok": True,
        "correlation_id": correlation_id,
        "snapshot_label": body.snapshot_label,
        "generated_at": generated_at,
        "sources_requested": sources,
        "partial": bool(auth_failures),
        "auth_failures": auth_failures,
        "biva_summary": biva_summary,
        "facts_pack": facts_pack,
        "errors": errors,
        "meta": {
            "browserbase_session_id": last_bb_session_id,
            "live_view_url": live_view_url,
            "source_status": {
                "wsj": "auth_required" if "wsj" in auth_failures else ("ok" if wsj_pages else "skipped"),
                "nyt": "auth_required" if "nyt" in auth_failures else ("ok" if nyt_pages else "skipped"),
                "yahoo": "ok" if mx_indices or int_equities else "error",
            },
        },
    }


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_morning_shot(body: ChatRequest) -> AsyncGenerator[str, None]:
    correlation_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    sources = [s.lower() for s in (_dispatch_intent(body.message)[1].get("sources") or ["wsj", "nyt"]) if s]
    if body.resume_auth:
        sources = ["wsj", "nyt"]

    errors: list[str] = []
    auth_failures: list[str] = []
    live_view_url: str | None = None
    last_bb_session_id: str | None = None

    if not os.getenv("BROWSERBASE_API_KEY", "").strip():
        yield _sse_event("result", {"tool": "morning_shot", "result": {"ok": False, "error": "BROWSERBASE_API_KEY_required"}})
        return

    sites_cfg: list[dict] = []
    wsj_base = os.getenv("WSJ_BASE_URL", "https://www.wsj.com")
    if "wsj" in sources:
        sites_cfg.append({
            "site": "wsj",
            "base_url": wsj_base,
            "probe_url": os.getenv("WSJ_SESSION_PROBE_URL", f"{wsj_base}/signin"),
            "paths": {
                "market_snapshot": os.getenv("WSJ_MARKETS_PATH", "/markets"),
                "top_headlines": os.getenv("WSJ_HEADLINES_PATH", "/news"),
                "economy_policy": os.getenv("WSJ_ECONOMY_PATH", "/economy"),
            },
        })

    nyt_base = os.getenv("NYT_BASE_URL", "https://www.nytimes.com")
    if "nyt" in sources:
        sites_cfg.append({
            "site": "nyt",
            "base_url": nyt_base,
            "probe_url": os.getenv("NYT_SESSION_PROBE_URL", nyt_base),
            "paths": {
                "nyt_world": os.getenv("NYT_WORLD_PATH", "/section/world"),
                "nyt_business": os.getenv("NYT_BUSINESS_PATH", "/section/business"),
            },
        })

    yield _sse_event("step", {"text": "Conectando a Browserbase…"})
    bb_result = await fetch_multi_site_pages_via_browserbase(
        sites=sites_cfg,
        browserbase_session_id=(body.browserbase_session_id or "").strip() or None,
    )

    if not bb_result.get("ok"):
        yield _sse_event(
            "result",
            {
                "tool": "morning_shot",
                "result": {
                    "ok": False,
                    "error": bb_result.get("error", "browserbase_failed"),
                    "detail": bb_result.get("detail"),
                    "browserbase_session_id": bb_result.get("browserbase_session_id"),
                },
            },
        )
        return

    last_bb_session_id = bb_result.get("browserbase_session_id")
    auth_failures = list(bb_result.get("auth_failures") or [])
    live_view_url = bb_result.get("live_view_url")
    pages = bb_result.get("pages") or {}

    if auth_failures and not pages:
        yield _sse_event(
            "auth",
            {
                "site": (body.site or auth_failures[0] if auth_failures else "wsj"),
                "session_id": last_bb_session_id or "",
                "live_view_url": live_view_url or "",
                "message": f"Inicia sesión en {', '.join(s.upper() for s in auth_failures)} en Live View.",
            },
        )
        return

    yield _sse_event("step", {"text": "Extrayendo titulares y datos de mercado…"})
    wsj_pages = {k: v for k, v in pages.items() if v.get("site") == "wsj"}
    nyt_pages = {k: v for k, v in pages.items() if v.get("site") == "nyt"}

    wsj_market_html = str(wsj_pages.get("market_snapshot", {}).get("html", ""))
    int_equity_selection, eq_errs = _select_equities_for_wsj(wsj_market_html)
    errors.extend(eq_errs)

    yield _sse_event("step", {"text": "Consultando cotizaciones internacionales…"})
    int_equities: list[dict] = []
    for entry in int_equity_selection:
        sym = entry["ticker"]
        try:
            int_equities.append({"ticker": sym, "source": entry["source"], "quote": await asyncio.to_thread(_quote_one, sym)})
        except Exception as e:  # noqa: BLE001
            errors.append(f"int quote {sym}: {e}")

    yield _sse_event("step", {"text": "Consultando índices y emisoras MX…"})
    macro_raw = os.getenv("MACRO_YAHOO_TICKERS", "MXN=X,^MXX,^GSPC,^DJI,^IXIC")
    mx_indices: list[dict] = []
    for sym in _parse_env_ticker_csv(macro_raw):
        try:
            mx_indices.append({"ticker": sym, "quote": await asyncio.to_thread(_quote_one, sym)})
        except Exception as e:  # noqa: BLE001
            errors.append(f"mx index {sym}: {e}")

    mx_equity_selection = _select_mx_equities()
    mx_equities: list[dict] = []
    for entry in mx_equity_selection:
        sym = entry["ticker"]
        try:
            mx_equities.append({"ticker": sym, "source": entry["source"], "quote": await asyncio.to_thread(_quote_one, sym)})
        except Exception as e:  # noqa: BLE001
            errors.append(f"mx equity {sym}: {e}")

    facts_pack = {
        "generated_at": generated_at,
        "wsj_pages_available": list(wsj_pages.keys()),
        "nyt_pages_available": list(nyt_pages.keys()),
        "int_equities": int_equities,
        "mx_indices": mx_indices,
        "mx_equities": mx_equities,
    }
    compact = _compact_biva_for_llm(wsj_pages, nyt_pages, int_equities, mx_indices, mx_equities)

    biva_summary: dict | None = None
    if os.getenv("OPENAI_API_KEY", "").strip():
        yield _sse_event("step", {"text": "Generando Morning Shot con OpenAI…"})
        raw = ""
        try:
            async for token in _openai_biva_stream_raw(compact):
                raw += token
                yield _sse_event("chunk", {"chars": len(raw)})
            parsed = json.loads(raw) if raw else None
            if isinstance(parsed, dict):
                biva_summary = _apply_biva_attribution(_normalize_biva_morning_shot(parsed), compact)
            else:
                errors.append("openai: response not an object")
        except json.JSONDecodeError as e:
            errors.append(f"openai json: {e}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"openai: {e}")
    else:
        errors.append("OPENAI_API_KEY not set; biva_summary omitted")

    yield _sse_event(
        "result",
        {
            "tool": "morning_shot",
            "result": {
                "ok": True,
                "correlation_id": correlation_id,
                "snapshot_label": "biva-morning-shot",
                "generated_at": generated_at,
                "sources_requested": sources,
                "partial": bool(auth_failures),
                "auth_failures": auth_failures,
                "biva_summary": biva_summary,
                "facts_pack": facts_pack,
                "errors": errors,
                "meta": {
                    "browserbase_session_id": last_bb_session_id,
                    "live_view_url": live_view_url,
                    "source_status": {
                        "wsj": "auth_required" if "wsj" in auth_failures else ("ok" if wsj_pages else "skipped"),
                        "nyt": "auth_required" if "nyt" in auth_failures else ("ok" if nyt_pages else "skipped"),
                        "yahoo": "ok" if mx_indices or int_equities else "error",
                    },
                },
            },
        },
    )


# ---------------------------------------------------------------------------
# News Summary endpoint
# ---------------------------------------------------------------------------

class NewsSummaryRequest(BaseModel):
    snapshot_label: str = Field(default="news-summary")
    source_ids: list[str] | None = Field(
        default=None,
        description="Subset of NEWS_SOURCES keys; None or empty = all 11 fuentes.",
    )


def _compact_news_for_llm(pages: dict[str, dict]) -> dict:
    result: dict[str, list[dict]] = {}
    for source_id, page in pages.items():
        if not page.get("ok"):
            continue
        cfg = NEWS_SOURCES.get(source_id, {})
        host = cfg.get("host", "")
        base = cfg.get("base_url", "")
        links = _extract_links(
            str(page.get("html", "")),
            limit=10,
            allowed_host=host,
            base_url=base,
        )
        result[source_id] = [{"title": link["title"], "url": link["url"]} for link in links[:8]]
    return {"sources": result}


def _openai_news_summary_sync(compact: dict) -> tuple[dict | None, list[str]]:
    errs: list[str] = []
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        errs.append("OPENAI_API_KEY not set; news classification omitted")
        return None, errs
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    try:
        from openai import OpenAI
    except ImportError as e:
        errs.append(f"openai: {e}")
        return None, errs

    payload = json.dumps(compact, ensure_ascii=False)
    if len(payload) > 48000:
        payload = payload[:48000] + "\n...(truncated)"

    system = (
        "Eres un editor financiero en México. Clasifica titulares en las secciones pedidas. "
        "Responde SOLO con JSON válido, sin Markdown. Usa solo titulares del pack."
    )
    # Estructura alineada con docs/RESUMEN_NOTICIAS_ESTRUCTURA.md (PDF referencia BIVA).
    user = (
        "Pack de titulares por fuente (JSON):\n"
        f"{payload}\n\n"
        "Devuelve JSON con esta estructura exacta:\n"
        "{\n"
        '  "emisoras_locales": {\n'
        '    "capitales": [{"ticker", "headline", "source"}],\n'
        '    "deuda":     [{"ticker", "headline", "source"}]\n'
        "  },\n"
        '  "internacionales_sic": [{"ticker", "headline", "source"}],\n'
        '  "vigilancia": {\n'
        '    "eventos_relevantes":     [{"ticker", "headline", "source"}],\n'
        '    "movimientos_inusitados": [{"ticker", "headline", "source"}]\n'
        "  },\n"
        '  "economia_politica": {\n'
        '    "economia":      [{"headline", "source"}],\n'
        '    "internacional": [{"headline", "source"}],\n'
        '    "mercados":      [{"headline", "source"}],\n'
        '    "crypto":        [{"headline", "source"}]\n'
        "  }\n"
        "}\n"
        "Si un titular no tiene ticker claro, omítelo de las secciones que requieren ticker. "
        "Máximo 4 items por sub-sección. source = id de fuente (ej. 'reuters', 'elfinanciero')."
    )

    try:
        client = OpenAI(api_key=api_key)
        completion = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.25,
            max_tokens=2400,
        )
    except Exception as e:
        errs.append(f"openai: {e}")
        return None, errs

    raw = completion.choices[0].message.content or ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        errs.append(f"openai json: {e}")
        return None, errs
    return parsed if isinstance(parsed, dict) else None, errs


@app.post("/news-summary")
async def news_summary(body: NewsSummaryRequest):
    correlation_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    timeout = float(os.getenv("NEWS_TIMEOUT_SECONDS", "15"))
    errors: list[str] = []

    ids_for_fetch: list[str] | None = None
    if body.source_ids is not None:
        raw = [str(s).strip() for s in body.source_ids if str(s).strip()]
        if raw:
            valid = [s for s in raw if s in NEWS_SOURCES]
            bad = [s for s in raw if s not in NEWS_SOURCES]
            if bad:
                errors.append("unknown_source_ids: " + ", ".join(bad))
            if not valid:
                return {
                    "ok": False,
                    "correlation_id": correlation_id,
                    "snapshot_label": body.snapshot_label,
                    "generated_at": generated_at,
                    "error": "no_valid_source_ids",
                    "errors": errors,
                }
            ids_for_fetch = valid

    pages = await fetch_news_sources(timeout=timeout, source_ids=ids_for_fetch)
    sources_ok = sum(1 for p in pages.values() if p.get("ok"))
    sources_error = sum(1 for p in pages.values() if not p.get("ok"))

    compact = _compact_news_for_llm(pages)
    classification: dict | None = None
    if os.getenv("OPENAI_API_KEY", "").strip():
        classification, llm_errs = await asyncio.to_thread(_openai_news_summary_sync, compact)
        errors.extend(llm_errs)

    asset_raw = os.getenv(
        "NEWS_ASSET_TICKERS",
        "^MXX,^DJI,^GSPC,^IXIC,MXN=X,EURUSD=X,GC=F,SI=F,CL=F,BTC-USD,ETH-USD",
    )
    asset_symbols = _parse_env_ticker_csv(asset_raw)
    activos: list[dict] = []
    for sym in asset_symbols:
        try:
            activos.append({"ticker": sym, "quote": await asyncio.to_thread(_quote_one, sym)})
        except Exception as e:
            errors.append(f"asset {sym}: {e}")

    return {
        "ok": True,
        "correlation_id": correlation_id,
        "snapshot_label": body.snapshot_label,
        "generated_at": generated_at,
        "news_summary": classification,
        "activos": activos,
        "source_stats": {
            "total": len(pages),
            "ok": sources_ok,
            "error": sources_error,
            "errors_by_source": {
                k: v.get("error") for k, v in pages.items() if not v.get("ok")
            },
        },
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = ""
    resume_auth: bool = False
    browserbase_session_id: str | None = None
    site: str | None = None


def _auth_required_response(site: str, result: dict) -> dict:
    return {
        "tool": "morning_shot",
        "auth_required": True,
        "site": site,
        "browserbase_session_id": result.get("browserbase_session_id"),
        "live_view_url": result.get("interactive_live_view_url"),
        "message": result.get("message", ""),
    }


@app.post("/chat")
async def chat(body: ChatRequest):
    if body.resume_auth:
        ms_body = MorningShotRequest(
            browserbase_session_id=(body.browserbase_session_id or "").strip() or None,
        )
        result = await morning_shot(ms_body)
        if result.get("state") == REQUIRES_AUTH_STATE or result.get("error") == "REQUIRES_AUTH":
            return _auth_required_response(body.site or "wsj", result)
        return {"tool": "morning_shot", "result": result}

    tool, params = _dispatch_intent(body.message)

    if tool == "morning_shot":
        sources = params.get("sources") or ["wsj", "nyt"]
        ms_body = MorningShotRequest(
            sources=sources,
            browserbase_session_id=(body.browserbase_session_id or "").strip() or None,
        )
        result = await morning_shot(ms_body)
        if result.get("state") == REQUIRES_AUTH_STATE or result.get("error") == "REQUIRES_AUTH":
            # Prioriza el primer sitio que requirió auth para el pill
            site = (result.get("auth_failures") or ["wsj"])[0]
            return _auth_required_response(site, result)
        return {"tool": "morning_shot", "result": result}

    if tool == "news_summary":
        src = params.get("source_ids")
        result = await news_summary(NewsSummaryRequest(source_ids=src))
        return {"tool": "news_summary", "result": result}

    if tool == "price_chart":
        ticker = (params.get("ticker") or "").strip().upper()
        period = str(params.get("period") or "3mo")
        if ticker:
            try:
                chart = await asyncio.to_thread(_price_chart_sync, ticker, period)
                return {"tool": "price_chart", "result": chart}
            except Exception as e:  # noqa: BLE001
                return {
                    "tool": "price_chart",
                    "result": {"ok": False, "error": str(e), "Ticker": ticker, "period": period},
                }

    if tool == "quote":
        ticker = params.get("ticker", "")
        if ticker:
            try:
                quote = await asyncio.to_thread(_quote_one, ticker)
                return {"tool": "quote", "result": quote}
            except Exception as e:  # noqa: BLE001
                return {"tool": "quote", "result": {"ok": False, "error": str(e), "Ticker": ticker}}

    return {
        "tool": "none",
        "message": (
            "Puedo generar el morning shot (WSJ + NYT), el resumen de noticias (todas las fuentes o una en concreto), "
            "cotización, gráfico de precio o detalles de un activo. "
            "Prueba: 'morning shot', 'resumen de noticias de Reuters', 'cotización de AMXL.MX', "
            "'necesito ver un gráfico de VOO', 'detalles de NVDA'."
        ),
    }


async def _stream_followup_tools(tool: str, params: dict) -> AsyncGenerator[str, None]:
    """SSE para noticias, cotización, gráfica y fallback (sin morning shot)."""
    try:
        if tool == "news_summary":
            yield _sse_event("step", {"text": "Consultando fuentes de noticias…"})
            result = await news_summary(NewsSummaryRequest(source_ids=params.get("source_ids")))
            yield _sse_event("result", {"tool": "news_summary", "result": result})
            return

        if tool == "price_chart":
            ticker = (params.get("ticker") or "").strip().upper()
            period = str(params.get("period") or "3mo")
            if ticker:
                yield _sse_event("step", {"text": "Consultando precios e histórico…"})
                try:
                    chart = await asyncio.to_thread(_price_chart_sync, ticker, period)
                    yield _sse_event("result", {"tool": "price_chart", "result": chart})
                except Exception as e:  # noqa: BLE001
                    yield _sse_event(
                        "result",
                        {
                            "tool": "price_chart",
                            "result": {"ok": False, "error": str(e), "Ticker": ticker, "period": period},
                        },
                    )
                return

        if tool == "quote":
            ticker = params.get("ticker", "")
            if ticker:
                yield _sse_event("step", {"text": "Consultando cotización…"})
                try:
                    quote = await asyncio.to_thread(_quote_one, ticker)
                    yield _sse_event("result", {"tool": "quote", "result": quote})
                except Exception as e:  # noqa: BLE001
                    yield _sse_event(
                        "result",
                        {"tool": "quote", "result": {"ok": False, "error": str(e), "Ticker": ticker}},
                    )
                return

        yield _sse_event(
            "result",
            {
                "tool": "none",
                "message": (
                    "Puedo generar el morning shot (WSJ + NYT), el resumen de noticias (todas las fuentes o una en concreto), "
                    "cotización, gráfico de precio o detalles de un activo. "
                    "Prueba: 'morning shot', 'resumen de noticias de Reuters', 'cotización de AMXL.MX', "
                    "'necesito ver un gráfico de VOO', 'detalles de NVDA'."
                ),
            },
        )
    except Exception as e:  # noqa: BLE001
        yield _sse_event("error", {"text": str(e)})


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest):
    if body.resume_auth:
        return StreamingResponse(
            _stream_morning_shot(body),
            media_type="text/event-stream",
            headers=dict(_SSE_HEADERS),
        )
    tool, params = _dispatch_intent(body.message)
    if tool == "morning_shot":
        return StreamingResponse(
            _stream_morning_shot(body),
            media_type="text/event-stream",
            headers=dict(_SSE_HEADERS),
        )
    return StreamingResponse(
        _stream_followup_tools(tool, params),
        media_type="text/event-stream",
        headers=dict(_SSE_HEADERS),
    )
