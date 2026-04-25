# Estructura del resumen de noticias (canónica)

Documento de referencia: *Resumen de noticias* (BIVA / ejemplo 21.04.2026), PDF `Resumen de noticias 20260421.pdf`.

Esta estructura es la que debe reflejar el producto: titular del día, cuerpo por bloques, y cierre con cotizaciones agrupadas.

---

## 1. Encabezado

| Elemento   | Descripción |
|-----------|-------------|
| Título    | `RESUMEN DE NOTICIAS` |
| Fecha     | Día de publicación (ej. `21.04.26`) — en API: `generated_at` |

---

## 2. Cuerpo informativo (titulares clasificados)

Orden y jerarquía **fijos**:

### 2.1 EMISORAS LOCALES

- **CAPITALES**  
  Línea por emisora: `TICKER` — resumen (texto; puede ser varias líneas en PDF).

- **DEUDA**  
  Mismo patrón (ej. CFE, bonos, etc.).

> En JSON (LLM): `emisoras_locales.capitales`, `emisoras_locales.deuda`  
> Items: `{ ticker?, headline, source }`

---

### 2.2 INTERNACIONALES → SIC

Bloque **INTERNACIONALES** con subnivel **SIC** (equity internacional SIC, no el mismo corte que “Internacionales” de economía).

> En JSON: arreglo `internacionales_sic`  
> Items: `{ ticker, headline, source }` cuando aplique.

---

### 2.3 VIGILANCIA

- **EVENTOS RELEVANTES** (auditoría externa, asambleas, avisos, acuerdos gubernamentales, etc.)  
- **MOVIMIENTOS INUSITADOS** (suspensión, movimientos atípicos de precio, etc.)

> En JSON: `vigilancia.eventos_relevantes`, `vigilancia.movimientos_inusitados`

---

### 2.4 ECONOMÍA / POLÍTICA

Subbloques (orden del PDF):

| Subsección        | Contenido típico |
|-------------------|------------------|
| **ECONOMÍA**      | T-MEC, indicadores, política local. Titulares sin ticker obligatorio. |
| **INTERNACIONALES** | Geopolítica, Asia, EEUU, no es el listado SIC. |
| **MERCADOS**      | Índices globales, petróleo, sesión. |
| **CRYPTO / BLOCKCHAIN** | CBDC, estafas, custodia, etc. |

> En JSON: `economia_politica.economia`, `.internacional`, `.mercados`, `.crypto`  
> Cada fila: `{ headline, source }` (y `ticker` si el modelo lo infiere).

---

## 3. ACTIVOS (cotizaciones)

Bloque separado al cierre, **no** mezclado con titulares. En el documento de referencia va agrupado así:

| Grupo        | Ejemplos de instrumentos (referencia) |
|-------------|----------------------------------------|
| **ÍNDICES** | FTSE BIVA, S&P/BMV IPC, DOW, S&P 500, Nasdaq… |
| **DIVISAS** | USD–MXN, USD–EUR (u otros pares) |
| **COMMODITIES** | plata, oro, petróleo, etc. |
| **CRIPTOMONEDA** | BTC-USD, ETH-USD |

> En API: arreglo plano `activos[]` con `{ ticker, quote }`. La UI agrupa tickers conocidos en estas cuatro filas; tickers no mapeados se listan bajo *Otros*.

Universo por defecto (configurable, `NEWS_ASSET_TICKERS`):

`^MXX`, `^DJI`, `^GSPC`, `^IXIC`, `MXN=X`, `EURUSD=X`, `GC=F`, `SI=F`, `CL=F`, `BTC-USD`, `ETH-USD`

---

## 4. Anexo opcional (no siempre en plantilla)

- **EARNINGS WHISPERS** — Puede existir en el PDF; **no** forma parte del JSON del LLM de clasificación de titulares en la versión actual. Si se implementa, documentar en API aparte.

---

## 5. Mapeo JSON ↔ secciones

| Ruta en `news_summary` | Sección en documento / UI |
|------------------------|---------------------------|
| `emisoras_locales.capitales` | EMISORAS LOCALES → Capitalés |
| `emisoras_locales.deuda`     | EMISORAS LOCALES → Deuda |
| `internacionales_sic`        | INTERNACIONALES → SIC |
| `vigilancia.eventos_relevantes` | VIGILANCIA → Eventos relevantes |
| `vigilancia.movimientos_inusitados` | VIGILANCIA → Movimientos inusitados |
| `economia_politica.economia`    | ECONOMÍA / POLÍTICA → Economía |
| `economia_politica.internacional` | ECONOMÍA / POLÍTICA → Internacionales (macro) |
| `economia_politica.mercados`    | ECONOMÍA / POLÍTICA → Mercados |
| `economia_politica.crypto`      | ECONOMÍA / POLÍTICA → Crypto / Blockchain |
| (fuera de LLM) `activos`        | ACTIVOS → por grupo de ticket |

El sobre `result` de `/news-summary` y del tool `news_summary` en chat incluye además: `ok`, `correlation_id`, `generated_at`, `source_stats`, `errors`, etc.

---

*Última alineación con el PDF de referencia: documento fijado en repositorio para no depender de archivos sueltos en `Downloads`.*
