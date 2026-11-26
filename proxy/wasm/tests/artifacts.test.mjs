// Le document d'un artifact (web/static/chat/artifacts.js) : sa politique
// de contenu passe AVANT tout ce que l'artifact contient. Vérifié dans
// Chrome à l'écriture : un artifact sans <head> mais avec un <header>
// recevait sa <meta> CSP dans le corps, où elle est ignorée.
import { test } from "vitest";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import hljs from "highlight.js/lib/common";
import "./setup.mjs";
import { artifactDoc, artifactFile, entryOf, fileDoc, fileTree, MAX_HIGHLIGHT, projectDoc }
  from "../../llm_proxy/ui/web/static/chat/artifacts.js";

const PREFIX = /^<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none';/;

for (const [name, content] of Object.entries({
  "sans head, avec header": "<header>titre</header><script>fetch('https://x')</script>",
  "head dans un commentaire": "<!-- <head> --><script>1</script><html><head></head></html>",
  "document complet": "<!DOCTYPE html><html lang=fr><head><title>T</title></head><body></body></html>",
})) {
  test(`HTML ${name} : la politique en premier`, () => {
    const doc = artifactDoc({ type: "html", content });
    assert.match(doc, PREFIX);
    assert.ok(doc.endsWith(content));
  });
}

test("réseau ouvert : la politique ouverte, toujours en premier", () => {
  assert.match(artifactDoc({ type: "html", content: "<p>x" }, { network: true }),
    /^<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \*/);
});

test("débogage : le pont suit la politique, avant l'artifact", () => {
  const doc = artifactDoc({ type: "html", content: "<p>x</p>" }, { debug: true });
  assert.ok(doc.indexOf("Content-Security-Policy") < doc.indexOf("<script>(") );
  assert.ok(doc.indexOf("<script>(") < doc.indexOf("<p>x</p>"));
});

test("code : colorié par la page selon son langage, détecté sinon ; le cadre ne charge rien", () => {
  const hl = { hljs, css: readFileSync(join(import.meta.dirname, "../node_modules/highlight.js/styles/github.css"), "utf8") };
  const python = "import os\n\ndef f(x):\n    if x is None:\n        return []\n    return [os.path.basename(y) for y in x]\n";
  for (const language of ["python", "PY", "langage-inconnu", "", 'x" onload="alert(1)']) {
    const doc = artifactDoc({ type: "code", language, content: python }, { hl });
    assert.match(doc, /<code class="hljs"><span class="hljs-keyword">import<\/span> os\n\n<span class="hljs-keyword">def<\/span>/, language);
    assert.ok(!doc.includes("onload") && !doc.includes("<script") && !doc.includes("<link"), language);
  }
  const piege = artifactDoc({ type: "code", language: "python", content: "s = '</code><script>alert(1)</script>'" }, { hl });
  assert.ok(!piege.includes("<script>") && piege.includes("&lt;/code&gt;&lt;script&gt;"));
  // C# lu comme C# (son alias), pas comme du C : `var` y est un mot-clé.
  assert.match(artifactDoc({ type: "code", language: "c#", content: "var x = 1;" }, { hl }),
    /<span class="hljs-keyword">var<\/span>/);
  // Le langage demandé l'emporte sur la détection.
  assert.doesNotMatch(artifactDoc({ type: "code", language: "plaintext", content: python }, { hl }), /<span class="hljs-keyword">/);
  // highlight.js pas encore chargé, absent du serveur, ou code trop long : brut.
  assert.match(artifactDoc({ type: "code", language: "python", content: "x < 1" }), /<pre><code>x &lt; 1<\/code><\/pre>/);
  assert.doesNotMatch(artifactDoc({ type: "code", language: "python", content: python.repeat(MAX_HIGHLIGHT / python.length + 1) }, { hl }),
    /<code class="hljs">/);
});

// ── un projet ───────────────────────────────────────────────────────────

const PROJECT = { type: "project", at: Date.UTC(2026, 10, 26, 10, 30), files: [
  { path: "serveur.py", content: "import http.server\n" },
  { path: "static/index.html", content: '<link rel="stylesheet" href="style.css"><script src="./app.js"></script>'
    + '<link rel="stylesheet" href="https://cdn.example/x.css"><h1>Bonjour</h1>' },
  { path: "static/style.css", content: "h1 { color: red }" },
  { path: "static/app.js", content: "console.log('</script>')" },
] };

test("projet : l'aperçu d'un HTML met en ligne le CSS et le JS du projet, la politique en premier", () => {
  assert.equal(entryOf(PROJECT).path, "static/index.html");
  assert.equal(entryOf({ ...PROJECT, files: PROJECT.files.filter((f) => !f.path.endsWith(".html")) }), null);
  const doc = projectDoc(PROJECT, "static/index.html", { debug: true });
  assert.match(doc, PREFIX);
  assert.match(doc, /<style>h1 \{ color: red \}<\/style>/);
  assert.ok(doc.includes("<script>console.log('<\\/script>')</script>"), "un </script> du fichier ne ferme pas la balise");
  assert.ok(doc.includes('href="https://cdn.example/x.css"'), "un lien hors du projet reste tel quel");
  assert.ok(doc.indexOf("Content-Security-Policy") < doc.indexOf("<style>h1"));
});

