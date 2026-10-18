/*
  Ce que les parties de la configuration partagent : l'écriture TOML, la
  clé du poste, le catalogue des modèles et les comptes (Clés y prend la
  liste pour rattacher une clé du TOML, le sélecteur de parties le nombre
  de comptes en attente).
*/
import { call } from "../lib/http.js";
import { catalog, catalogState } from "../lib/catalog.js";

const { ref, computed } = Vue;

// ── TOML et variables ───────────────────────────────────────────────────
// Une chaîne TOML « basique » : les échappements de JSON en sont un
// sous-ensemble valide.
export const q = (s) => JSON.stringify(String(s ?? ""));
export const list = (xs) => "[" + xs.map(q).join(", ") + "]";
export const csv = (s) => String(s || "").split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
export const envName = (s) => String(s || "").toUpperCase().replace(/[^A-Z0-9_]/g, "_")
  .replace(/^(\d)/, "_$1");
export function randomKey() {
  const b = new Uint8Array(24);
  crypto.getRandomValues(b);
  return "sk-" + [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
}

// ── la clé du poste ─────────────────────────────────────────────────────
// Où vit la clé du proxy sur le poste, et comment l'y poser sans qu'elle
// passe par l'historique du shell.
export const KEYFILE = "~/.config/llm-proxy.key";
export const KEY_ENV = "LLM_PROXY_API_KEY";
export const KEY_SETUP = `(umask 077; read -rsp "Clé du proxy : " k && echo && printf '%s' "$k" > ${KEYFILE})\n`;

// ── catalogue des modèles ───────────────────────────────────────────────
// Celui de la page (lib/catalog.js, réactif) : les modèles de conversation, avec
// leur fenêtre de contexte, et le catalogue complet (images, voix, OCR —
// pour les outils qui en visent un).
const chatModels = computed(() => catalogState.data.filter((m) => m.chat));
const models = computed(() => chatModels.value.map((m) => m.id).sort());
const contexts = computed(() => Object.fromEntries(
  chatModels.value.filter((m) => m.context).map((m) => [m.id, m.context])));
const allModels = computed(() => catalogState.data.map((m) => m.id).sort());
export function useCatalog() {
  // Hors ligne : saisie libre.
  catalog().catch(() => {});
  // Les modèles d'un menu : le catalogue, et la valeur en place si elle
  // n'y est pas (un modèle de configuration hors catalogue ne se perd
  // pas). `all` : tout le catalogue — l'OCR, la transcription ou l'image
  // visent des modèles qui ne sont pas de chat.
  function modelChoices(current, all = false) {
    const pool = all && allModels.value.length ? allModels.value : models.value;
    return current && !pool.includes(current) ? [current, ...pool] : pool;
  }
  return { models, contexts, allModels, modelChoices };
}

// ── comptes (admin) ─────────────────────────────────────────────────────
const users = ref([]);
const userError = ref("");
const pendingCount = computed(() => users.value.filter((u) => u.role === "pending").length);
async function loadUsers() {
  try {
    users.value = (await call("GET", "/ui/users")).data || [];
    userError.value = "";
  } catch (e) { userError.value = e.message; }
}
export function useUsers() {
  return { users, userError, pendingCount, loadUsers };
}

// Le compte connecté (comptes OIDC), et s'il est admin.
export function useAccount(props) {
  const account = computed(() => props.session.account || null);
  const isAdmin = computed(() => !!account.value && account.value.role === "admin");
  return { account, isAdmin };
}
