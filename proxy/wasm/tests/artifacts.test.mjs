// Le document d'un artifact (web/static/chat/artifacts.js) : sa politique
// de contenu passe AVANT tout ce que l'artifact contient. Vérifié dans
// Chrome à l'écriture : un artifact sans <head> mais avec un <header>
// recevait sa <meta> CSP dans le corps, où elle est ignorée.
import { test } from "node:test";
import assert from "node:assert/strict";
import { artifactDoc } from "../../llm_proxy/ui/web/static/chat/artifacts.js";

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
