/**
 * wavr-diag — the receiving end of Wavr's OPT-IN diagnostics reporting.
 * Cloudflare PAGES Function (https://wavr-diag.pages.dev/report) — project-scoped
 * URL, no account subdomain in the name. Same contract as the original Worker:
 *
 *   POST /report  {"schema": 1, "report": "<MAC-redacted text ≤64KB>"}  -> {"ok": true}
 *   other methods -> 405. No GET listing — reports are write-only from the wild.
 *
 * Privacy posture:
 *   - The client only ever sends the MAC-redacted plain-text report (no device
 *     identities, house maps, or credentials — enforced client-side in diag.py).
 *   - Defense in depth: we re-redact MAC-shaped tokens AGAIN on arrival (OUI kept,
 *     host half masked — identical to diag.py's redact_macs) before storing.
 *   - We deliberately persist NO client IP / UA metadata of our own (Cloudflare's
 *     edge logs are outside this function's control; it stores report+ts only).
 *   - PUBLICATION NOTE. There is no authentication and no rate limit here, and
 *     that was a reasonable trade while the repository holding this URL was
 *     private: the endpoint was known to one person. Publishing the repository
 *     publishes the URL and this file, which makes it an OPEN write path into
 *     the operator's KV namespace. Nothing sensitive is exposed by that — the
 *     payload is re-redacted on arrival and no client metadata is kept — but
 *     the daily write quota is finite and somebody else can spend it.
 *
 *     The fix belongs in front of this function, not inside it: a rate-limiting
 *     rule on the route, configured where the project is hosted. Adding
 *     untested counter logic to a live receiver would be the worse trade. This
 *     note exists so the decision is made at publication time rather than
 *     discovered afterwards.
 *   - Reports expire from KV after 90 days (expirationTtl) — debugging data, not
 *     an archive.
 */
const MAX_BYTES = 64 * 1024;
// aa:bb:cc:dd:ee:ff -> aa:bb:cc:**:**:**
const MAC_RE = /\b([0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}\2[0-9A-Fa-f]{2})\2[0-9A-Fa-f]{2}\2[0-9A-Fa-f]{2}\2[0-9A-Fa-f]{2}\b/g;
const TTL_S = 90 * 24 * 3600;

export async function onRequest({ request, env }) {
  if (request.method !== "POST") {
    return new Response("method not allowed", { status: 405 });
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return Response.json({ ok: false, error: "invalid json" }, { status: 400 });
  }
  const report = typeof body?.report === "string" ? body.report : null;
  if (!report || !report.trim()) {
    return Response.json({ ok: false, error: "report (string) required" }, { status: 422 });
  }
  const clean = report.replace(MAC_RE, "$1$2**$2**$2**").slice(0, MAX_BYTES);
  const key = `r:${new Date().toISOString()}:${crypto.randomUUID()}`;
  await env.DIAG.put(key, clean, { expirationTtl: TTL_S });
  return Response.json({ ok: true });
}
