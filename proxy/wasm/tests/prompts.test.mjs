// Les prompts de la page (web/static/lib/prompts.js) : les gabarits du
// catalogue du proxy, remplis par la page avec la même syntaxe que lui.
import { test } from "node:test";
import assert from "node:assert/strict";
import { install, prompt } from "../../llm_proxy/ui/web/static/lib/prompts.js";
import { PAGE_PROMPTS } from "./setup.mjs";

test("prompt : variables remplies, accolades doublées, faute du code sur une clé ou une variable absente", () => {
  install({ a: "Open {name} ({count} files). {{literal}} }}", b: "{missing}" });
  assert.equal(prompt("a", { name: "x", count: 3 }), "Open x (3 files). {literal} }");
  assert.throws(() => prompt("b"), /\{missing\}/);
  assert.throws(() => prompt("inconnue"), /inconnu/);
  install(PAGE_PROMPTS);
});

