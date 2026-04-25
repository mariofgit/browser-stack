"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// ─── Types ────────────────────────────────────────────────────────────────────

type BivaItem = { ticker: string; change_pct: string; reason: string; source: string };
type BivaIndex = { name: string; value: string; change_pct: string };

type ChatMessage =
  | { id: string; role: "user"; kind: "text"; text: string }
  | { id: string; role: "agent"; kind: "text"; text: string }
  | { id: string; role: "agent"; kind: "loading"; stepText?: string }
  | { id: string; role: "agent"; kind: "error"; text: string }
  | { id: string; role: "agent"; kind: "morning_shot"; data: Record<string, unknown> }
  | { id: string; role: "agent"; kind: "news_summary"; data: Record<string, unknown> }
  | { id: string; role: "agent"; kind: "auth_required"; site: string; sessionId: string; liveViewUrl: string }
  | { id: string; role: "agent"; kind: "quote"; ticker: string; data: Record<string, unknown> }
  | { id: string; role: "agent"; kind: "price_chart"; data: Record<string, unknown> };

// ─── Constants ────────────────────────────────────────────────────────────────

const MORNING_SHOT_STEPS = [
  "Conectando a Browserbase…",
  "Iniciando sesión en el navegador…",
  "Verificando sesión…",
  "Navegando WSJ y NYT…",
  "Extrayendo datos de mercado…",
  "Consultando Yahoo Finance…",
  "Generando resumen con OpenAI…",
];

const NEWS_STEPS = [
  "Consultando fuentes públicas…",
  "Extrayendo titulares…",
  "Clasificando noticias con IA…",
  "Consultando cotizaciones de activos…",
];

const ASSET_STEPS = ["Consultando Yahoo Finance…"];

const INITIAL_MESSAGE: ChatMessage = {
  id: "init",
  role: "agent",
  kind: "text",
  text: "Hola. Soy tu asistente de mercados. Puedo generar el morning shot (WSJ y NYT), el resumen de noticias (todas las fuentes o una en concreto), cotizaciones, gráficos de precio y detalles de un activo. ¿Qué necesitas?",
};

function mkId() {
  return Math.random().toString(36).slice(2, 10);
}

/** Misma idea que el agente: quita acentos para acertar con "muéstrame", "gráfico", etc. */
function normalizeIntentText(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toLowerCase();
}

