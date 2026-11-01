// Phase 0 meter: logs xAI usage and outbound request counts per host/RPC method. Never logs URLs' query strings, headers or keys.
import dc from "node:diagnostics_channel";
import fs from "node:fs";
const OUT = process.env.METER_OUT;
const w = (o) => { if (OUT) fs.appendFileSync(OUT, JSON.stringify({ t: Date.now(), ...o }) + "\n"); };
function methods(body) {
  try { const s = typeof body === "string" ? body : Buffer.isBuffer(body) ? body.toString() : body instanceof Uint8Array ? Buffer.from(body).toString() : null;
    if (!s) return null; const j = JSON.parse(s); const a = Array.isArray(j) ? j : [j];
    return a.map((x) => x.method).filter(Boolean); } catch { return null; }
}
dc.subscribe("undici:request:create", ({ request }) => {
  try { const host = new URL(request.origin).host; if (host.includes("x.ai")) return;
    w({ kind: "req", host, path: String(request.path).split("?")[0].slice(0, 80), method: request.method, rpc: methods(request.body) }); } catch {}
});
dc.subscribe("undici:request:bodyChunkSent", ({ request, chunk }) => {
  try { const host = new URL(request.origin).host; if (!host.includes("helius")) return; const m = methods(chunk); if (m) w({ kind: "rpc", host, rpc: m }); } catch {}
});
dc.subscribe("undici:request:headers", ({ request, response }) => {
  try { const host = new URL(request.origin).host; if (host.includes("x.ai")) return; if (response.statusCode >= 400) w({ kind: "httperr", host, status: response.statusCode }); } catch {}
});
const of = globalThis.fetch;
globalThis.fetch = async (url, init) => {
  const u = String(url?.url ?? url); const t0 = Date.now();
  const res = await of(url, init);
  if (u.includes("api.x.ai") && u.includes("chat/completions")) {
    let model = null; try { model = JSON.parse(init.body).model; } catch {}
    res.clone().text().then((txt) => {
      let usage = null;
      try { usage = JSON.parse(txt).usage; } catch {
        for (const line of txt.split("\n")) { if (line.startsWith("data:") && line.includes('"usage"')) { try { const j = JSON.parse(line.slice(5)); if (j.usage) usage = j.usage; } catch {} } }
      }
      w({ kind: "llm", model, status: res.status, ms: Date.now() - t0, usage, err: res.status >= 400 ? txt.slice(0, 300) : undefined, sent: (() => { try { const b = JSON.parse(init.body); return { tools: !!b.tools, thinking: b.thinking, reasoning_effort: b.reasoning_effort, max_tokens: b.max_tokens, keys: Object.keys(b) }; } catch { return null; } })() });
    }).catch(() => w({ kind: "llm", model, status: res.status, err: "read" }));
  }
  return res;
};
