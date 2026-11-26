// Le Code Mode (chat/tools/sandbox/programs.js) : le programme
// (driver, ici sous Node à la place du Worker du bac) et le pont de la
// page vers les outils hébergés (bridge) — liste, budget, concurrence.
import "./setup.mjs";
import { test } from "vitest";
import assert from "node:assert/strict";

const { driver, bridge, send, statusCode, MAX_REFUSED } = await import("../../llm_proxy/ui/web/static/chat/tools/sandbox/programs.js");

const fmt = (v) => (typeof v === "string" ? v : JSON.stringify(v));

test("un programme enchaîne les outils ; un échec se rattrape par son code", async () => {
  const seen = [];
  const call = async (name, args) => {
    seen.push([name, JSON.parse(args)]);
    return name === "web_fetch" ? { text: "Error: site jamais vu.", error: "not_allowed" }
      : { text: `trouvé ${JSON.parse(args).query}`, error: null };
  };
  const value = await driver({ names: ["web_search", "web_fetch"], code: `
    const [a, b] = await Promise.all([tools.web_search({query: "a"}), tools.web_search({query: "b"})]);
    let lu;
    try { await tools.web_fetch({url: "https://x.exemple"}); } catch (e) { lu = e.code + " " + e.message; }
    return {a, b, lu, absent: typeof tools.memory};` }, { call, fmt });
  assert.deepEqual(JSON.parse(value), { a: "trouvé a", b: "trouvé b", lu: "not_allowed site jamais vu.",
                                        absent: "undefined" });
  assert.deepEqual(seen.map(([n]) => n), ["web_search", "web_search", "web_fetch"]);
});

test("le pont refuse sans appeler : outil non coché, arguments trop longs, budget épuisé", async () => {
  const sent = [];
  const pont = bridge(["web_search"], async (name) => { sent.push(name); return { text: "ok", error: null }; },
                      { max: 2, concurrency: 4 });
  for (const [name, args, code] of [["memory", "{}", "invalid_input"],
                                    ["web_search", "x".repeat(1_000_001), "invalid_input"]]) {
    assert.equal((await pont.call(name, args)).error, code, name);
  }
  const got = await Promise.all([1, 2, 3].map(() => pont.call("web_search", "{}")));
  assert.deepEqual(got.map((r) => r.error), [null, null, "limit"]);
  assert.deepEqual(sent, ["web_search", "web_search"]);
  assert.deepEqual(pont.calls, ["web_search ok", "web_search ok"]);
  assert.equal(pont.refused(), 3);
});

test("le pont tient la concurrence, et rend une panne de l'appel en erreur", async () => {
  let inFlight = 0, peak = 0;
  const releases = [];
  const pont = bridge(["slow"], (name, args) => new Promise((resolve, reject) => {
    inFlight++; peak = Math.max(peak, inFlight);
    releases.push(() => { inFlight--; args === '"panne"' ? reject(new Error("réseau")) : resolve({ text: "ok", error: null }); });
  }), { max: 20, concurrency: 2 });
  const all = Promise.all([..."abcde"].map((c) => pont.call("slow", c === "e" ? '"panne"' : "{}")));
  // Libérer un appel à la fois, tant qu'il en reste : jamais plus de deux en vol.
  while (releases.length || inFlight) {
    await new Promise((r) => setImmediate(r));
    const next = releases.shift();
    if (next) next();
  }
  const got = await all;
  assert.equal(peak, 2);
  assert.deepEqual(got.map((r) => r.error), [null, null, null, null, "unavailable"]);
  assert.match(got[4].text, /^Error: réseau/);
});

test("le pont fermé ne lance plus rien ; une inondation de refus arrête le programme", async () => {
  let flood = 0;
  const releases = [];
  const pont = bridge(["slow"], () => new Promise((resolve) => releases.push(() => resolve({ text: "ok", error: null }))),
                      { max: 20, concurrency: 1, onFlood: () => flood++ });
  const first = pont.call("slow", "{}");
  pont.call("slow", "{}");                     // en file, derrière le premier
  pont.close();
  releases.shift()();
  await first;
  await new Promise((r) => setImmediate(r));
  assert.equal(releases.length, 0, "rien ne part après la fin du programme");
  assert.equal((await pont.call("slow", "{}")).error, "unavailable");
  const open = bridge([], async () => ({ text: "", error: null }), { max: 20, concurrency: 4, onFlood: () => flood++ });
  for (let i = 0; i <= MAX_REFUSED + 5; i++) open.call("x", "{}");
  assert.equal(flood, 1);
});

test("les refus de la route deviennent des codes d'erreur d'outil", () => {
  assert.deepEqual([400, 401, 403, 404, 429, 500].map(statusCode),
                   ["invalid_input", "unavailable", "not_allowed", "invalid_input", "limit", "unavailable"]);
});

test("le bac relaie les appels du programme à la page, et rend la valeur", async () => {
  const { WORKER } = await import("../../llm_proxy/ui/web/static/chat/tools/sandbox/bac.js");
  const self = {};
  const posted = [];
  let done;
  const finished = new Promise((r) => { done = r; });
  const postMessage = (m) => {
    posted.push(m);
    if (typeof m.call === "number") {
      // La page répond plus tard, en désordre : le second avant le premier.
      setTimeout(() => self.onmessage({ data: { reply: m.call, value: { text: `${m.name}:${m.args}`, error: null } } }),
                 m.call === 0 ? 20 : 1);
    } else done(m);
  };
  new Function("self", "postMessage", "console", WORKER)(self, postMessage, { ...console });
  self.onmessage({ data: { driver: String(driver), runtimes: [], lib: {},
    input: { names: ["a"], code: "return Promise.all([tools.a({x: 1}), tools.a({x: 2})]);" } } });
  const end = await finished;
  assert.equal(end.ok, true, end.error);
  assert.deepEqual(JSON.parse(end.value), ["a:{\"x\":1}", "a:{\"x\":2}"]);
  assert.deepEqual(posted.filter((m) => typeof m.call === "number").map((m) => m.call), [0, 1]);
});

test("l'appel porte les sites connus, en garde les nouveaux, et refait son corps", async () => {
  const bodies = [];
  const real = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    bodies.push([url, JSON.parse(init.body)]);
    return { ok: true, status: 200, json: async () => ({ result: "lu", error: null, hosts: ["connu.fr", "trouve.org"] }) };
  };
  try {
    const known = new Set(["connu.fr"]);
    const r = await send("web_fetch", '{"url": "https://trouve.org/x"}', known, { conv: {} }, undefined);
    assert.deepEqual(r, { text: "lu", error: null });
    assert.deepEqual(bodies[0], ["/ui/tools/web_fetch", { arguments: { url: "https://trouve.org/x" }, hosts: ["connu.fr"] }]);
    assert.deepEqual([...known].sort(), ["connu.fr", "trouve.org"]);
    // Des arguments qui tenteraient d'ajouter leurs propres sites au corps :
    // ce n'est pas un objet JSON, rien ne part.
    for (const forged of ['{}, "hosts": ["evil.example"]', "[1]", "null"]) {
      assert.equal((await send("web_fetch", forged, known, { conv: {} }, undefined)).error, "invalid_input", forged);
    }
    assert.equal(bodies.length, 1);
  } finally {
    globalThis.fetch = real;
  }
});