/** Pasos del bubble de carga; todo el chat usa SSE (`/api/chat/stream`). */
function loadingStepsForMessage(text: string): string[] {
  const t = normalizeIntentText(text.trim());
  if (/\b(morning[\s\-]?shot|mshot|disparo)\b|\bmorning\b|\bshot\b/i.test(t)) {
    return MORNING_SHOT_STEPS;
  }
  if (/\b(resumen|noticias|news|headlines?|summary)\b/i.test(t)) {
    return NEWS_STEPS;
  }
  if (/\b(?:cotizaci[oó]n|precio|quote|ticker)\s+(?:de\s+)?[A-Za-z.^]{1,10}/i.test(t)) {
    return ASSET_STEPS;
  }
  if (
    /\b(gráfic[oa]|grafic[oa]|chart|historial\s+de\s+precio|evoluci[oó]n(?:\s+del\s+precio)?|curva\s+de\s+precio|desempe[nñ]o)\b/i.test(
      t,
    ) ||
    /(?:necesito|quiero|quieres|puedo)\s+ver\s+(?:un\s+|el\s+|la\s+)?(?:gráfic[oa]|grafic[oa]|chart)\b/i.test(
      t,
    ) ||
    /(?:muestr(?:a|ame)|ens[eé]n(?:a|ame)|dame|trae(?:me)?)\s+(?:por\s+favor\s+)?(?:el\s+|la\s+)?(?:gráfic[oa]|grafic[oa]|chart)\b/i.test(
      t,
    )
  ) {
    return ASSET_STEPS;
  }
  if (
    /\b(detalles?|informaci[oó]n|info|ficha)\s+(?:de|del|sobre)\b/i.test(t) ||
    /\b(?:c[oó]mo\s+va|a\s+c[oó]mo\s+est[aá])\b/i.test(t)
  ) {
    return ASSET_STEPS;
  }
  return MORNING_SHOT_STEPS;
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function ItemList({ items, toneColor }: { items: BivaItem[]; toneColor: string }) {
  if (items.length === 0) return null;
  return (
    <ul style={{ listStyle: "none", padding: 0, margin: "6px 0", display: "grid", gap: 6 }}>
      {items.map((it, i) => (
        <li key={`${it.ticker}-${i}`} style={{ display: "flex", gap: 8, alignItems: "baseline", fontSize: "0.88rem" }}>
          <span className="hl-ticker" style={{ flexShrink: 0 }}>{it.ticker}</span>
          <span style={{ color: toneColor, flexShrink: 0, fontWeight: 600, minWidth: 58 }}>{it.change_pct}</span>
          <span style={{ opacity: 0.85 }}>{it.reason}</span>
          {it.source && <span className="pill" style={{ flexShrink: 0 }}>{it.source}</span>}
        </li>
      ))}
    </ul>
  );
}

function SourceBanner({ sourceStatus, partial }: { sourceStatus: Record<string, string>; partial: boolean }) {
  const entries = Object.entries(sourceStatus);
  if (entries.length === 0) return null;
  return (
    <div style={{ display: "flex", gap: 6, marginBottom: 8, flexWrap: "wrap" }}>
      {entries.map(([src, status]) => {
        const cls = status === "ok" ? "pill-ok" : status === "auth_required" ? "pill-warn" : "pill-error";
        return (
          <span key={src} className={`pill ${cls}`}>
            {src}: {status === "ok" ? "✓" : status === "auth_required" ? "auth" : status}
          </span>
        );
      })}
      {partial && <span className="pill pill-warn">parcial</span>}
    </div>
  );
}

function JsonDebugPanel({ title = "JSON", data }: { title?: string; data: unknown }) {
  return (
    <details className="wsj-source-panel" style={{ marginTop: 8 }}>
      <summary style={{ cursor: "pointer", fontWeight: 600 }}>{title}</summary>
      <pre
        style={{
          margin: "8px 0 0",
          maxHeight: 360,
          overflow: "auto",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          fontSize: "0.75rem",
          lineHeight: 1.45,
          opacity: 0.78,
        }}
      >
        {JSON.stringify(data, null, 2)}
      </pre>
    </details>
  );
}

function MissingSourcePanel({
  authFailures,
  sessionId,
  liveViewUrl,
  onResume,
  resuming,
}: {
  authFailures: string[];
  sessionId: string;
  liveViewUrl: string;
  onResume?: (sessionId: string, site: string) => void;
  resuming: boolean;
}) {
  if (authFailures.length === 0) return null;

  return (
    <div className="bubble-auth" style={{ marginBottom: 10 }}>
      <p style={{ margin: "0 0 10px", lineHeight: 1.45 }}>
        Este Morning Shot se generó con las fuentes disponibles. Falta autenticar{" "}
        <strong>{authFailures.map((s) => s.toUpperCase()).join(", ")}</strong> para completarlo.
      </p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        {liveViewUrl && (
          <a className="auth-pill-btn" href={liveViewUrl} target="_blank" rel="noreferrer">
            Abrir Live View
          </a>
        )}
        {sessionId && onResume && (
          <button
            type="button"
            className="auth-pill-btn"
            onClick={() => onResume(sessionId, authFailures[0] ?? "wsj")}
            disabled={resuming}
          >
            {resuming ? "Retomando…" : "Ya me autentiqué"}
          </button>
        )}
      </div>
      {sessionId && (
        <p style={{ margin: "8px 0 0", fontSize: "0.75rem", opacity: 0.45 }}>
          Sesión: <code>{sessionId.slice(0, 14)}…</code>
        </p>
      )}
    </div>
  );
}

function MorningShotCard({
  data,
  onResume,
  resuming,
}: {
  data: Record<string, unknown>;
  onResume?: (sessionId: string, site: string) => void;
  resuming: boolean;
}) {
  const [open, setOpen] = useState(true);

  if (data.ok === false) {
    const msg = typeof data.message === "string" ? data.message : String(data.error ?? "Error");
    const detail = typeof data.detail === "string" ? data.detail : null;
    return (
      <div className="result-card">
        <span className="result-card-title">Morning Shot BIVA</span>
        <p style={{ margin: "8px 0 0", opacity: 0.75, fontSize: "0.88rem" }}>{msg}</p>
        {detail && <p style={{ margin: "6px 0 0", opacity: 0.55, fontSize: "0.8rem" }}>{detail}</p>}
        <JsonDebugPanel title="JSON del Morning Shot" data={data} />
      </div>
    );
  }

  const biva = (data.biva_summary ?? null) as Record<string, unknown> | null;
  const factsPack = (data.facts_pack ?? null) as Record<string, unknown> | null;
  const meta = (data.meta ?? null) as Record<string, unknown> | null;
  const sourceStatus = (meta != null && typeof meta.source_status === "object" && meta.source_status != null
    ? meta.source_status
    : {}) as Record<string, string>;
  const partial = data.partial === true;
  const authFailures = (Array.isArray(data.auth_failures) ? data.auth_failures : []) as string[];
  const sessionId = meta != null && typeof meta.browserbase_session_id === "string" ? meta.browserbase_session_id : "";
  const liveViewUrl = meta != null && typeof meta.live_view_url === "string" ? meta.live_view_url : "";

  const internacional = (biva != null && typeof biva.internacional === "object" && biva.internacional != null
    ? biva.internacional
    : null) as Record<string, unknown> | null;
  const nacional = (biva != null && typeof biva.nacional === "object" && biva.nacional != null
    ? biva.nacional
    : null) as Record<string, unknown> | null;
  const disclaimer = biva != null && typeof biva.disclaimer === "string" ? biva.disclaimer : null;
  const datoDelDia = biva != null && typeof biva.dato_del_dia === "string" ? biva.dato_del_dia : null;

  const intBulls = (internacional != null && Array.isArray(internacional.bulls) ? internacional.bulls : []) as BivaItem[];
  const intBears = (internacional != null && Array.isArray(internacional.bears) ? internacional.bears : []) as BivaItem[];
  const intNarrative = internacional != null && typeof internacional.narrative === "string" ? internacional.narrative : "";

  const nacBulls = (nacional != null && Array.isArray(nacional.bulls) ? nacional.bulls : []) as BivaItem[];
  const nacBears = (nacional != null && Array.isArray(nacional.bears) ? nacional.bears : []) as BivaItem[];
  const nacIndices = (nacional != null && Array.isArray(nacional.indices) ? nacional.indices : []) as BivaIndex[];
  const nacNarrative = nacional != null && typeof nacional.narrative === "string" ? nacional.narrative : "";

  const mxIndices = (factsPack != null && Array.isArray(factsPack.mx_indices) ? factsPack.mx_indices : []) as Record<string, unknown>[];
  const errors = (Array.isArray(data.errors) ? data.errors : []) as string[];

  return (
    <div className="result-card">
      <div className="result-card-header">
        <span className="result-card-title">Morning Shot BIVA</span>
        <button type="button" className="icon-toggle" onClick={() => setOpen((v) => !v)}
          aria-label={open ? "Colapsar" : "Expandir"}>
          {open ? "▾" : "▸"}
        </button>
      </div>

      {open && (
        <div>
          <SourceBanner sourceStatus={sourceStatus} partial={partial} />
          <MissingSourcePanel
            authFailures={authFailures}
            sessionId={sessionId}
            liveViewUrl={liveViewUrl}
            onResume={onResume}
            resuming={resuming}
          />
          {disclaimer && <p className="wsj-disclaimer">{disclaimer}</p>}

          {biva ? (
            <div className="wsj-summary">
              {internacional && (
                <div>
                  <h3>Internacional</h3>
                  {intNarrative && <p style={{ margin: "4px 0 8px", fontSize: "0.9rem", opacity: 0.88 }}>{intNarrative}</p>}
                  {intBulls.length > 0 && (
                    <>
                      <strong style={{ fontSize: "0.82rem", color: "#4ade80" }}>Bulls</strong>
                      <ItemList items={intBulls} toneColor="#4ade80" />
                    </>
                  )}
                  {intBears.length > 0 && (
                    <>
                      <strong style={{ fontSize: "0.82rem", color: "#f87171" }}>Bears</strong>
                      <ItemList items={intBears} toneColor="#f87171" />
                    </>
                  )}
                </div>
              )}

              {nacional && (
                <div>
                  <h3>Nacional (México)</h3>
                  {nacNarrative && <p style={{ margin: "4px 0 8px", fontSize: "0.9rem", opacity: 0.88 }}>{nacNarrative}</p>}
                  {nacIndices.length > 0 && (
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", margin: "4px 0 8px" }}>
                      {nacIndices.map((ix, i) => (
                        <span key={i} className="pill">
                          {ix.name}: {ix.value} ({ix.change_pct})
                        </span>
                      ))}
                    </div>
                  )}
                  {nacBulls.length > 0 && (
                    <>
                      <strong style={{ fontSize: "0.82rem", color: "#4ade80" }}>Bulls</strong>
                      <ItemList items={nacBulls} toneColor="#4ade80" />
                    </>
                  )}
                  {nacBears.length > 0 && (
                    <>
                      <strong style={{ fontSize: "0.82rem", color: "#f87171" }}>Bears</strong>
                      <ItemList items={nacBears} toneColor="#f87171" />
                    </>
                  )}
                </div>
              )}

              {datoDelDia && (
                <div className="wsj-watch">
                  <strong>★ Dato del día</strong>
                  <p style={{ margin: "6px 0 0", fontSize: "0.9rem" }}>{datoDelDia}</p>
                </div>
              )}
            </div>
          ) : (
            <p style={{ opacity: 0.65, margin: "8px 0", fontSize: "0.88rem" }}>
              Sin resumen BIVA. Verifica <code>OPENAI_API_KEY</code>.
            </p>
          )}

          {mxIndices.length > 0 && (
            <details className="wsj-source-panel">
              <summary style={{ cursor: "pointer", fontWeight: 600 }}>Cotizaciones en vivo (Yahoo)</summary>
              <div className="quotes-grid" style={{ marginTop: 8 }}>
                {mxIndices.map((m) => {
                  const q = m.quote as Record<string, unknown> | undefined;
                  const chg = String(q?.change_pct ?? "");
                  return (
                    <article className="quote-card" key={String(m.ticker ?? mkId())}>
                      <div className="quote-head">
                        <span className="hl-ticker">{String(m.ticker ?? "-")}</span>
                        <span className="hl-name">{String(q?.name ?? "")}</span>
                      </div>
                      <div className="quote-metrics">
                        <div>{String(q?.last_close ?? "-")}</div>
                        <div style={{ color: chg.startsWith("+") ? "#4ade80" : chg.startsWith("-") ? "#f87171" : undefined }}>
                          {chg || "-"}
                        </div>
                      </div>
                    </article>
                  );
                })}
              </div>
            </details>
          )}

          {errors.length > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary style={{ cursor: "pointer", opacity: 0.55, fontSize: "0.82rem" }}>
                {errors.length} aviso{errors.length > 1 ? "s" : ""}
              </summary>
              <ul style={{ fontSize: "0.78rem", opacity: 0.65, marginTop: 4 }}>
                {errors.slice(0, 5).map((e, i) => <li key={i}>{e}</li>)}
              </ul>
            </details>
          )}

          <JsonDebugPanel title="JSON del Morning Shot" data={data} />
        </div>
      )}
    </div>
  );
}

