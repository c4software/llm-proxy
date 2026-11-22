// Les messages d'une requête du chat (web/static/chat/api.js) et les tours
// d'outils (chat/turn.js) : la règle « ajout seul » — une requête commence
// exactement comme la précédente.
import "./setup.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";

const { apiContent, apiMessages, requestTools } = await import("../../llm_proxy/ui/web/static/chat/api.js");
const { chipFor, recordCall, endStep, closeStep } = await import("../../llm_proxy/ui/web/static/chat/turn.js");

const user = (content, extra = {}) => ({ role: "user", content, ...extra });
const answer = (content, extra = {}) => ({ role: "assistant", content, ...extra });

test("pièces jointes : fichiers dans le texte, images en parties", () => {
  assert.equal(apiContent(user("q", { attachments: [{ kind: "text", name: 'a"b.txt', data: "x" }] })),
    "q\n\n<file name=\"a'b.txt\">\nx\n</file>");
  assert.deepEqual(apiContent(user("", { attachments: [{ kind: "image", data: "data:image/png;base64,AA" }] })),
    [{ type: "image_url", image_url: { url: "data:image/png;base64,AA" } }]);
});

test("ajout seul : un tour de plus ne change rien à ce qui précède", () => {
  const conv = { system: "Sois bref.", messages: [
    user("un", { state: "artifact A" }), answer("r1"),
    user("deux", { state: "artifact A" }), answer("", { error: "boom" }),
    user("trois", { state: "artifact B" }), answer("r3")] };
  const before = apiMessages(conv, 3, "consignes");
  const after = apiMessages(conv, 5, "consignes");
  assert.deepEqual(after.slice(0, before.length), before);
  assert.equal(after[0].content, "Sois bref.\n\nconsignes");
  // L'état n'est joint que quand il change ; la réponse en erreur n'est pas rejouée.
  assert.match(after[1].content, /\[État du moment, ajouté par la page\]\nartifact A/);
  assert.equal(after[3].content, "deux");
  assert.equal(after[4].role, "user");
  assert.match(after[4].content, /artifact B/);
});

test("après un résumé : lui au prompt système, rien de ce qui le précède", () => {
  const conv = { system: "", messages: [user("vieux"), answer("vieille"),
    { role: "summary", content: "RÉSUMÉ", error: "" }, user("neuf")] };
  const out = apiMessages(conv, 4);
  assert.equal(out.length, 2);
  assert.match(out[0].content, /RÉSUMÉ/);
  assert.equal(out[1].content, "neuf");
});

test("requestTools : pas de mémoire en incognito, rien d'hébergé sans la capacité", () => {
  const conv = { tools: ["web_search", "memory", "absent"], incognito: true };
  const ctx = { ws: {} };
  const groups = [{ type: "web_search", tools: ["web_search", "web_fetch"] }, { type: "memory", tools: ["memory"] }];
  const got = requestTools(conv, ctx, { caps: { hostedTools: true }, available: new Set(["web_search", "memory"]),
                                        families: [], groups });
  assert.deepEqual(got.hosted, ["web_search"]);
  assert.deepEqual(ctx.hosted, ["web_search", "web_fetch"], "un programme d'outils ne joint que les outils présentés");
  assert.equal(ctx.ws, null, "sans la capacité folder, le dossier n'est pas présenté");
  assert.deepEqual(requestTools({ ...conv, incognito: false }, {}, { caps: {}, available: new Set(["memory"]), families: [] }).hosted, []);
});

test("tours d'outils : appel fait, tour clos, tour laissé en plan rejouable", () => {
  const m = { content: "texte", tools: [{ id: "a", name: "run_js", status: "writing" }], steps: [] };
  m.step = { content: "texte", calls: [], queue: [{ id: "a", name: "run_js", arguments: "{}" },
                                                   { id: "b", name: "run_php", arguments: "{}" }] };
  const chip = chipFor(m, m.step.queue[0]);
  assert.equal(chip.status, "running");
  recordCall(m, chip, m.step.queue[0], "ok");
  assert.equal(chip.status, "done");
  assert.deepEqual(m.step.queue.map((q) => q.id), ["b"]);
  chipFor(m, m.step.queue[0]);
  closeStep(m, "Arrêté par l'utilisateur.");
  assert.equal(m.step, null);
  assert.deepEqual(m.steps[0].calls.map((c) => [c.id, c.result]),
    [["a", "ok"], ["b", "Error: Arrêté par l'utilisateur."]]);
  assert.equal(m.tools.find((t) => t.id === "b").status, "error");
  // Une carte qui attend : rien n'est clos.
  const w = { tools: [], step: { content: "", calls: [], queue: [{ id: "c" }] }, waiting: { id: "c" } };
  closeStep(w, "x");
  assert.ok(w.step);
  // endStep : le tour rejoint les passés, le texte continue à la ligne.
  const e = { content: "fin", tools: [], step: { content: "fin", calls: [], queue: [] } };
  endStep(e);
  assert.equal(e.content, "fin\n\n");
  assert.equal(e.steps.length, 1);
});
