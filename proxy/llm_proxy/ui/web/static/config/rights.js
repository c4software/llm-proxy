// @ts-check
/*
  Les DROITS d'une clé sur les outils hébergés, en mots : le nom lisible
  des outils natifs, la liste complète (info-bulle) et sa forme courte
  (colonne « Outils » de Configuration → Clés). ToolRights.vue les choisit.
*/
// Le nom lisible des outils natifs ; un outil inconnu ici garde son nom.
export const TOOL_LABELS = {
  web_search: "Recherche web", web_fetch: "Lecture de pages web",
  web_grep: "Recherche dans une page", ocr: "OCR (images, PDF)",
  transcribe: "Transcription audio", code_execution: "Exécution de code",
  image_generation: "Génération d'images", memory: "Mémoire persistante",
  webhook: "Webhooks sortants", repository: "Dépôts de code",
};

// La liste complète des droits d'une clé (l'info-bulle de la colonne).
export function rightsSummary(rights) {
  if (rights == null) return "tous";
  if (!rights.length) return "aucun";
  return rights.map((r) => TOOL_LABELS[r] || (r === "mcp" ? "tous les MCP" : r.replace(/^mcp:/, "MCP "))).join(", ");
}

// Ce qu'en dit la colonne « Outils » du tableau : court, la liste en
// info-bulle (rightsSummary).
export function rightsShort(rights) {
  if (rights == null || !rights.length || rights.length === 1) return rightsSummary(rights);
  return `${rights.length} outils`;
}