type NewsItem = { ticker?: string; headline: string; source: string };

/** Alineado con `docs/RESUMEN_NOTICIAS_ESTRUCTURA.md` y el PDF de referencia. */
type ActivosCategoria = "indices" | "divisas" | "commodities" | "cripto" | "otro";

const TICKER_CATEGORIA: Record<string, ActivosCategoria> = {
  "^MXX": "indices",
  "^DJI": "indices",
  "^GSPC": "indices",
  "^IXIC": "indices",
  "MXN=X": "divisas",
  "EURUSD=X": "divisas",
  "GC=F": "commodities",
  "SI=F": "commodities",
  "CL=F": "commodities",
  "BTC-USD": "cripto",
  "ETH-USD": "cripto",
};

const ACTIVOS_CATEGORIA_LABEL: Record<ActivosCategoria, string> = {
  indices: "Índices",
  divisas: "Divisas",
  commodities: "Commodities",
  cripto: "Criptomoneda",
  otro: "Otros",
};

const ACTIVOS_CATEGORIA_ORDEN: ActivosCategoria[] = [
  "indices",
  "divisas",
  "commodities",
  "cripto",
  "otro",
];

function categoriaActivo(ticker: string): ActivosCategoria {
  return TICKER_CATEGORIA[ticker] ?? "otro";
}

