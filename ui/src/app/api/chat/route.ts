import { NextResponse } from "next/server";

/** Proxy JSON directo a `POST /chat` (p. ej. scripts). La UI usa `/api/chat/stream`. */
// Allow long-running requests (morning shot can take 60-120 s)
export const maxDuration = 300;

export async function POST(request: Request) {
  try {
    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const base = process.env.AGENT_BASE_URL ?? "http://localhost:8012";

    const forwardBody: Record<string, unknown> = {
      message: typeof body.message === "string" ? body.message : "",
    };
    if (body.resume_auth === true) forwardBody.resume_auth = true;
    if (typeof body.browserbase_session_id === "string" && body.browserbase_session_id.trim()) {
      forwardBody.browserbase_session_id = body.browserbase_session_id.trim();
    }
    if (typeof body.site === "string") forwardBody.site = body.site;

    const response = await fetch(`${base}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(forwardBody),
    });

    const json = (await response.json().catch(() => ({ parse_error: true }))) as Record<string, unknown>;
    return NextResponse.json(
      { ok: response.ok, status: response.status, data: json },
      { status: response.ok ? 200 : response.status >= 400 ? response.status : 502 },
    );
  } catch (error) {
    return NextResponse.json({ ok: false, error: String(error) }, { status: 500 });
  }
}
