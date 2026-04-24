"use client";

import { useEffect, useState } from "react";
import type { ReactNode } from "react";

type JsonValue = Record<string, unknown> | Array<unknown> | string | number | boolean | null;

type MorningQuote = {
  Ticker: string;
  name?: string;
  last_close?: string;
  prev_close?: string;
  change_pct?: string;
  ok?: boolean;
  error?: string;
};

type WsjItem = {
  title?: string;
  url?: string;
  instrument?: string;
  value?: string;
  change?: string | null;
  as_of?: string;
};

type WsjEquityFact = {
  ticker?: string;
  source?: string;
  quote?: MorningQuote;
};

type StructuredSummary = {
  disclaimer?: string;
  sections: Array<{ id: string; title: string; bullets: string[] }>;
  watch_today: string[];
};

const RUN_STEPS = [
  "Connecting to Browserbase and Playwright…",
  "Resuming or creating cloud browser session…",
  "Session probe (sign-in / SSO) if enabled…",
  "Navigating WSJ (markets, headlines, economy, finance)…",
  "Extracting HTML and Yahoo quotes…",
  "Building morning shot (optional OpenAI summary)…",
] as const;

function CollapsibleSection({
  title,
  defaultOpen = true,
  children,
  className,
}: {
  title: string;
  defaultOpen?: boolean;
  children: ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className={className ? `card ${className}` : "card"}>
      <div className="card-header">
        <h2>{title}</h2>
        <button
          type="button"
          className="icon-toggle"
          onClick={() => setOpen((v) => !v)}
          aria-label={open ? `Collapse ${title}` : `Expand ${title}`}
          title={open ? "Collapse" : "Expand"}
        >
          {open ? "▾" : "▸"}
        </button>
      </div>
      {open ? children : null}
    </section>
  );
}