test("projet : un script mis en ligne garde ses attributs, différé en fin de document ; rien ne s'échappe de sa balise", () => {
  const files = [
    { path: "index.html", content: '<head><script type="module" src="m.js"></script><script defer src="d.js"></script>'
      + '<link rel="stylesheet" href="s.css"></head><body><p id="x">corps</p></body>' },
    { path: "m.js", content: "import { a } from './a.js';" },
    { path: "d.js", content: "if (a <!-- b) x();" },
    { path: "s.css", content: "p { color: red } </STYLE><script>alert(1)</script>" },
  ];
  const doc = projectDoc({ type: "project", files }, "index.html");
  assert.match(doc, /<script type="module">import \{ a \}/, "type=module gardé");
  assert.ok(doc.indexOf("x();") > doc.indexOf('<p id="x">corps</p>'), "un script defer s'exécute après le corps");
  assert.ok(!/<script defer>/.test(doc) && !/\sdefer/.test(doc.slice(doc.indexOf("x();") - 40)), "plus d'attente sur un script en ligne");
  assert.ok(doc.includes("<\\/STYLE>") && !/<\/STYLE>/.test(doc), "la casse gardée, la balise ne se ferme pas");
  assert.ok(doc.includes("a <\\!-- b"), "un <!-- dans un script en ligne est échappé");
});

test("projet : l'arborescence triée segment par segment, les dossiers d'abord", () => {
  // « src-old » trié après « src » et TOUT son contenu (« - » vient avant
  // « / » : comparer les chemins entiers les mélangerait).
  const rows = fileTree([{ path: "src-old/x.js" }, { path: "src/a.js" }, { path: "README.md" }, { path: "src/b/c.js" }]);
  assert.deepEqual(rows.map((r) => [r.key, r.depth, r.dir]), [
    ["src", 0, true], ["src/b", 1, true], ["src/b/c.js", 2, false], ["src/a.js", 1, false],
    ["src-old", 0, true], ["src-old/x.js", 1, false], ["README.md", 0, false]]);
});

test("projet : la source d'un fichier colorée selon son extension", () => {
  const hl = { hljs, css: "" };
  assert.match(fileDoc(PROJECT.files[0], hl), /<span class="hljs-keyword">import<\/span>/);
  assert.match(fileDoc({ path: "a.txt", content: "<b>" }, null), /&lt;b&gt;/);
});

test("projet : téléchargé en .zip, que Python relit à l'identique, fichier vide compris", () => {
  const project = { ...PROJECT, files: [...PROJECT.files, { path: "docs/vide.txt", content: "" },
                                        { path: "docs/notes.md", content: "é à ü" }] };
  const file = artifactFile({ id: "srv" }, project);
  assert.equal(file.name, "srv.zip");
  const dir = mkdtempSync(join(tmpdir(), "zip-"));
  try {
    writeFileSync(join(dir, "a.zip"), file.content);
    const r = spawnSync("python3", ["-I", "-c", `import zipfile, json, sys
z = zipfile.ZipFile(sys.argv[1]); assert z.testzip() is None
print(json.dumps({n: z.read(n).decode() for n in z.namelist()}))`, join(dir, "a.zip")], { encoding: "utf8" });
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(JSON.parse(r.stdout), Object.fromEntries(project.files.map((f) => [f.path, f.content])));
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("capture : seule une image JPEG ou PNG en base64, bornée, est gardée", async () => {
  const { shotImage, MAX_SHOT_CHARS } = await import("../../llm_proxy/ui/web/static/chat/artifacts.js");
  assert.deepEqual(shotImage({ image: "data:image/jpeg;base64,/9j/AA==", size: [10, 20] }),
    { image: "data:image/jpeg;base64,/9j/AA==", size: [10, 20] });
  assert.equal(shotImage({ image: "data:text/html;base64,PHNjcmlwdD4=" }), null);
  assert.equal(shotImage({ image: "data:image/svg+xml;base64,PHN2Zz4=" }), null);
  assert.equal(shotImage({ image: 'data:image/png;base64,AA" onerror="x' }), null);
  assert.equal(shotImage({ image: "https://evil/x.png" }), null);
  assert.equal(shotImage({ image: "data:image/png;base64," + "A".repeat(MAX_SHOT_CHARS) }), null);
  assert.deepEqual(shotImage({ image: "data:image/png;base64,AAAA", size: ["<b>", -1] }).size, [0, 0]);
});
