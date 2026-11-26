// Les messages d'une requête du chat (web/static/chat/api.js) et les tours
// d'outils (chat/turn.js) : la règle « ajout seul » — une requête commence
// exactement comme la précédente.
import "./setup.mjs";
import { test } from "vitest";
import assert from "node:assert/strict";

const { apiContent, apiMessages, requestTools } = await import("../../llm_proxy/ui/web/static/chat/api.js");
const { chipFor, recordCall, endStep, closeStep, timeline } = await import("../../llm_proxy/ui/web/static/chat/turn.js");
const { take } = await import("../../llm_proxy/ui/web/static/chat/stream.js");
const { prompt } = await import("../../llm_proxy/ui/web/static/lib/prompts.js");

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
  assert.ok(after[1].content.endsWith("\n\n" + prompt("messages.state", { state: "artifact A" })));
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
  assert.deepEqual(ctx.hosted, ["web_search", "web_fetch"], "un programme du Code Mode ne joint que les outils présentés");
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

// Une réponse en plusieurs tours, simulée comme ChatPanel la mène : le
// modèle réfléchit et écrit, appelle un outil, le tour se clôt.
function turnOf(m, thought, text, id) {
  m.reasoning += thought;
  const start = m.content.length;
  m.content += text;
  if (!id) return;
  m.step = { content: m.content.slice(start), calls: [], queue: [{ id, name: "read_artifact", arguments: "{}" }] };
  const chip = chipFor(m, m.step.queue[0]);
  recordCall(m, chip, m.step.queue[0], "ok " + id);
  endStep(m);
}

test("chronologie : chaque tour a sa réflexion, son texte et ses puces, le tour en cours en dernier", () => {
  const m = { content: "", reasoning: "", tools: [] };
  turnOf(m, "je lis. ", "Je lis le fichier.", "a");
  turnOf(m, "je corrige. ", "", "b");
  turnOf(m, "presque. ", "Corrigé, voici", "");
  const t = timeline(m);
  assert.equal(t.head, "");
  assert.deepEqual(t.segments.map((s) => [s.reasoning, s.content.trim(), s.tools.map((c) => c.id)]), [
    ["je lis. ", "Je lis le fichier.", ["a"]],
    ["je corrige. ", "", ["b"]],
    ["presque. ", "Corrigé, voici", []],
  ]);
  // Un tour en cours (outil qui tourne, ou une carte qui attend) : ses
  // puces vont avec lui, au dernier segment.
  m.step = { content: "Corrigé, voici", calls: [], queue: [{ id: "c", name: "ask_user", arguments: "{}" }] };
  chipFor(m, m.step.queue[0]);
  assert.deepEqual(timeline(m).segments.at(-1).tools.map((c) => c.id), ["c"]);
  // Un tour clos par un arrêt garde aussi sa place.
  closeStep(m, "Arrêté.");
  m.reasoning += "fin. ";
  const after = timeline(m).segments;
  assert.deepEqual(after.map((s) => s.tools.map((c) => c.id)), [["a"], ["b"], ["c"], []]);
  assert.equal(after.at(-1).reasoning, "fin. ");
});

test("chronologie d'une réponse d'avant (tours sans positions) : réflexion en tête, textes et puces par tour", () => {
  const old = { reasoning: "tout le raisonnement", content: "Je lis.\n\nJe corrige.\n\nFini.",
                tools: [{ id: "a" }, { id: "b" }],
                steps: [{ content: "Je lis.", calls: [{ id: "a" }] }, { content: "Je corrige.", calls: [{ id: "b" }] }] };
  const t = timeline(old);
  assert.equal(t.head, "tout le raisonnement");
  assert.deepEqual(t.segments.map((s) => [s.reasoning, s.content.trim(), s.tools.map((c) => c.id)]),
    [["", "Je lis.", ["a"]], ["", "Je corrige.", ["b"]], ["", "Fini.", []]]);
  // Un texte de tour introuvable : le rendu d'un seul tenant, comme avant.
  const odd = { ...old, steps: [{ content: "ailleurs", calls: [{ id: "a" }] }] };
  assert.deepEqual(timeline(odd).segments.map((s) => [s.content, s.tools.map((c) => c.id)]),
    [[old.content, ["a", "b"]]]);
  // Une puce d'outil HÉBERGÉ (le proxy l'exécute : elle n'est dans aucun
  // tour rejoué) reste au tour où elle est arrivée : celui de la puce de
  // page qui la suit.
  const hosted = { ...old, tools: [{ id: "a" }, { id: "h" }, { id: "b" }] };
  assert.deepEqual(timeline(hosted).segments.map((s) => s.tools.map((c) => c.id)), [["a"], ["h", "b"], []]);
  // Sans tour : un seul segment, sa réflexion avec lui.
  assert.deepEqual(timeline({ reasoning: "r", content: "c", tools: [] }),
    { head: "", segments: [{ reasoning: "r", content: "c", tools: [], start: 0 }] });
});

