// L'enregistrement des conversations (web/static/chat/store.js), avec le
// vrai Vue et un proxy simulé (fetch) : ce qui est mémorisé comme
// enregistré est ce qui est parti, et une révision dépassée suspend
// l'enregistrement au lieu d'écraser l'autre onglet.
import { test } from "vitest";
import assert from "node:assert/strict";
import { listeners } from "./setup.mjs";

// Le proxy simulé : la dernière version gardée et sa révision, par id.
const server = new Map();
const puts = [];
let gate = null;                     // une promesse qui retient le PUT
globalThis.fetch = async (url, opts = {}) => {
  const json = (status, body, headers = {}) => ({ ok: status < 400, status, headers, json: async () => body });
  const m = String(url).match(/^\/ui\/conversations\/([^?]+)$/);
  if (opts.method === "PUT" && m) {
    puts.push({ id: m[1], body: opts.body, ifMatch: opts.headers["if-match"], keepalive: opts.keepalive });
    if (gate) await gate;
    const cur = server.get(m[1]);
    const rev = cur ? cur.rev : 0;
    if (opts.headers["if-match"] !== undefined && Number(opts.headers["if-match"]) !== rev) {
      return json(409, { error: { message: "conversation modifiée ailleurs" } });
    }
    server.set(m[1], { doc: JSON.parse(opts.body), rev: rev + 1 });
    return json(200, { id: m[1], saved: true, rev: rev + 1 });
  }
  if ((!opts.method || opts.method === "GET") && m) {
    const cur = server.get(m[1]);
    return cur ? json(200, { ...cur.doc, rev: cur.rev }) : json(404, { error: { message: "inconnue" } });
  }
  if (String(url).startsWith("/ui/conversations?")) {
    return json(200, { data: [], next: null, usage: { conversations: 0 } });
  }
  return json(404, {});
};

const { ChatStore: S } = await import("../../llm_proxy/ui/web/static/chat/store.js");
const tick = () => new Promise((r) => setTimeout(r, 0));

await S.open("k");
S.newConv();
const conv = S.state.convs.find((c) => c.id === S.state.currentId);
conv.messages.push({ role: "user", content: "un" });

test("un changement pendant l'envoi n'est pas marqué enregistré", async () => {
  let open;
  gate = new Promise((r) => { open = r; });
  const first = S.persist();
  await tick();
  conv.messages.push({ role: "assistant", content: "deux" });   // pendant le PUT
  open();
  gate = null;
  await first;
  await S.persist();
  const last = JSON.parse(puts.at(-1).body);
  assert.equal(last.messages.length, 2, "le second état est reparti");
  assert.equal(server.get(conv.id).doc.messages.length, 2);
  assert.equal(server.get(conv.id).rev, 2);
  assert.equal(puts.at(-1).ifMatch, "1");                       // appuyé sur la révision rendue
  assert.equal("rev" in last, false);
});

test("une révision dépassée suspend l'enregistrement ; reprendre l'autre version", async () => {
  // Un autre onglet enregistre par-dessus.
  server.set(conv.id, { doc: { ...server.get(conv.id).doc, title: "ailleurs" }, rev: 7 });
  conv.messages.push({ role: "user", content: "trois" });
  await S.persist();
  assert.deepEqual(S.state.conflict, { id: conv.id, title: conv.title });
  const n = puts.length;
  conv.messages.push({ role: "user", content: "quatre" });
  await S.persist();
  assert.equal(puts.length, n, "rien ne part pendant le conflit");
  await S.resolveConflict("reload");
  assert.equal(S.state.conflict, null);
  const now = S.state.convs.find((c) => c.id === conv.id);
  assert.equal(now.title, "ailleurs");
  assert.equal(now.stub, undefined);
  now.messages.push({ role: "user", content: "cinq" });
  await S.persist();
  assert.equal(puts.at(-1).ifMatch, "7");
  assert.equal(server.get(conv.id).rev, 8);
});

test("garder celle-ci : enregistrée sans contrôle", async () => {
  const c = S.state.convs.find((x) => x.id === conv.id);
  server.set(conv.id, { doc: server.get(conv.id).doc, rev: 20 });
  c.messages.push({ role: "user", content: "six" });
  await S.persist();
  assert.ok(S.state.conflict);
  await S.resolveConflict("overwrite");
  assert.equal(S.state.conflict, null);
  assert.equal(puts.at(-1).ifMatch, undefined);
  assert.equal(server.get(conv.id).rev, 21);
  assert.equal(server.get(conv.id).doc.messages.at(-1).content, "six");
});

test("page cachée : l'enregistrement part en keepalive", async () => {
  const c = S.state.convs.find((x) => x.id === conv.id);
  c.messages.push({ role: "user", content: "sept" });
  await tick();                      // le watcher pose le délai
  document.visibilityState = "hidden";
  listeners.visibilitychange();
  document.visibilityState = "visible";
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(puts.at(-1).keepalive, true);
});
