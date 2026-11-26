// Les tests de la page : le minimum du navigateur (../browser.mjs), et
// les prompts du catalogue installés.
export { listeners } from "../browser.mjs";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { parse as parseToml } from "smol-toml";
import { install as installPrompts } from "../../llm_proxy/ui/web/static/lib/prompts.js";

// Les prompts de la page (lib/prompts.js) : ceux du VRAI catalogue du proxy,
// comme /ui/session les donne (prompts.for_page : le domaine chat, l'état
// des familles).

const catalog = (name) => parseToml(readFileSync(join(import.meta.dirname, `../../llm_proxy/prompts/${name}.toml`), "utf8"));
/** @type {Record<string, string>} */
export const PAGE_PROMPTS = {};
for (const [subject, entries] of Object.entries(catalog("chat"))) {
  for (const [name, text] of Object.entries(entries)) PAGE_PROMPTS[`${subject}.${name}`] = text;
}
for (const [subject, entries] of Object.entries(catalog("page"))) {
  for (const [name, text] of Object.entries(entries)) {
    if (subject.startsWith("family.") && name.startsWith("state")) PAGE_PROMPTS[`${subject}.${name}`] = text;
  }
}
installPrompts(PAGE_PROMPTS);
