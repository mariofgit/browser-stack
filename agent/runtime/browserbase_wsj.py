"""WSJ HTML fetch via Browserbase + Playwright (replaces Docker mini-VM + in-memory cookie vault)."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from browserbase import Browserbase

REQUIRES_AUTH_STATE = "REQUIRES_AUTH"


def _wsj_force_requires_auth_enabled() -> bool:
    """
    Live View testing override: load a seed URL in the session, then return REQUIRES_AUTH + Live View.

    Without a ``goto``, the remote tab stays on ``about:blank`` and the embedded debugger looks empty.

    ``WSJ_FORCE_REQUIRES_AUTH`` (case-insensitive, trimmed):

    - **Unset or empty** — normal POC (heuristic paywall detection).
    - **true, 1, yes, on** — navigate to ``WSJ_LIVE_VIEW_SEED_URL`` or ``WSJ_BASE_URL``, then Live View URL.
    - **false, 0, no, off** — explicit normal POC (same as unset).
    - **Any other value** — treated as off (safe default).
    """
    raw = os.getenv("WSJ_FORCE_REQUIRES_AUTH", "").strip().lower()
    if not raw:
        return False
    if raw in ("0", "false", "no", "off"):
        return False
    return raw in ("1", "true", "yes", "on")


def _wsj_session_probe_first_enabled() -> bool:
    """
    When True (default), navigate to the session probe URL before /markets · /news · /economy.

    Set ``WSJ_SESSION_PROBE_FIRST=false`` to restore the older "sections only" order.
    """
    raw = os.getenv("WSJ_SESSION_PROBE_FIRST", "true").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def _bb() -> Browserbase:
    key = os.getenv("BROWSERBASE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("BROWSERBASE_API_KEY is not set")
    return Browserbase(api_key=key)


def _nyt_page_requires_auth(*, url: str, html_sample: str) -> bool:
    u = url.lower()
    if (
        u.startswith("https://myaccount.nytimes.com/auth/login?response_type=cookie")
        or "myaccount.nytimes.com/auth/login" in u
        or "myaccount.nytimes" in u
        or "/login" in u
        or "/auth/login" in u
        or "/subscription" in u
        or "myaccount" in u
        or "response_type=cookie" in u
    ):
        return True
    h = html_sample[:48000].lower()
    if "myaccount.nytimes.com/auth/login" in h or "response_type=cookie" in h[:12000]:
        return True
    if re.search(r">\s*log\s+in\s*<", h) or re.search(r'aria-label=["\']log\s+in["\']', h):
        return True
    if "log in to the new york times" in h or "create a free account" in h[:8000]:
        return True
    if "subscribe to the times" in h and "already a subscriber" in h:
        return True
    if "you have reached your limit of free articles" in h:
        return True
    if "access is temporarily restricted" in h:
        return True
    return False


def page_requires_auth(*, url: str, html_sample: str, site: str = "wsj") -> bool:
    """Dispatch to per-site heuristic. Supports 'wsj' and 'nyt'."""
    if site == "nyt":
        return _nyt_page_requires_auth(url=url, html_sample=html_sample)
    return wsj_page_requires_auth(url=url, html_sample=html_sample)


def wsj_page_requires_auth(*, url: str, html_sample: str) -> bool:
    """
    Heuristic: pages where automated fetch should stop and Live View is needed.

    Covers subscriber paywall/login, anti-bot / rate limits, device verification, and hard 404s.
    Agent never handles credentials; user uses Browserbase Live View if this returns True.
    """
    u = url.lower()
    # WSJ SSO lives on Dow Jones hosts, not only accounts.wsj.com.
    if (
        "accounts.wsj" in u
        or "accounts.dowjones" in u
        or "sso.accounts.dowjones" in u
        or "signin" in u
        or "/login" in u
    ):
        return True
    # Large slice: bot/verification copy is often below the fold in heavy HTML shells.
    h = html_sample[:48000].lower()
    if "sign in to the wall street journal" in h or "sign in to wsj" in h:
        return True
    if "subscribe" in h and ("sign in" in h[:6000] or "already a subscriber" in h):
        return True
    if "subscriber-only" in h or "subscriber only" in h:
        return True
    # Anti-bot / throttling (often blocks datacenter IPs e.g. Browserbase).
    if "access is temporarily restricted" in h:
        return True
    if "we detected unusual activity" in h or "unusual activity from your device" in h:
        return True
    if "automated (bot) activity" in h or "automated bot activity" in h:
        return True
    # Device / JS verification interstitial.
    if "verifying the device" in h:
        return True
    if "the requested content will be available after verification" in h:
        return True
    # Hard 404 — HTML may still feed extractors with unrelated snippets if not caught.
    if (
        "we can't find the page you're looking for" in h
        or "we can't find the page you\u2019re looking for" in h
        or "we can\u2019t find the page you\u2019re looking for" in h
    ):
        return True
    if "page not found" in h and "404" in h:
        return True
    return False


async def _run_sync(fn):
    return await asyncio.to_thread(fn)


async def fetch_wsj_pages_via_browserbase(
    *,
    client_key: str,
    requested_sections: list[str],
    browserbase_session_id: str | None,
    base_url: str,
    paths: dict[str, str],
) -> dict[str, Any]:
    """
    Returns:
      - { ok: True, pages: {section: {url, status_code, ok, requires_login, html}}, browserbase_session_id }
      - { ok: False, state: REQUIRES_AUTH, browserbase_session_id, interactive_live_view_url, message }
      - { ok: False, error: str, ... }
    """
    _ = client_key
    bb = _bb()
    project_id = os.getenv("BROWSERBASE_PROJECT_ID", "").strip()
    timeout_s = int(os.getenv("BROWSERBASE_SESSION_TIMEOUT_SECONDS", "3600"))

    session_id: str
    connect_url: str

    try:
        if browserbase_session_id:
            retrieved = await _run_sync(lambda: bb.sessions.retrieve(browserbase_session_id))
            if retrieved.status in ("COMPLETED", "ERROR", "TIMED_OUT"):
                return {
                    "ok": False,
                    "error": "browserbase_session_not_active",
                    "browserbase_status": retrieved.status,
                }
            session_id = retrieved.id
            connect_url = retrieved.connect_url
        else:
            if not project_id:
                return {
                    "ok": False,
                    "error": "BROWSERBASE_PROJECT_ID_required",
                    "hint": "Set BROWSERBASE_PROJECT_ID in agents/.env (Browserbase project id).",
                }
            # persistCookies: project default persistent context (no explicit context.id).
            browser_settings: dict[str, Any] = {"persistCookies": True}
            create_kwargs: dict[str, Any] = {
                "project_id": project_id,
                "keep_alive": True,
                "api_timeout": timeout_s,
                "browser_settings": browser_settings,
            }
            created = await _run_sync(lambda: bb.sessions.create(**create_kwargs))
            session_id = created.id
            connect_url = created.connect_url
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "browserbase_session_create_failed", "detail": str(e)}

    if _wsj_force_requires_auth_enabled():
        from playwright.async_api import async_playwright

        nav_timeout_ms = int(float(os.getenv("WSJ_BROWSERBASE_GOTO_TIMEOUT_SECONDS", "45")) * 1000)
        post_wait = float(os.getenv("WSJ_BROWSERBASE_POST_GOTO_WAIT_SECONDS", "1.5"))
        # Do not embed Dow Jones SSO URLs with ?nonce=&state= — they expire. Use /signin or home; site redirects.
        seed_url = os.getenv("WSJ_LIVE_VIEW_SEED_URL", "").strip() or base_url.rstrip("/")
        nav_error: str | None = None
        fullscreen: str | None = None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(connect_url)
                try:
                    if not browser.contexts:
                        return {"ok": False, "error": "no_browser_context", "browserbase_session_id": session_id}
                    context = browser.contexts[0]
                    page = context.pages[0] if context.pages else await context.new_page()
                    try:
                        await page.goto(seed_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                        await asyncio.sleep(post_wait)
                    except Exception as e:  # noqa: BLE001
                        nav_error = str(e)
                    live = await _run_sync(lambda: bb.sessions.debug(session_id))
                    fullscreen = live.debugger_fullscreen_url
                finally:
                    try:
                        await browser.close()
                    except Exception:
                        pass
        except Exception as e:  # noqa: BLE001
            nav_error = (nav_error + "; " if nav_error else "") + str(e)

        if not fullscreen:
            try:
                live = await _run_sync(lambda: bb.sessions.debug(session_id))
                fullscreen = live.debugger_fullscreen_url
            except Exception as e:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": "browserbase_force_requires_auth_failed",
                    "detail": str(e),
                    "seed_url": seed_url,
                    "seed_navigation_error": nav_error,
                }

        msg = (
            "WSJ_FORCE_REQUIRES_AUTH: Live View after seed navigation. "
            "Sign in or complete verification in the embedded view, then Continue. "
            "Set WSJ_FORCE_REQUIRES_AUTH=false to restore normal POC."
        )
        if nav_error:
            msg += f" (navigation note: {nav_error})"

        return {
            "ok": False,
            "state": REQUIRES_AUTH_STATE,
            "browserbase_session_id": session_id,
            "interactive_live_view_url": fullscreen,
            "message": msg,
            "seed_url": seed_url,
            "seed_navigation_error": nav_error,
        }

    from playwright.async_api import async_playwright

    nav_timeout_ms = int(float(os.getenv("WSJ_BROWSERBASE_GOTO_TIMEOUT_SECONDS", "45")) * 1000)
    post_wait = float(os.getenv("WSJ_BROWSERBASE_POST_GOTO_WAIT_SECONDS", "1.5"))

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(connect_url)
            try:
                if not browser.contexts:
                    return {"ok": False, "error": "no_browser_context", "browserbase_session_id": session_id}
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()

                pages_out: dict[str, dict[str, Any]] = {}
                base = base_url.rstrip("/")

                if _wsj_session_probe_first_enabled():
                    probe_url = os.getenv("WSJ_SESSION_PROBE_URL", "").strip() or f"{base}/signin"
                    await page.goto(probe_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                    await asyncio.sleep(post_wait)
                    html_probe = await page.content()
                    url_probe = page.url
                    if wsj_page_requires_auth(url=url_probe, html_sample=html_probe):
                        live = await _run_sync(lambda: bb.sessions.debug(session_id))
                        fullscreen = live.debugger_fullscreen_url
                        await browser.close()
                        return {
                            "ok": False,
                            "state": REQUIRES_AUTH_STATE,
                            "browserbase_session_id": session_id,
                            "interactive_live_view_url": fullscreen,
                            "message": (
                                "Session probe indicated login, SSO, or blocked state before scraping sections. "
                                "Use Live View to sign in or pass verification, then retry with browserbase_session_id."
                            ),
                            "probe_url": probe_url,
                            "probe_final_url": url_probe,
                        }

                for section in requested_sections:
                    path = paths.get(section)
                    if not path:
                        continue
                    target = f"{base}/{path.lstrip('/')}"
                    await page.goto(target, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                    await asyncio.sleep(post_wait)
                    html = await page.content()
                    url = page.url
                    if wsj_page_requires_auth(url=url, html_sample=html):
                        live = await _run_sync(lambda: bb.sessions.debug(session_id))
                        fullscreen = live.debugger_fullscreen_url
                        await browser.close()
                        return {
                            "ok": False,
                            "state": REQUIRES_AUTH_STATE,
                            "browserbase_session_id": session_id,
                            "interactive_live_view_url": fullscreen,
                            "message": (
                                "WSJ page needs human handling in Live View (login, verification, or blocked session). "
                                "Then run morning shot again with browserbase_session_id."
                            ),
                        }
                    pages_out[section] = {
                        "url": url,
                        "status_code": 200,
                        "ok": True,
                        "requires_login": False,
                        "html": html,
                    }

                await browser.close()
            except Exception:
                await browser.close()
                raise

        await _run_sync(lambda: bb.sessions.update(session_id, status="REQUEST_RELEASE"))
        return {"ok": True, "pages": pages_out, "browserbase_session_id": session_id}
    except Exception as e:  # noqa: BLE001
        try:
            await _run_sync(lambda: bb.sessions.update(session_id, status="REQUEST_RELEASE"))
        except Exception:
            pass
        return {"ok": False, "error": "browserbase_playwright_failed", "detail": str(e)}


async def fetch_multi_site_pages_via_browserbase(
    *,
    sites: list[dict],
    browserbase_session_id: str | None,
) -> dict[str, Any]:
    """
    Navegar múltiples sitios (WSJ + NYT) en una sola sesión Browserbase.

    Args:
        sites: lista de dicts {site: "wsj"|"nyt", base_url, probe_url, paths: {section: path}}

    Returns:
        {
            ok: True,
            pages: {section: {...}},          # agregado de todas las secciones
            auth_failures: ["wsj"] | [],      # sitios que requirieron auth
            partial: bool,                    # True si algún sitio requirió auth (aunque no haya HTML)
            live_view_url: str | None,        # de la primera falla de auth
            browserbase_session_id: str
        }
    """
    bb = _bb()
    project_id = os.getenv("BROWSERBASE_PROJECT_ID", "").strip()
    timeout_s = int(os.getenv("BROWSERBASE_SESSION_TIMEOUT_SECONDS", "3600"))

    try:
        if browserbase_session_id:
            retrieved = await _run_sync(lambda: bb.sessions.retrieve(browserbase_session_id))
            if retrieved.status in ("COMPLETED", "ERROR", "TIMED_OUT"):
                return {
                    "ok": False,
                    "error": "browserbase_session_not_active",
                    "browserbase_status": retrieved.status,
                }
            session_id = retrieved.id
            connect_url = retrieved.connect_url
        else:
            if not project_id:
                return {"ok": False, "error": "BROWSERBASE_PROJECT_ID_required"}
            created = await _run_sync(
                lambda: bb.sessions.create(
                    project_id=project_id,
                    keep_alive=True,
                    api_timeout=timeout_s,
                    browser_settings={"persistCookies": True},
                )
            )
            session_id = created.id
            connect_url = created.connect_url
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "browserbase_session_create_failed", "detail": str(e)}

    from playwright.async_api import async_playwright

    nav_timeout_ms = int(float(os.getenv("WSJ_BROWSERBASE_GOTO_TIMEOUT_SECONDS", "45")) * 1000)
    post_wait = float(os.getenv("WSJ_BROWSERBASE_POST_GOTO_WAIT_SECONDS", "1.5"))

    pages_out: dict[str, dict[str, Any]] = {}
    auth_failures: list[str] = []
    live_view_url: str | None = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(connect_url)
            try:
                if not browser.contexts:
                    return {"ok": False, "error": "no_browser_context", "browserbase_session_id": session_id}
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()

                for site_cfg in sites:
                    site_id = site_cfg["site"]
                    base = site_cfg["base_url"].rstrip("/")
                    probe_url = site_cfg.get("probe_url") or base
                    site_paths = site_cfg.get("paths") or {}

                    try:
                        await page.goto(probe_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                        await asyncio.sleep(post_wait)
                        html_probe = await page.content()
                        if page_requires_auth(url=page.url, html_sample=html_probe, site=site_id):
                            auth_failures.append(site_id)
                            if not live_view_url:
                                live = await _run_sync(lambda: bb.sessions.debug(session_id))
                                live_view_url = live.debugger_fullscreen_url
                            # No navegar al siguiente sitio: el tab debe quedarse en la pantalla
                            # de login (p. ej. WSJ) para que Live View sirva al usuario.
                            break

                        site_halted_for_auth = False
                        for section, path in site_paths.items():
                            target = f"{base}/{path.lstrip('/')}"
                            try:
                                await page.goto(target, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                                await asyncio.sleep(post_wait)
                                html = await page.content()
                                if page_requires_auth(url=page.url, html_sample=html, site=site_id):
                                    if site_id not in auth_failures:
                                        auth_failures.append(site_id)
                                    if not live_view_url:
                                        live = await _run_sync(lambda: bb.sessions.debug(session_id))
                                        live_view_url = live.debugger_fullscreen_url
                                    site_halted_for_auth = True
                                    break
                                pages_out[section] = {
                                    "url": page.url,
                                    "status_code": 200,
                                    "ok": True,
                                    "requires_login": False,
                                    "html": html,
                                    "site": site_id,
                                }
                            except Exception as e:  # noqa: BLE001
                                pages_out[section] = {"ok": False, "error": str(e), "site": site_id}

                        if site_halted_for_auth:
                            break
                    except Exception as e:  # noqa: BLE001
                        auth_failures.append(site_id)
                        pages_out[f"{site_id}_error"] = {"ok": False, "error": str(e), "site": site_id}

                await browser.close()
            except Exception:
                await browser.close()
                raise

        if not auth_failures:
            await _run_sync(lambda: bb.sessions.update(session_id, status="REQUEST_RELEASE"))
        return {
            "ok": True,
            "pages": pages_out,
            "auth_failures": auth_failures,
            "partial": bool(auth_failures),
            "live_view_url": live_view_url,
            "browserbase_session_id": session_id,
        }
    except Exception as e:  # noqa: BLE001
        try:
            await _run_sync(lambda: bb.sessions.update(session_id, status="REQUEST_RELEASE"))
        except Exception:
            pass
        return {"ok": False, "error": "browserbase_multi_failed", "detail": str(e)}
