import { NextResponse } from "next/server";

export const maxDuration = 300;

export async function POST(request: Request) {
  try {
    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const base = process.env.AGENT_BASE_URL ?? "http://localhost:8012";

    const forwardBody: Record<string, unknown> = {
      snapshot_label: typeof body.snapshot_label === "string" ? body.snapshot_label : "ui-morning-shot",
    };
    if (typeof body.browserbase_session_id === "string" && body.browserbase_session_id.trim()) {
      forwardBody.browserbase_session_id = body.browserbase_session_id.trim();
    }
    if (Array.isArray(body.sections)) forwardBody.sections = body.sections;

    const response = await fetch(`${base}/morning-shot`, {
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
