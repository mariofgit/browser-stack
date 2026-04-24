"""Finance agent — WSJ morning shot via Browserbase scraping (no NAP audit, no SDR/CRM)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from html import unescape
from datetime import datetime, timezone

import httpx
import yfinance as yf
from fastapi import FastAPI
from pydantic import BaseModel, Field

from runtime.browserbase_wsj import REQUIRES_AUTH_STATE, fetch_wsj_pages_via_browserbase

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


def _extract_links(html: str, *, limit: int = 15) -> list[dict]:
    links: list[dict] = []
    seen: set[str] = set()
    for match in re.finditer(r"<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", html, re.IGNORECASE | re.DOTALL):
        href = match.group(1).strip()
        title = _clean_text(match.group(2))
        if not title or len(title) < 20:
            continue
        if href.startswith("/"):
            href = f"https://www.wsj.com{href}"
        if "wsj.com" not in href:
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
