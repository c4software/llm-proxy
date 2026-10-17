/*
  La section « Avancé » de Configuration → Proxy, sans Vue : chaque
  réglage que les autres sections ne couvrent pas (formCovers), généré
  depuis la configuration en vigueur — une ligne par réglage, éditable
  selon son type (case, nombre, texte, liste, JSON). Ce qui change part
  au proxy en `overrides` ({path, value}) avec le formulaire.
*/
import { csv } from "../shared.js";
import { TOOL_LIST } from "./form.js";

export const FORM_FLAT = new Set(["proxy.log_level", "proxy.api_keys", "proxy.exempt_paths",
  "fair_share.tokens_per_hour", "anthropic.enabled", "anthropic.model_map.default",
  "responses.enabled", "chat.hosted_tools", "chat.always", "chat.always_except",
  "tools.max_calls", "mcp_server.enabled", "ui.chat", "ui.chat_model",
  "files.public_url", "stats.retention_days"]);
export const BACKEND_FORM_KEYS = ["url", "api_key", "quotas", "images", "force_tool_choice",
                           "max_tokens", "fair_share"];
export const OIDC_FORM_KEYS = ["issuer", "client_id", "client_secret", "redirect_uri",
                        "new_accounts", "session_days", "max_keys_per_account", "label"];
export function formCovers(path) {
  const [top, a, b] = path;
  if (top === "backends") return path.length === 3 && BACKEND_FORM_KEYS.includes(b);
  if (top === "oidc") return path.length === 2 && OIDC_FORM_KEYS.includes(a);
  if (top === "tools" && path.length === 3 && TOOL_LIST.some((t) => t.id === a)) {
    const tool = TOOL_LIST.find((t) => t.id === a);
    return b === "enabled" || (b === "model" && !!tool.model)
      || (a === "web_search" && b === "searxng_url")
      || (a === "code_execution" && (b === "url" || b === "token"));
  }
  return FORM_FLAT.has(path.join("."));
}
export const kindOf = (v) => typeof v === "boolean" ? "bool" : typeof v === "number" ? "number"
  : typeof v === "string" ? "string"
  : Array.isArray(v) && v.every((x) => typeof x === "string") ? "list" : "json";
export const asText = (v, kind) => kind === "list" ? v.join(", ")
  : kind === "json" ? JSON.stringify(v) : v;

export function advancedRows(values, defaultPaths) {
  const rows = [];
  const defaults = new Set(defaultPaths);
  const walk = (node, path) => {
    for (const [k, v] of Object.entries(node)) {
      const p = [...path, k];
      if (v && typeof v === "object" && !Array.isArray(v)) { walk(v, p); continue; }
      if (formCovers(p)) continue;
      const kind = kindOf(v);
      rows.push({ path: p, group: path.join(".") || "(racine)", key: k, kind, orig: v,
                  value: asText(v, kind), isDefault: defaults.has(p.join(".")), error: "" });
    }
  };
  walk(values || {}, []);
  return rows;
}

// La valeur saisie, typée comme l'originale : {value} ou {error}.
export function parseAdv(row) {
  if (row.kind === "bool") return { value: row.value };
  if (row.kind === "number") {
    const n = Number(row.value);
    return row.value === "" || Number.isNaN(n) ? { error: "nombre attendu" } : { value: n };
  }
  if (row.kind === "list") return { value: csv(row.value) };
  if (row.kind === "json") {
    try { return { value: JSON.parse(row.value) }; } catch { return { error: "JSON invalide" }; }
  }
  return { value: row.value };
}