// Un morceau du flux de /ui/chat/completions.
const delta = (d) => ({ choices: [{ delta: d }] });

test("chronologie : une puce d'outil hébergé garde sa place, avant le tour de la page qui la suit ; des positions qui ne tiennent plus → repli", () => {
  const m = { content: "", reasoning: "", tools: [], sources: [] };
  m.reasoning += "je cherche. ";
  m.content += "Je cherche.";
  take(delta({ x_hosted_tool: { id: "web", name: "web_search", status: "done" } }), m);
  m.step = { content: "Je cherche.", calls: [], queue: [{ id: "a", name: "read_artifact", arguments: "{}" }] };
  recordCall(m, chipFor(m, m.step.queue[0]), m.step.queue[0], "ok");
  endStep(m);
  turnOf(m, "fin. ", "Voilà.", "");
  assert.deepEqual(timeline(m).segments.map((s) => s.tools.map((c) => c.id)), [["web"], ["a"], []]);
  // Une réflexion plus courte que ses positions : la découpe repart des
  // textes des tours, la réflexion en tête.
  const short = { ...m, reasoning: "r" };
  const t = timeline(short);
  assert.equal(t.head, "r");
  assert.deepEqual(t.segments.map((s) => [s.content.trim(), s.tools.map((c) => c.id)]),
    [["Je cherche.", ["web", "a"]], ["Voilà.", []]]);
  // Une coupure qui ne tient plus (texte remplacé, réflexion raccourcie) :
  // les tours seuls découpent.
  const stale = { ...m, cuts: [{ contentEnd: 99, reasoningEnd: 0, toolsEnd: 1 }] };
  assert.deepEqual(timeline(stale).segments.map((s) => s.tools.map((c) => c.id)), [["web", "a"], []]);
  // Une réponse non diffusée remplace tout le texte : ni les positions ni
  // les textes des tours ne tiennent, le rendu d'un seul tenant.
  const replaced = { ...m, content: "Une réponse entièrement remplacée." };
  assert.deepEqual(timeline(replaced).segments.map((s) => [s.content, s.tools.length]),
    [["Une réponse entièrement remplacée.", 2]]);
});

test("chronologie : les outils hébergés d'un même tour de la page s'affichent entre la réflexion qui les appelle et ce qui suit", () => {
  // Un tour de la page (device_context), puis le proxy enchaîne seul deux
  // recherches et la réponse : tout arrive dans le dernier tour de la page.
  const m = { content: "", reasoning: "", tools: [], sources: [] };
  turnOf(m, "l'heure. ", "", "dev");
  take(delta({ reasoning_content: "je cherche la version. " }), m);
  take(delta({ x_hosted_tool: { id: "w1", name: "web_search", status: "running" } }), m);
  take(delta({ x_hosted_tool: { id: "w2", name: "web_search", status: "running" } }), m);
  take(delta({ x_hosted_tool: { id: "w1", status: "done" } }), m);
  take(delta({ x_hosted_tool: { id: "w2", status: "done" } }), m);
  take(delta({ reasoning_content: "je peux répondre. " }), m);
  take(delta({ content: "Chrome 158 est à jour." }), m);
  assert.deepEqual(timeline(m).segments.map((s) => [s.reasoning, s.content, s.tools.map((c) => c.id)]), [
    ["l'heure. ", "", ["dev"]],
    ["je cherche la version. ", "", ["w1", "w2"]],
    ["je peux répondre. ", "Chrome 158 est à jour.", []],
  ]);
  // Sans tour de la page du tout, la coupure vaut aussi.
  const solo = { content: "", reasoning: "", tools: [], sources: [] };
  take(delta({ content: "Je cherche." }), solo);
  take(delta({ x_hosted_tool: { id: "w", name: "web_search", status: "done" } }), solo);
  take(delta({ content: " Trouvé." }), solo);
  assert.deepEqual(timeline(solo).segments.map((s) => [s.content, s.tools.map((c) => c.id)]),
    [["Je cherche.", ["w"]], [" Trouvé.", []]]);
});
