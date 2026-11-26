// Les outils des ARTIFACTS (chat/tools/page/artifacts.js), par leur
// entrée publique (TOOL.run) : un artifact d'un seul fichier, et un
// PROJET de plusieurs fichiers — créé, lu, cherché, retouché fichier par
// fichier. Les bornes viennent des `settings` de la déclaration (côté
// proxy) : le test en donne de petites.
import "./setup.mjs";
import { test } from "vitest";
import assert from "node:assert/strict";
import * as Vue from "vue";

const { FAMILIES } = await import("../../llm_proxy/ui/web/static/chat/tools/page/artifacts.js");
const bridge = await import("../../llm_proxy/ui/web/static/chat/artifact-bridge.js");
const { entryOf } = await import("../../llm_proxy/ui/web/static/chat/artifacts.js");
const { default: ArtifactPanel } = await import("../../llm_proxy/ui/web/static/chat/ArtifactPanel.vue");

const SETTINGS = { types: ["html", "markdown", "svg", "mermaid", "code", "project"],
                   max_chars: 200, max_files: 3, slice_lines: 50, max_slice: 200, context: 1, max_context: 5 };
const tool = (name) => {
  const t = FAMILIES.flatMap((f) => f.tools).find((x) => x.name === name);
  t.settings = SETTINGS;
  return t;
};
const run = (name, args, conv) => {
  const r = tool(name).run(args, { conv });
  return typeof r === "string" ? r : r.result;
};
const runAsync = async (name, args, conv) => {
  const r = await tool(name).run(args, { conv });
  return typeof r === "string" ? r : r.result;
};
const last = (conv, id) => conv.artifacts.find((a) => a.id === id).versions.at(-1);
const isError = (r) => typeof r === "string" && r.startsWith("Error:");

const PROJECT = [
  { path: "serveur.py", content: "import http.server\nprint('ok')\n" },
  { path: "static/index.html", content: "<link rel=stylesheet href=style.css><h1>Hi</h1>" },
  { path: "static/style.css", content: "h1 { color: red }" },
];

// ── un seul fichier ─────────────────────────────────────────────────────

