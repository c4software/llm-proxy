// Configuration → Proxy (config/proxy/form.js) : les en-têtes propres d'un
// backend, du fichier en place au TOML produit. Leurs valeurs ne s'écrivent
// jamais en clair : un renvoi au .env reste tel quel, une valeur en clair
// (masquée) devient un ${VAR} ; un nom que le proxy refuserait au démarrage
// est écarté avec un avertissement.
import "./setup.mjs";
import { test } from "vitest";
import assert from "node:assert/strict";

const { newForm, prefillForm, buildProxy } =
  await import("../../llm_proxy/ui/web/static/config/proxy/form.js");

function built(headers, edit = (b) => b) {
  const px = newForm();
  prefillForm(px, { backends: { zen: { url: "https://zen.exemple.fr", headers } } });
  edit(px.backends[0]);
  const out = buildProxy(px);
  const env = out.files.find((f) => f.name === ".env").content.split("\n");
  const toml = out.partial.slice(out.partial.indexOf("[backends.zen.headers]"));
  return { toml: toml.slice(0, toml.indexOf("\n\n") + 1 || undefined), env, warn: out.warn };
}

test("une valeur qui renvoie au .env reste telle quelle, une valeur en clair devient un ${VAR}", () => {
  const { toml, env, warn } = built({
    Authorization: "Bearer ${ZEN_TOKEN}", "X-Equipe": "${EQUIPE}", "User-Agent": "••• masqué •••" });
  assert.match(toml, /"Authorization" = "Bearer \$\{ZEN_TOKEN\}"/);
  assert.match(toml, /"X-Equipe" = "\$\{EQUIPE\}"/);
  assert.match(toml, /"User-Agent" = "\$\{ZEN_HEADER_USER_AGENT\}"/);
  assert.ok(!toml.includes("masqué"), toml);
  assert.ok(env.includes("ZEN_HEADER_USER_AGENT="), env.join("\n"));
  assert.ok(warn.some((w) => w.includes("en clair")), warn.join("\n"));
});

test("un nom invalide ou réglé par le proxy est écarté, un renommage de casse garde sa valeur", () => {
  const { toml, warn } = built({ "X-Equipe": "${EQUIPE}" }, (b) => {
    b.headerNames = "x-equipe, X-A: v, Host, X-Équipe, x-EQUIPE";
  });
  assert.match(toml, /"x-equipe" = "\$\{EQUIPE\}"/);
  for (const bad of ["X-A: v", "Host", "X-Équipe"]) {
    assert.ok(!toml.includes(bad), `${bad} écrit : ${toml}`);
    assert.ok(warn.some((w) => w.includes(`« ${bad} »`)), `${bad} sans avertissement`);
  }
  assert.ok(warn.some((w) => w.includes("en double")), warn.join("\n"));
});

test("deux en-têtes au même nom de variable ne doublent pas la ligne du .env", () => {
  const { env } = built({}, (b) => { b.headerNames = "X-A, X_A"; });
  assert.equal(env.filter((l) => l === "ZEN_HEADER_X_A=").length, 1);
});
