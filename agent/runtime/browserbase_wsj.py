"""WSJ HTML fetch via Browserbase + Playwright (replaces Docker mini-VM + in-memory cookie vault)."""

from __future__ import annotations

import asyncio
import os
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