test("un artifact d'un fichier : montré, lu, cherché, retouché en une nouvelle version", () => {
  const conv = {};
  assert.ok(!isError(run("show_artifact", { id: "note", title: "Note", type: "markdown", content: "# A\nb\nc" }, conv)));
  assert.match(run("read_artifact", { id: "note" }, conv), /# A\nb\nc$/);
  assert.match(run("grep_artifact", { id: "note", pattern: "b" }, conv), /2→ b/);
  assert.ok(!isError(run("edit_artifact", { id: "note", edits: [{ old_string: "b", new_string: "B" }] }, conv)));
  assert.equal(conv.artifacts[0].versions.length, 2);
  assert.equal(last(conv, "note").content, "# A\nB\nc");
  assert.ok(isError(run("show_artifact", { title: "Long", type: "code", content: "x".repeat(201) }, conv)),
            "max_chars des settings");
});

// ── un projet ───────────────────────────────────────────────────────────

test("un projet : ses fichiers gardés ; lu sans chemin, la liste ; avec, le fichier ou une tranche", () => {
  const conv = {};
  assert.ok(!isError(run("show_artifact", { id: "srv", title: "Serveur", type: "project", files: PROJECT }, conv)));
  const v = last(conv, "srv");
  assert.equal(v.type, "project");
  assert.deepEqual(v.files.map((f) => f.path), ["serveur.py", "static/index.html", "static/style.css"]);
  const listing = run("read_artifact", { id: "srv" }, conv);
  for (const f of PROJECT) assert.ok(listing.includes(f.path), f.path);
  assert.ok(!listing.includes("import http.server"), "la liste, pas les contenus");
  assert.match(run("read_artifact", { id: "srv", path: "serveur.py" }, conv), /import http\.server\nprint\('ok'\)\n$/);
  assert.match(run("read_artifact", { id: "srv", path: "serveur.py", start_line: 2, end_line: 2 }, conv), /2.*print\('ok'\)/);
  assert.ok(isError(run("read_artifact", { id: "srv", path: "absent.py" }, conv)), "fichier inconnu");
});

test("un projet : la recherche couvre tous ses fichiers, chaque ligne préfixée par son chemin", () => {
  const conv = {};
  run("show_artifact", { id: "srv", title: "Serveur", type: "project", files: PROJECT }, conv);
  const all = run("grep_artifact", { id: "srv", pattern: "h1" }, conv);
  assert.match(all, /static\/index\.html/);
  assert.match(all, /static\/style\.css/);
  assert.ok(!/serveur\.py/.test(all.split("\n").slice(1).join("\n")), "pas de ligne d'un fichier sans résultat");
  const one = run("grep_artifact", { id: "srv", pattern: "h1", path: "static/style.css" }, conv);
  assert.ok(one.includes("static/style.css") && !one.includes("index.html"));
});

test("un projet : retouche, ajout, retrait d'un fichier — chacun une version, les autres intacts", () => {
  const conv = {};
  run("show_artifact", { id: "srv", title: "Serveur", type: "project", files: PROJECT }, conv);
  assert.ok(!isError(run("edit_artifact", { id: "srv", path: "static/style.css",
                                            edits: [{ old_string: "red", new_string: "blue" }] }, conv)));
  let v = last(conv, "srv");
  assert.equal(v.files.find((f) => f.path === "static/style.css").content, "h1 { color: blue }");
  assert.equal(v.files.find((f) => f.path === "serveur.py").content, PROJECT[0].content);
  assert.ok(!isError(run("edit_artifact", { id: "srv", path: "static/style.css", delete: true }, conv)));
  assert.ok(!isError(run("edit_artifact", { id: "srv", path: "README.md", content: "# Lisez-moi" }, conv)));
  v = last(conv, "srv");
  assert.deepEqual(v.files.map((f) => f.path).sort(), ["README.md", "serveur.py", "static/index.html"]);
  assert.equal(conv.artifacts[0].versions.length, 4);
  assert.equal(conv.artifacts[0].versions[0].files.length, 3, "une ancienne version ne change pas");
});

test("un projet mal formé ou une retouche sans fichier sont refusés, rien n'est ajouté", () => {
  const cases = {
    "chemin ..": [{ path: "../x.py", content: "x" }],
    "chemin absolu": [{ path: "/x.py", content: "x" }],
    "segment vide": [{ path: "a//b.py", content: "x" }],
    "caractère interdit": [{ path: "a b.py", content: "x" }],
    "doublon": [{ path: "a.py", content: "x" }, { path: "a.py", content: "y" }],
    "doublon à la casse près": [{ path: "A.py", content: "x" }, { path: "a.py", content: "y" }],
    "fichier aussi dossier": [{ path: "a", content: "x" }, { path: "a/b", content: "y" }],
    "trop de fichiers": ["a", "b", "c", "d"].map((p) => ({ path: p, content: "x" })),
    "trop long au total": [{ path: "a", content: "x".repeat(150) }, { path: "b", content: "x".repeat(60) }],
    "aucun fichier": [],
    "pas une liste": "a.py",
  };
  for (const [name, files] of Object.entries(cases)) {
    const conv = {};
    assert.ok(isError(run("show_artifact", { id: "p", title: "P", type: "project", files }, conv)), name);
    assert.ok(!(conv.artifacts || []).length, name);
  }
  const conv = {};
  run("show_artifact", { id: "srv", title: "Serveur", type: "project", files: PROJECT }, conv);
  const refused = {
    "retouche sans chemin": { id: "srv", edits: [{ old_string: "ok", new_string: "OK" }] },
    "fichier inconnu": { id: "srv", path: "absent.py", edits: [{ old_string: "a", new_string: "b" }] },
    "ajout trop long": { id: "srv", path: "gros.txt", content: "x".repeat(200) },
    "ajout au-delà du nombre": { id: "srv", path: "quatre.txt", content: "x" },
    "chemin invalide": { id: "srv", path: "../x", content: "x" },
    "ajout d'un doublon à la casse près": { id: "srv", path: "SERVEUR.py", content: "x" },
    "ajout d'un fichier sous un fichier": { id: "srv", path: "serveur.py/x", content: "x" },
    "content et edits ensemble": { id: "srv", path: "serveur.py", content: "x", edits: [{ old_string: "ok", new_string: "OK" }] },
    "content qui n'est pas du texte": { id: "srv", path: "serveur.py", content: 42 },
  };
  for (const [name, args] of Object.entries(refused)) {
    assert.ok(isError(run("edit_artifact", args, conv)), name);
    assert.equal(conv.artifacts[0].versions.length, 1, name);
  }
});

test("un artifact d'un fichier refuse ce qui n'est que pour un projet", () => {
  const conv = {};
  run("show_artifact", { id: "note", title: "Note", type: "markdown", content: "# A" }, conv);
  for (const [name, args] of Object.entries({
    "path": { id: "note", path: "a.md", edits: [{ old_string: "A", new_string: "B" }] },
    "content": { id: "note", content: "# B" },
    "delete": { id: "note", path: "a.md", delete: true },
  })) {
    assert.ok(isError(run("edit_artifact", args, conv)), name);
    assert.equal(conv.artifacts[0].versions.length, 1, name);
  }
});

test("l'état du tour dit le nombre de fichiers d'un projet", () => {
  const conv = {};
  run("show_artifact", { id: "srv", title: "Serveur", type: "project", files: PROJECT }, conv);
  const family = FAMILIES.find((f) => f.tools.some((t) => t.name === "show_artifact"));
  assert.match(family.state({ conv }), /"srv".*3 files/);
});

// ── déboguer un projet ──────────────────────────────────────────────────

// La frontière extérieure du pont : le CADRE de l'artifact (une fenêtre qui
// répond aux messages de la page) et la page qui ouvre le panneau
// (setOpener). Le panneau, lui, montre l'aperçu de l'entrée (entryOf) de
// l'artifact que le pont attend (`wanted`).
function fakePanel() {
  const target = new EventTarget();
  globalThis.addEventListener = target.addEventListener.bind(target);
  globalThis.removeEventListener = target.removeEventListener.bind(target);
  const opened = [];
  const win = { postMessage(msg) {
    queueMicrotask(() => {
      const e = new Event("message");
      Object.assign(e, { source: win, data: msg.op === "run"
        ? { llmBridge: msg.llmBridge, value: "42", logs: [] }
        : { llmBridge: msg.llmBridge, title: "Page", size: [800, 600], scroll: [0, 0, 900], logs: [], outline: "body" } });
      target.dispatchEvent(e);
    });
  } };
  const frame = { contentWindow: win };
  bridge.setOpener((id) => {
    opened.push({ id, wanted: bridge.wanted.value && bridge.wanted.value.id });
    queueMicrotask(() => bridge.register(id, 1, frame));
  });
  return { opened, done: () => bridge.unregister(frame) };
}

test("déboguer un projet vise l'aperçu de son entrée ; sans HTML, refusé clairement", async () => {
  assert.equal(entryOf({ files: PROJECT }).path, "static/index.html", "sans index.html à la racine : le premier .html");
  assert.equal(entryOf({ files: [...PROJECT, { path: "index.html", content: "x" }] }).path, "index.html");
  const panel = fakePanel();
  try {
    const conv = {};
    run("show_artifact", { id: "srv", title: "Serveur", type: "project", files: PROJECT }, conv);
    assert.match(await runAsync("inspect_artifact", { id: "srv" }, conv), /800 × 600/);
    assert.match(await runAsync("run_in_artifact", { id: "srv", code: "return 42" }, conv), /42/);
    assert.deepEqual(panel.opened[0], { id: "srv", wanted: "srv" }, "le panneau est ouvert sur le projet, que le pont attend");
    run("show_artifact", { id: "py", title: "Script", type: "project", files: [PROJECT[0]] }, conv);
    for (const name of ["inspect_artifact", "run_in_artifact"]) {
      const r = await runAsync(name, { id: "py", code: "return 1" }, conv);
      assert.ok(isError(r) && /pas de fichier HTML/.test(r), name);
    }
    assert.ok(!panel.opened.some((o) => o.id === "py"), "rien n'est ouvert pour un projet sans HTML");
  } finally { panel.done(); }
});

test("le panneau montre l'entrée du projet que le pont attend : ouvert après la demande, puis à chaque demande", async () => {
  // Le panneau n'est pas encore là quand le pont demande l'artifact (le
  // tour s'ouvre panneau fermé) : il la prend en s'ouvrant. Puis
  // l'utilisateur regarde un autre fichier (le cadre de l'aperçu n'est
  // plus inscrit), et le modèle redemande.
  const fake = fakePanel();
  const conv = {};
  run("show_artifact", { id: "srv", title: "Serveur", type: "project",
                         files: [PROJECT[0], PROJECT[1], { path: "index.html", content: "<p>racine</p>" }] }, conv);
  const pending = [bridge.call("srv", 1, "inspect")];
  const scope = Vue.effectScope();
  const panel = scope.run(() => ArtifactPanel.setup(Vue.reactive({ artifacts: conv.artifacts, selected: "srv" }),
                                                    { emit: () => {} }));
  try {
    await Vue.nextTick();
    assert.equal(panel.path.value, "index.html");
    assert.equal(bridge.wanted.value, null, "la demande traitée est vidée");
    panel.path.value = "serveur.py";
    panel.showSource.value = true;
    fake.done();
    pending.push(bridge.call("srv", 1, "inspect"));
    await Vue.nextTick();
    assert.equal(panel.path.value, "index.html", "seconde demande : l'entrée, de nouveau");
    assert.equal(panel.showSource.value, false, "en aperçu");
    await Promise.all(pending);
  } finally { scope.stop(); fake.done(); }
});
