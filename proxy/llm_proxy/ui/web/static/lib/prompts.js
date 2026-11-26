// @ts-check
/*
  Les PROMPTS de la page : les textes qu'elle ajoute aux messages envoyés
  au modèle (le résumé d'une conversation compactée, l'état du moment des
  outils, le protocole de Gemini Nano). Ils viennent du catalogue du proxy
  (prompts/, modifiables par l'admin) avec /ui/session (`chat.prompts`),
  variables non remplies : la page les remplit ici.

    install(gabarits)       {clé: gabarit}, à l'arrivée de la session
    prompt(clé, variables)  le texte rempli

  Même syntaxe que le proxy : `{nom}` est une variable, `{{` et `}}`
  écrivent une accolade. Une clé inconnue ou une variable manquante est
  une faute du code : une erreur, pas un texte à trou.
*/

/** @type {Record<string, string>} */
let templates = {};

/** @param {Record<string, string> | null | undefined} given */
export function install(given) {
  templates = { ...(given || {}) };
}

/** @param {string} key @param {Record<string, string | number>} [vars] @returns {string} */
export function prompt(key, vars = {}) {
  const template = templates[key];
  if (typeof template !== "string") throw new Error(`prompt inconnu : ${key}`);
  return template.replace(/\{\{|\}\}|\{([a-z_][a-z0-9_]*)\}/g, (all, name) => {
    if (all === "{{") return "{";
    if (all === "}}") return "}";
    if (!(name in vars)) throw new Error(`prompt ${key} : variable {${name}} non fournie`);
    return String(vars[name]);
  });
}