export default function Page() {
  const [wsjShotLog, setWsjShotLog] = useState<JsonValue>(null);
  const [wsjBrowserbaseSessionId, setWsjBrowserbaseSessionId] = useState("");
  const [wsjLiveViewUrl, setWsjLiveViewUrl] = useState("");
  const [wsjLoading, setWsjLoading] = useState(false);
  const [wsjBlockingError, setWsjBlockingError] = useState("");
  const [wsjRunStepIndex, setWsjRunStepIndex] = useState(0);
  const [wsjAwaitingSignIn, setWsjAwaitingSignIn] = useState(false);

  useEffect(() => {
    if (!wsjLoading) {
      setWsjRunStepIndex(0);
      return;
    }
    const t = window.setInterval(() => {
      setWsjRunStepIndex((i) => (i + 1) % RUN_STEPS.length);
    }, 2200);
    return () => window.clearInterval(t);
  }, [wsjLoading]);

  const parsedWsjData = (() => {
    if (!wsjShotLog || typeof wsjShotLog !== "object" || !("response" in wsjShotLog)) return null;
    const response = (wsjShotLog as Record<string, unknown>).response;
    if (!response || typeof response !== "object" || !("data" in response)) return null;
    const data = (response as Record<string, unknown>).data;
    if (!data || typeof data !== "object") return null;
    return data as Record<string, unknown>;
  })();

  const wsjHeadlines = Array.isArray(parsedWsjData?.top_headlines) ? (parsedWsjData?.top_headlines as WsjItem[]) : [];
  const wsjEconomy = Array.isArray(parsedWsjData?.economy_policy) ? (parsedWsjData?.economy_policy as WsjItem[]) : [];
  const wsjMarket = Array.isArray(parsedWsjData?.market_snapshot) ? (parsedWsjData?.market_snapshot as WsjItem[]) : [];
  const wsjFinance = Array.isArray(parsedWsjData?.wsj_finance) ? (parsedWsjData?.wsj_finance as WsjItem[]) : [];

  const wsjFactsPack =
    parsedWsjData && typeof parsedWsjData.facts_pack === "object" && parsedWsjData.facts_pack !== null
      ? (parsedWsjData.facts_pack as Record<string, unknown>)
      : null;
  const wsjEquityFacts = Array.isArray(wsjFactsPack?.equities) ? (wsjFactsPack.equities as WsjEquityFact[]) : [];
  const wsjMacroFacts = Array.isArray(wsjFactsPack?.macro_indices)
    ? (wsjFactsPack.macro_indices as Array<{ ticker?: string; quote?: MorningQuote }>)
    : [];

  const wsjStructured: StructuredSummary | null = (() => {
    const s = parsedWsjData?.structured_summary;
    if (!s || typeof s !== "object") return null;
    const o = s as Record<string, unknown>;
    const rawSections = Array.isArray(o.sections) ? o.sections : [];
    const sections = rawSections
      .map((x) => {
        if (typeof x !== "object" || x === null) return null;
        const r = x as Record<string, unknown>;
        const bullets = Array.isArray(r.bullets)
          ? (r.bullets as unknown[]).filter((b): b is string => typeof b === "string")
          : [];
        return { id: String(r.id ?? ""), title: String(r.title ?? r.id ?? ""), bullets };
      })
      .filter(
        (x): x is { id: string; title: string; bullets: string[] } =>
          x !== null && x.id.length > 0 && x.bullets.length > 0,
      );
    const watch = Array.isArray(o.watch_today)
      ? (o.watch_today as unknown[]).filter((w): w is string => typeof w === "string")
      : [];
    return {
      disclaimer: typeof o.disclaimer === "string" ? o.disclaimer : undefined,
      sections,
      watch_today: watch,
    };
  })();

  const runWsjMorningShot = async () => {
    setWsjLoading(true);
    setWsjBlockingError("");
    const sid = wsjBrowserbaseSessionId.trim();
    const requestParams: Record<string, unknown> = {
      snapshot_label: "ui-wsj-morning-shot",
      sections: ["market_snapshot", "top_headlines", "economy_policy", "wsj_finance"],
    };
    if (sid) requestParams.browserbase_session_id = sid;

    try {
      const res = await fetch("/api/wsj-morning-shot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestParams),
      });
      const json = (await res.json()) as Record<string, unknown>;
      setWsjShotLog({ requestParams, response: json });
      const data = (json?.data ?? {}) as Record<string, unknown>;
      const napHttpOk = typeof json?.ok === "boolean" ? json.ok : res.ok;

      if (data.state === "REQUIRES_AUTH" || data.error === "REQUIRES_AUTH") {
        if (typeof data.browserbase_session_id === "string") setWsjBrowserbaseSessionId(data.browserbase_session_id);
        if (typeof data.interactive_live_view_url === "string") setWsjLiveViewUrl(data.interactive_live_view_url);
        setWsjBlockingError("");
        setWsjAwaitingSignIn(true);
      } else if (data.ok === true) {
        setWsjLiveViewUrl("");
        setWsjBrowserbaseSessionId("");
        setWsjBlockingError("");
        setWsjAwaitingSignIn(false);
      } else {
        const parts: string[] = [];
        if (!napHttpOk) parts.push(`Agent HTTP error (ok=${String(json.ok)})`);
        if (typeof data.error === "string") parts.push(data.error);
        if (typeof data.detail === "string") parts.push(data.detail);
        if (typeof data.message === "string") parts.push(data.message);
        setWsjBlockingError(parts.length > 0 ? parts.join(" — ") : `Unexpected response: ${JSON.stringify(data)}`);
      }
    } finally {
      setWsjLoading(false);
    }
  };

  return (
    <>
      <main>
        <h1 style={{ marginBottom: 20 }}>WSJ Morning Shot</h1>
        <div className="wsj-shot-shell">
          <CollapsibleSection title="WSJ morning shot (risk summary + market data)" className="wsj-shot-panel" defaultOpen>
            {wsjLoading ? (
              <p
                style={{
                  marginTop: 0,
                  padding: "10px 12px",
                  borderRadius: 6,
                  background: "rgba(59, 130, 246, 0.12)",
                  border: "1px solid rgba(59, 130, 246, 0.35)",
                  fontWeight: 600,
                  lineHeight: 1.45,
                }}
                aria-live="polite"
              >
                {RUN_STEPS[wsjRunStepIndex]}
              </p>
            ) : wsjAwaitingSignIn ? (
              <p style={{ marginTop: 0, opacity: 0.85 }}>
                Complete WSJ sign-in or verification in the Live View. When signed in, press{" "}
                <strong>Run morning shot</strong> below to fetch content with this Browserbase session.
                Alternatively set <code>WSJ_SESSION_COOKIE</code> in the agent for HTTP-only fetch without Browserbase.
              </p>
            ) : (
              <p style={{ marginTop: 0, opacity: 0.85 }}>
                WSJ HTML via <strong>Browserbase</strong> (Playwright + persisted context). Yahoo quotes and optional
                OpenAI summary. Press <strong>Run morning shot</strong> once: if a subscriber session is already active,
                the shot runs immediately. Otherwise use the embedded Live View to sign in, then press the button again.
                Alternatively set <code>WSJ_SESSION_COOKIE</code> in the agent for legacy HTTP-only fetch.
              </p>
            )}

            <div style={{ display: "grid", gap: 8, marginBottom: 10 }}>
              <button
                type="button"
                onClick={() => void runWsjMorningShot()}
                disabled={wsjLoading || wsjAwaitingSignIn}
                title={wsjAwaitingSignIn ? "Use the button below the Live View after you are signed in" : undefined}
              >
                {wsjAwaitingSignIn ? "Sign in to continue" : wsjLoading ? "Running morning shot…" : "Run morning shot"}
              </button>

              {wsjLiveViewUrl && wsjAwaitingSignIn ? (
                <div style={{ display: "grid", gap: 8 }}>
                  <p style={{ margin: 0, opacity: 0.9 }}>
                    Sign in to WSJ in the embedded view (or{" "}
                    <a href={wsjLiveViewUrl} target="_blank" rel="noreferrer">open in a new tab</a>
                    ). When fully signed in, press <strong>Run morning shot</strong> below.
                    Session: <code>{wsjBrowserbaseSessionId || "—"}</code>
                  </p>
                  <iframe
                    title="Browserbase Live View"
                    src={wsjLiveViewUrl}
                    sandbox="allow-same-origin allow-scripts allow-forms"
                    allow="clipboard-read; clipboard-write"
                    style={{ width: "100%", height: 520, border: "1px solid #ccc", borderRadius: 4 }}
                  />
                  <button
                    type="button"
                    onClick={() => void runWsjMorningShot()}
                    disabled={wsjLoading || !wsjBrowserbaseSessionId}
                  >
                    {wsjLoading ? "Running morning shot…" : "Run morning shot"}
                  </button>
                </div>
              ) : null}
            </div>

            {wsjBlockingError ? (
              <p className="tone-warn" style={{ marginTop: 0 }}>{wsjBlockingError}</p>
            ) : null}

            {parsedWsjData ? (
              <div className="event-list" style={{ marginTop: 12 }}>
                <div
                  className={`event-row ${
                    parsedWsjData.state === "REQUIRES_AUTH" || parsedWsjData.error === "REQUIRES_AUTH"
                      ? "tone-warn"
                      : parsedWsjData.login_required
                        ? "tone-warn"
                        : "tone-info"
                  }`}
                >
                  <span className="pill">source: {String(parsedWsjData.source ?? "wsj")}</span>
                  <span className="pill">headlines: {wsjHeadlines.length}</span>
                  <span className="pill">economy: {wsjEconomy.length}</span>
                  <span className="pill">finance: {wsjFinance.length}</span>
                  <span className="pill">equities: {wsjEquityFacts.length}</span>
                  <span className="event-id">
                    {parsedWsjData.state === "REQUIRES_AUTH" || parsedWsjData.error === "REQUIRES_AUTH"
                      ? "REQUIRES_AUTH — sign in in Live View, then Run morning shot below"
                      : parsedWsjData.login_required
                        ? "login or session required"
                        : "session ok"}
                  </span>
                </div>
              </div>
            ) : null}

            {wsjStructured && (wsjStructured.sections.length > 0 || wsjStructured.watch_today.length > 0) ? (
              <div className="wsj-summary">
                {wsjStructured.disclaimer ? <p className="wsj-disclaimer">{wsjStructured.disclaimer}</p> : null}
                {wsjStructured.sections.map((sec) => (
                  <div key={sec.id}>
                    <h3>{sec.title}</h3>
                    <ul>
                      {sec.bullets.map((b, i) => <li key={`${sec.id}-${i}`}>{b}</li>)}
                    </ul>
                  </div>
                ))}
                {wsjStructured.watch_today.length > 0 ? (
                  <div className="wsj-watch">
                    <strong>Watch today</strong>
                    <ul>
                      {wsjStructured.watch_today.map((w, i) => <li key={`watch-${i}`}>{w}</li>)}
                    </ul>
                  </div>
                ) : null}
              </div>
            ) : parsedWsjData && !(parsedWsjData.login_required === true) ? (
              <p style={{ opacity: 0.85 }}>
                No structured summary yet. Check <code>OPENAI_API_KEY</code> or the raw JSON below.
              </p>
            ) : null}

            {parsedWsjData ? (
              <>
                <details className="wsj-source-panel">
                  <summary style={{ cursor: "pointer", fontWeight: 600 }}>Source data (WSJ + quotes)</summary>
                  {wsjMarket.length > 0 ? (
                    <div className="quotes-grid">
                      {wsjMarket.map((m, idx) => (
                        <article className="quote-card" key={`${m.instrument ?? "market"}-${idx}`}>
                          <div className="quote-head">
                            <span className="hl-ticker">{m.instrument ?? "-"}</span>
                            <span className="hl-name">{m.value ?? "-"}</span>
                          </div>
                          <div className="quote-metrics">
                            <div>Change: {m.change ?? "-"}</div>
                            <div>As of: {m.as_of ?? "-"}</div>
                          </div>
                        </article>
                      ))}
                    </div>
                  ) : null}
                  {wsjEquityFacts.length > 0 ? (
                    <div className="quotes-grid">
                      {wsjEquityFacts.map((e) => {
                        const q = e.quote;
                        return (
                          <article className="quote-card" key={e.ticker ?? "eq"}>
                            <div className="quote-head">
                              <span className="hl-ticker">{e.ticker ?? "-"}</span>
                              <span className="hl-name">{q?.name ?? q?.Ticker ?? "-"}</span>
                              <span className="pill">{e.source ?? "-"}</span>
                            </div>
                            {q?.ok === false ? (
                              <div className="tone-warn">Error: {q.error}</div>
                            ) : (
                              <div className="quote-metrics">
                                <div>Last close: {q?.last_close ?? "-"}</div>
                                <div>Change: {q?.change_pct ?? "-"}</div>
                              </div>
                            )}
                          </article>
                        );
                      })}
                    </div>
                  ) : null}
                  {wsjMacroFacts.length > 0 ? (
                    <div className="quotes-grid">
                      {wsjMacroFacts.map((m) => {
                        const q = m.quote;
                        const sym = m.ticker ?? "-";
                        return (
                          <article className="quote-card" key={sym}>
                            <div className="quote-head">
                              <span className="hl-ticker">{sym}</span>
                              <span className="hl-name">{q?.name ?? "macro"}</span>
                            </div>
                            {q?.ok === false ? (
                              <div className="tone-warn">Error: {q.error}</div>
                            ) : (
                              <div className="quote-metrics">
                                <div>Last close: {q?.last_close ?? "-"}</div>
                                <div>Change: {q?.change_pct ?? "-"}</div>
                              </div>
                            )}
                          </article>
                        );
                      })}
                    </div>
                  ) : null}
                  {wsjHeadlines.length > 0 ? (
                    <div className="event-list">
                      {wsjHeadlines.slice(0, 5).map((h, idx) => (
                        <div className="event-row tone-neutral" key={`${h.url ?? "headline"}-${idx}`}>
                          <span className="pill">headline</span>
                          <span className="event-id">{h.title ?? "-"}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                  {wsjEconomy.length > 0 ? (
                    <div className="event-list">
                      {wsjEconomy.slice(0, 5).map((h, idx) => (
                        <div className="event-row tone-neutral" key={`${h.url ?? "econ"}-${idx}`}>
                          <span className="pill">economy</span>
                          <span className="event-id">{h.title ?? "-"}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                  {wsjFinance.length > 0 ? (
                    <div className="event-list">
                      {wsjFinance.slice(0, 5).map((h, idx) => (
                        <div className="event-row tone-neutral" key={`${h.url ?? "fin"}-${idx}`}>
                          <span className="pill">finance</span>
                          <span className="event-id">{h.title ?? "-"}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </details>
                <details style={{ marginTop: 10 }}>
                  <summary style={{ cursor: "pointer", opacity: 0.85 }}>Raw JSON (request + response)</summary>
                  <pre style={{ marginTop: 8 }}>{JSON.stringify(wsjShotLog, null, 2)}</pre>
                </details>
              </>
            ) : null}
          </CollapsibleSection>
        </div>
      </main>
    </>
  );
}