function NewsSection({ title, items }: { title: string; items: NewsItem[] }) {
  if (items.length === 0) return null;
  return (
    <div style={{ marginBottom: 10 }}>
      <h3 style={{ fontSize: "0.88rem", margin: "0 0 5px" }}>{title}</h3>
      <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 4 }}>
        {items.map((it, i) => (
          <li key={i} style={{ display: "flex", gap: 6, alignItems: "baseline", fontSize: "0.85rem" }}>
            {it.ticker && <span className="hl-ticker" style={{ flexShrink: 0 }}>{it.ticker}</span>}
            <span style={{ opacity: 0.88 }}>{it.headline}</span>
            {it.source && <span className="pill" style={{ flexShrink: 0, opacity: 0.6 }}>{it.source}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function NewsSummaryCard({ data }: { data: Record<string, unknown> }) {
  const [open, setOpen] = useState(true);

  if (data.ok === false) {
    const msg = typeof data.message === "string" ? data.message : String(data.error ?? "Error");
    return (
      <div className="result-card">
        <span className="result-card-title">Resumen de noticias</span>
        <p style={{ margin: "8px 0 0", opacity: 0.7, fontSize: "0.88rem" }}>{msg}</p>
      </div>
    );
  }

  const summary = (data.news_summary ?? null) as Record<string, unknown> | null;
  const activos = (Array.isArray(data.activos) ? data.activos : []) as Record<string, unknown>[];
  const stats = (data.source_stats ?? null) as Record<string, unknown> | null;
  const total = stats != null && typeof stats.total === "number" ? stats.total : 0;
  const ok = stats != null && typeof stats.ok === "number" ? stats.ok : 0;
  const errorCount = stats != null && typeof stats.error === "number" ? stats.error : 0;

  const emisoras = (summary != null && typeof summary.emisoras_locales === "object" && summary.emisoras_locales != null
    ? summary.emisoras_locales
    : {}) as Record<string, unknown>;
  const capitales = (Array.isArray(emisoras.capitales) ? emisoras.capitales : []) as NewsItem[];
  const deuda = (Array.isArray(emisoras.deuda) ? emisoras.deuda : []) as NewsItem[];

  const internacionales = (Array.isArray(summary?.internacionales_sic) ? summary.internacionales_sic : []) as NewsItem[];

  const vigilancia = (summary != null && typeof summary.vigilancia === "object" && summary.vigilancia != null
    ? summary.vigilancia
    : {}) as Record<string, unknown>;
  const eventos = (Array.isArray(vigilancia.eventos_relevantes) ? vigilancia.eventos_relevantes : []) as NewsItem[];
  const movimientos = (Array.isArray(vigilancia.movimientos_inusitados) ? vigilancia.movimientos_inusitados : []) as NewsItem[];

  const econ = (summary != null && typeof summary.economia_politica === "object" && summary.economia_politica != null
    ? summary.economia_politica
    : {}) as Record<string, unknown>;
  const economia = (Array.isArray(econ.economia) ? econ.economia : []) as NewsItem[];
  const internacional = (Array.isArray(econ.internacional) ? econ.internacional : []) as NewsItem[];
  const mercados = (Array.isArray(econ.mercados) ? econ.mercados : []) as NewsItem[];
  const crypto = (Array.isArray(econ.crypto) ? econ.crypto : []) as NewsItem[];

  const generatedAt =
    typeof data.generated_at === "string" ? (data.generated_at as string) : null;
  const fechaLabel = generatedAt
    ? new Date(generatedAt).toLocaleString("es-MX", { dateStyle: "short", timeStyle: "short" })
    : null;

  const activosByCat: Record<ActivosCategoria, typeof activos> = {
    indices: [],
    divisas: [],
    commodities: [],
    cripto: [],
    otro: [],
  };
  for (const m of activos) {
    const t = String(m.ticker ?? "");
    activosByCat[categoriaActivo(t)].push(m);
  }

  return (
    <div className="result-card">
      <div className="result-card-header">
        <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          <span className="result-card-title">Resumen de noticias</span>
          {fechaLabel && (
            <span style={{ fontSize: "0.75rem", opacity: 0.55, fontWeight: 500 }}>{fechaLabel}</span>
          )}
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <span className="pill pill-ok">{ok}/{total} fuentes</span>
          {errorCount > 0 && <span className="pill pill-error">{errorCount} error</span>}
          <button type="button" className="icon-toggle" onClick={() => setOpen((v) => !v)}>
            {open ? "▾" : "▸"}
          </button>
        </div>
      </div>

      {open && summary && (
        <div style={{ marginTop: 8 }}>
          {(capitales.length > 0 || deuda.length > 0) && (
            <div style={{ marginBottom: 12 }}>
              <strong style={{ fontSize: "0.82rem", opacity: 0.75, letterSpacing: "0.02em" }}>
                EMISORAS LOCALES
              </strong>
              <NewsSection title="Capitales" items={capitales} />
              <NewsSection title="Deuda" items={deuda} />
            </div>
          )}
          {internacionales.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <strong style={{ fontSize: "0.82rem", opacity: 0.75, letterSpacing: "0.02em" }}>
                INTERNACIONALES
              </strong>
              <NewsSection title="SIC" items={internacionales} />
            </div>
          )}
          {(eventos.length > 0 || movimientos.length > 0) && (
            <div style={{ marginBottom: 12 }}>
              <strong style={{ fontSize: "0.82rem", opacity: 0.75, letterSpacing: "0.02em" }}>VIGILANCIA</strong>
              <NewsSection title="Eventos relevantes" items={eventos} />
              <NewsSection title="Movimientos inusitados" items={movimientos} />
            </div>
          )}
          {(economia.length + internacional.length + mercados.length + crypto.length > 0) && (
            <div style={{ marginBottom: 12 }}>
              <strong style={{ fontSize: "0.82rem", opacity: 0.75, letterSpacing: "0.02em" }}>
                ECONOMÍA / POLÍTICA
              </strong>
              <NewsSection title="Economía" items={economia} />
              <NewsSection title="Internacionales" items={internacional} />
              <NewsSection title="Mercados" items={mercados} />
              <NewsSection title="Crypto / Blockchain" items={crypto} />
            </div>
          )}
        </div>
      )}

      {open && activos.length > 0 && (
        <details className="wsj-source-panel" open>
          <summary style={{ cursor: "pointer", fontWeight: 600, letterSpacing: "0.02em" }}>ACTIVOS</summary>
          <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 12 }}>
            {ACTIVOS_CATEGORIA_ORDEN.map((cat) => {
              const list = activosByCat[cat];
              if (list.length === 0) return null;
              return (
                <div key={cat}>
                  <div style={{ fontSize: "0.8rem", fontWeight: 600, opacity: 0.9, marginBottom: 6 }}>
                    {ACTIVOS_CATEGORIA_LABEL[cat].toUpperCase()}
                  </div>
                  <div className="quotes-grid">
                    {list.map((m) => {
                      const q = m.quote as Record<string, unknown> | undefined;
                      const chg = String(q?.change_pct ?? "");
                      return (
                        <article className="quote-card" key={String(m.ticker ?? mkId())}>
                          <div className="quote-head">
                            <span className="hl-ticker">{String(m.ticker ?? "-")}</span>
                          </div>
                          <div className="quote-metrics">
                            <div>{String(q?.last_close ?? "-")}</div>
                            <div
                              style={{
                                color: chg.startsWith("+")
                                  ? "#4ade80"
                                  : chg.startsWith("-")
                                    ? "#f87171"
                                    : undefined,
                              }}
                            >
                              {chg || "-"}
                            </div>
                          </div>
                        </article>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        </details>
      )}

      {open && <JsonDebugPanel title="JSON del resumen de noticias" data={data} />}
    </div>
  );
}

function QuoteCard({ ticker, data }: { ticker: string; data: Record<string, unknown> }) {
  const ok = data.ok !== false;
  const chg = String(data.change_pct ?? "");
  return (
    <div className="result-card" style={{ maxWidth: 320 }}>
      <div className="result-card-header">
        <span className="hl-ticker">{ticker}</span>
        {ok ? (
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ fontWeight: 600 }}>{String(data.last_close ?? "-")}</span>
            <span style={{ color: chg.startsWith("+") ? "#4ade80" : chg.startsWith("-") ? "#f87171" : undefined }}>
              {chg}
            </span>
          </div>
        ) : (
          <span className="pill pill-error">error</span>
        )}
      </div>
      {ok && <p style={{ margin: "4px 0 0", opacity: 0.65, fontSize: "0.82rem" }}>{String(data.name ?? "")}</p>}
    </div>
  );
}

type ChartPoint = { t: string; c: number };

function PriceChartCard({ data }: { data: Record<string, unknown> }) {
  const ok = data.ok !== false;
  const ticker = String(data.Ticker ?? "?");
  const period = String(data.period ?? "");
  const series = (Array.isArray(data.series) ? data.series : []) as ChartPoint[];
  const quote = (data.quote && typeof data.quote === "object" ? data.quote : null) as Record<
    string,
    unknown
  > | null;

  if (!ok || series.length < 1) {
    const msg = String(data.error ?? "Sin historial de precios.");
    return (
      <div className="result-card" style={{ maxWidth: 400 }}>
        <div className="result-card-header">
          <span className="hl-ticker">{ticker}</span>
          <span className="pill pill-error">sin datos</span>
        </div>
        <p style={{ margin: "8px 0 0", opacity: 0.7, fontSize: "0.88rem" }}>{msg}</p>
      </div>
    );
  }

  const vals = series.map((p) => p.c);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const pad = 10;
  const w = 320;
  const h = 132;
  const normY = (v: number) => {
    if (max === min) return h / 2;
    return pad + (h - 2 * pad) * (1 - (v - min) / (max - min));
  };
  const stepX = series.length > 1 ? (w - 2 * pad) / (series.length - 1) : 0;
  const pts = series
    .map((p, i) => {
      const x = pad + i * stepX;
      const y = normY(p.c);
      return `${x},${y}`;
    })
    .join(" ");

  const up = vals[vals.length - 1] >= vals[0];
  const stroke = up ? "#4ade80" : "#f87171";
  const chg = quote ? String(quote.change_pct ?? "") : "";

  return (
    <div className="result-card" style={{ maxWidth: 400 }}>
      <div className="result-card-header">
        <span className="hl-ticker">{ticker}</span>
        {quote && quote.ok !== false ? (
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ fontWeight: 600 }}>{String(quote.last_close ?? "-")}</span>
            <span
              style={{
                color: chg.startsWith("+") ? "#4ade80" : chg.startsWith("-") ? "#f87171" : undefined,
              }}
            >
              {chg}
            </span>
          </div>
        ) : null}
      </div>
      {quote && quote.ok !== false ? (
        <p style={{ margin: "4px 0 8px", opacity: 0.65, fontSize: "0.82rem" }}>{String(quote.name ?? "")}</p>
      ) : null}
      <svg
        viewBox={`0 0 ${w} ${h}`}
        width="100%"
        height={h}
        style={{ display: "block", marginBottom: 6 }}
        role="img"
        aria-label={`Precio ${ticker} (${period})`}
      >
        <polyline fill="none" stroke={stroke} strokeWidth="2" strokeLinejoin="round" points={pts} />
      </svg>
      <span style={{ fontSize: "0.75rem", opacity: 0.5 }}>
        Periodo Yahoo Finance: {period || "—"}
      </span>
    </div>
  );
}

function LoadingBubble({ steps, loadingStep, stepText }: { steps: string[]; loadingStep: number; stepText?: string }) {
  return (
    <div className="bubble-loading">
      <span className="loading-dot" />
      <span>{stepText || steps[loadingStep % steps.length]}</span>
    </div>
  );
}

function AuthBubble({
  site,
  sessionId,
  liveViewUrl,
  onResume,
  resuming,
}: {
  site: string;
  sessionId: string;
  liveViewUrl: string;
  onResume: () => void;
  resuming: boolean;
}) {
  return (
    <div className="bubble-auth">
      <p style={{ margin: "0 0 12px", lineHeight: 1.55 }}>
        Necesito que te autentiques en <strong>{site.toUpperCase()}</strong> para continuar.
        Inicia sesión en el navegador de abajo y luego presiona el botón.
      </p>
      {liveViewUrl && (
        <>
          <p style={{ margin: "0 0 6px", fontSize: "0.8rem", opacity: 0.55 }}>
            O{" "}
            <a href={liveViewUrl} target="_blank" rel="noreferrer" style={{ color: "#7aa2d4" }}>
              abre en una nueva pestaña
            </a>
          </p>
          <iframe
            title={`Live View — ${site.toUpperCase()}`}
            src={liveViewUrl}
            sandbox="allow-same-origin allow-scripts allow-forms"
            allow="clipboard-read; clipboard-write"
            style={{ width: "100%", height: 480, border: "1px solid #252e40", borderRadius: 8 }}
          />
        </>
      )}
      <div style={{ marginTop: 12 }}>
        {resuming ? (
          <span style={{ opacity: 0.55, fontSize: "0.88rem" }}>Retomando…</span>
        ) : (
          <button type="button" className="auth-pill-btn" onClick={onResume}>
            Ya me autentiqué →
          </button>
        )}
      </div>
      <p style={{ margin: "8px 0 0", fontSize: "0.75rem", opacity: 0.4 }}>
        Sesión: <code>{sessionId.slice(0, 14)}…</code>
      </p>
    </div>
  );
}

function MessageBubble({
  msg,
  steps,
  loadingStep,
  onResume,
  resuming,
}: {
  msg: ChatMessage;
  steps: string[];
  loadingStep: number;
  onResume: (sessionId: string, site: string) => void;
  resuming: boolean;
}) {
  if (msg.role === "user") {
    return (
      <div className="bubble-user">
        <p className="bubble-text">{msg.text}</p>
      </div>
    );
  }

  if (msg.kind === "loading") {
    return (
      <div className="bubble-agent">
        <LoadingBubble steps={steps} loadingStep={loadingStep} stepText={msg.stepText} />
      </div>
    );
  }

  if (msg.kind === "auth_required") {
    return (
      <AuthBubble
        site={msg.site}
        sessionId={msg.sessionId}
        liveViewUrl={msg.liveViewUrl}
        onResume={() => onResume(msg.sessionId, msg.site)}
        resuming={resuming}
      />
    );
  }

  if (msg.kind === "morning_shot") {
    return (
      <div className="bubble-agent">
        <MorningShotCard data={msg.data} onResume={onResume} resuming={resuming} />
      </div>
    );
  }

  if (msg.kind === "news_summary") {
    return (
      <div className="bubble-agent">
        <NewsSummaryCard data={msg.data} />
      </div>
    );
  }

  if (msg.kind === "quote") {
    return (
      <div className="bubble-agent">
        <QuoteCard ticker={msg.ticker} data={msg.data} />
      </div>
    );
  }

  if (msg.kind === "price_chart") {
    return (
      <div className="bubble-agent">
        <PriceChartCard data={msg.data} />
      </div>
    );
  }

  if (msg.kind === "error") {
    return (
      <div className="bubble-agent">
        <p className="tone-warn">{msg.text}</p>
      </div>
    );
  }

  // text
  return (
    <div className="bubble-agent">
      <p className="bubble-text">{msg.text}</p>
    </div>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function Page() {
  const [messages, setMessages] = useState<ChatMessage[]>([INITIAL_MESSAGE]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [loadingStep, setLoadingStep] = useState(0);
  const [currentSteps, setCurrentSteps] = useState<string[]>(MORNING_SHOT_STEPS);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Cycle loading steps while waiting
  useEffect(() => {
    if (!loading && !resuming) {
      setLoadingStep(0);
      return;
    }
    const t = window.setInterval(() => setLoadingStep((i) => i + 1), 2200);
    return () => window.clearInterval(t);
  }, [loading, resuming]);

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }, [input]);

  const handleResult = useCallback((loadingId: string, data: Record<string, unknown>) => {
    const tool = data.tool as string | undefined;
    const result = data.result as Record<string, unknown> | undefined;

    if (data.auth_required) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === loadingId
            ? ({
                id: loadingId,
                role: "agent",
                kind: "auth_required",
                site: String(data.site ?? "wsj"),
                sessionId: String(data.browserbase_session_id ?? ""),
                liveViewUrl: String(data.live_view_url ?? ""),
              } as ChatMessage)
            : m,
        ),
      );
      return;
    }

    if (tool === "morning_shot" && result) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === loadingId
            ? ({ id: loadingId, role: "agent", kind: "morning_shot", data: result } as ChatMessage)
            : m,
        ),
      );
      return;
    }

    if (tool === "news_summary" && result) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === loadingId
            ? ({ id: loadingId, role: "agent", kind: "news_summary", data: result } as ChatMessage)
            : m,
        ),
      );
      return;
    }

    if (tool === "quote" && result) {
      const ticker = String((result.Ticker as string | undefined) ?? "?");
      setMessages((prev) =>
        prev.map((m) =>
          m.id === loadingId
            ? ({ id: loadingId, role: "agent", kind: "quote", ticker, data: result } as ChatMessage)
            : m,
        ),
      );
      return;
    }

    if (tool === "price_chart" && result) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === loadingId
            ? ({ id: loadingId, role: "agent", kind: "price_chart", data: result } as ChatMessage)
            : m,
        ),
      );
      return;
    }

    const text =
      typeof data.message === "string"
        ? data.message
        : tool === "none"
          ? "Puedo generar el morning shot, resumen de noticias, cotización, gráfico o detalles de un activo. Ej.: 'necesito ver un gráfico de VOO', 'detalles de NVDA', 'resumen de noticias de Reuters'."
          : "Respuesta recibida.";
    setMessages((prev) =>
      prev.map((m) =>
        m.id === loadingId
          ? ({ id: loadingId, role: "agent", kind: "text", text } as ChatMessage)
          : m,
      ),
    );
  }, []);

  const callChatStream = useCallback(
    async (payload: Record<string, unknown>, loadingId: string, steps: string[]) => {
      setCurrentSteps(steps);
      try {
        const res = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

        if (!res.ok || !res.body) {
          throw new Error(`Stream no disponible (${res.status})`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const blocks = buffer.split("\n\n");
          buffer = blocks.pop() ?? "";

          for (const block of blocks) {
            if (!block.trim()) continue;
            const event = block.match(/^event:\s*(\w+)/m)?.[1];
            const dataRaw = block.match(/^data:\s*(.+)$/m)?.[1];
            if (!event || !dataRaw) continue;

            const data = JSON.parse(dataRaw) as Record<string, unknown>;

            if (event === "step") {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === loadingId && m.role === "agent" && m.kind === "loading"
                    ? { ...m, stepText: String(data.text ?? "") }
                    : m,
                ),
              );
              continue;
            }

            if (event === "chunk") {
              const n = typeof data.chars === "number" ? data.chars : Number(data.chars ?? 0);
              const label = Number.isFinite(n) && n > 0
                ? `Generando resumen del modelo… (${n} caracteres)`
                : "Generando resumen del modelo…";
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === loadingId && m.role === "agent" && m.kind === "loading"
                    ? { ...m, stepText: label }
                    : m,
                ),
              );
              continue;
            }

            if (event === "auth") {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === loadingId
                    ? ({
                        id: loadingId,
                        role: "agent",
                        kind: "auth_required",
                        site: String(data.site ?? "wsj"),
                        sessionId: String(data.session_id ?? ""),
                        liveViewUrl: String(data.live_view_url ?? ""),
                      } as ChatMessage)
                    : m,
                ),
              );
              return;
            }

            if (event === "result") {
              handleResult(loadingId, data);
              return;
            }

            if (event === "error") {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === loadingId
                    ? ({ id: loadingId, role: "agent", kind: "error", text: String(data.text ?? "Error del stream") } as ChatMessage)
                    : m,
                ),
              );
              return;
            }
          }
        }

        setMessages((prev) =>
          prev.map((m) =>
            m.id === loadingId
              ? ({ id: loadingId, role: "agent", kind: "error", text: "El stream terminó sin resultado." } as ChatMessage)
              : m,
          ),
        );
      } catch (err) {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === loadingId
              ? ({ id: loadingId, role: "agent", kind: "error", text: `No se pudo abrir el stream: ${String(err)}` } as ChatMessage)
              : m,
          ),
        );
      }
    },
    [handleResult],
  );

  const sendMessage = useCallback(async () => {
    const text = input.trim();
    if (!text || loading || resuming) return;

    const loadingId = mkId();

    setMessages((prev) => [
      ...prev,
      { id: mkId(), role: "user", kind: "text", text } as ChatMessage,
      { id: loadingId, role: "agent", kind: "loading" } as ChatMessage,
    ]);
    setInput("");
    setLoading(true);
    setLoadingStep(0);

    try {
      await callChatStream({ message: text }, loadingId, loadingStepsForMessage(text));
    } finally {
      setLoading(false);
    }
  }, [input, loading, resuming, callChatStream]);

  const retryAuth = useCallback(
    async (sessionId: string, site: string) => {
      const loadingId = mkId();
      setResuming(true);
      setMessages((prev) => {
        const withoutAuth = prev.filter(
          (m) => !(m.role === "agent" && m.kind === "auth_required"),
        );
        return [...withoutAuth, { id: loadingId, role: "agent", kind: "loading" } as ChatMessage];
      });
      setLoadingStep(0);
      try {
        await callChatStream(
          { message: "", resume_auth: true, browserbase_session_id: sessionId, site },
          loadingId,
          MORNING_SHOT_STEPS,
        );
      } finally {
        setResuming(false);
      }
    },
    [callChatStream],
  );

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void sendMessage();
    }
  };

  const isLocked = loading || resuming;

  return (
    <div className="chat-shell">
      {/* Header */}
      <header className="chat-header">
        <div className="chat-header-inner">
          <img src="/biva-logo.svg" alt="BIVA" className="chat-logo-img" />
          <span className="chat-divider" />
          <span className="chat-title">Finance Agent</span>
        </div>
      </header>

      {/* Messages */}
      <div className="chat-messages" role="log" aria-live="polite">
        {messages.map((msg) => (
          <MessageBubble
            key={msg.id}
            msg={msg}
            steps={currentSteps}
            loadingStep={loadingStep}
            onResume={retryAuth}
            resuming={resuming}
          />
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Input bar */}
      <div className="chat-input-bar">
        <div className="chat-input-inner">
          <textarea
            ref={textareaRef}
            className="chat-textarea"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={isLocked ? "Procesando…" : "Escribe aquí… (Enter para enviar, Shift+Enter para nueva línea)"}
            disabled={isLocked}
            rows={1}
            aria-label="Mensaje al agente"
          />
          <button
            type="button"
            className="chat-send-btn"
            onClick={() => void sendMessage()}
            disabled={isLocked || !input.trim()}
            aria-label="Enviar mensaje"
          >
            ▶
          </button>
        </div>
      </div>
    </div>
  );
}
