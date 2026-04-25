import { NextResponse } from "next/server";

export const maxDuration = 120;

export async function POST(request: Request) {
  try {
    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const base = process.env.AGENT_BASE_URL ?? "http://localhost:8012";

    const forwardBody: Record<string, unknown> = {
      snapshot_label: typeof body.snapshot_label === "string" ? body.snapshot_label : "ui-news-summary",
    };
    if (Array.isArray(body.source_ids) && body.source_ids.every((x) => typeof x === "string")) {
      forwardBody.source_ids = body.source_ids as string[];
    }

    const response = await fetch(`${base}/news-summary`, {
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
