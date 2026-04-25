// Allow long-running streaming requests (morning shot can take 60-120 s)
export const maxDuration = 300;

export async function POST(request: Request) {
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

  const upstream = await fetch(`${base}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(forwardBody),
  });

  return new Response(upstream.body, {
    status: upstream.ok ? 200 : upstream.status,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}
