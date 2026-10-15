#!/bin/sh
# Les scénarios de validation pi → proxy, par l'API OpenAI (provider
# llm-proxy), rejoués pour chaque modèle de MODELS. PASS/FAIL par
# scénario, sortie en erreur si l'un échoue. Tout se passe dans /work du
# conteneur. (Le provider llm-proxy-anthropic de models.json n'est pas
# joué ici — il reste disponible pour un essai à la main.)
#
# Scénario 6 : la recherche web HÉBERGÉE par le proxy (README principal,
# « Serveur MCP »). L'outil vient du serveur MCP du proxy (POST /mcp),
# déclaré dans le dossier d'agent $PI_CODING_AGENT_DIR-mcp qu'écrit
# entrypoint.sh : pi l'appelle comme un de ses outils, l'appel et le
# résultat sont dans sa trace. (Jusqu'au 07/10/2026 : l'extension
# llm-proxy-web.ts ; jusqu'au 15/10/2026 : les outils d'office du proxy,
# [chat].always.) Il est SAUTÉ (SKIP, ni PASS ni FAIL) quand le proxy visé
# n'héberge pas web_search ou n'a pas son serveur MCP — lu dans /healthz.
# Les scénarios 1 à 5 tournent sans serveur MCP.
set -u
cd /work
fails=0
skips=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails + 1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; skips=$((skips + 1)); }

# Ce que le proxy héberge, lu dans /healthz — exempté de clé : `hosted`, ses
# outils actifs (« web_search web_fetch ») ; `mcp`, « true » si son serveur
# MCP est actif. Vides si rien n'est activé, si le proxy est plus ancien que
# ces réglages ou s'il ne répond pas.
health=$(node -e '
  fetch(process.argv[1] + "/healthz", {signal: AbortSignal.timeout(10000)})
    .then(r => r.json())
    .then(j => process.stdout.write(((j.tools || {}).enabled || []).join(" ") + "|"
      + String(!!(j.mcp_server || {}).enabled)))
    .catch(() => {});' "$PROXY_URL")
hosted=${health%%|*}
mcp=${health#*|}
hosts() { case " $hosted " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# Le compteur du proxy : exécutions de web_search arrivées par son serveur
# MCP (route /mcp), lues sur son Usage API des outils (README principal,
# « Usage des outils hébergés »). Imprime le nombre, ou rien si la route ne
# répond pas (proxy antérieur au 05/10/2026, clé refusée). $1 : le nombre à
# atteindre — le proxy écrit ses lignes hors de la requête, on relit donc
# jusqu'à 5 s tant qu'il n'y est pas (0 : une seule lecture).
# La fenêtre est la MÊME à chaque lecture (un jour avant le début du banc,
# deux jours après) : deux lectures ne diffèrent que par ce qui s'est
# exécuté entre elles, et l'horloge du proxy n'a pas à être celle d'ici.
usage_start=$(($(date +%s) - 86400))
usage_end=$((usage_start + 259200))
searches() { node -e '
  const [url, key, start, end, want] = process.argv.slice(1);
  const query = new URLSearchParams([["start_time", start], ["end_time", end], ["bucket_width", "all"],
    ["group_by[]", "endpoint"], ["group_by[]", "tool"]]);
  const read = () => fetch(url + "/v1/organization/usage/tools?" + query,
      {headers: {Authorization: "Bearer " + key}, signal: AbortSignal.timeout(10000)})
    .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
    .then(j => {
      if (j.object !== "page") throw new Error("forme inattendue");
      return (j.data || []).flatMap(b => b.results || [])
        .filter(r => r.endpoint === "/mcp" && r.tool === "web_search")
        .reduce((n, r) => n + (r.num_requests || 0), 0);
    });
  (async () => {
    let n = await read();
    for (let i = 0; n < Number(want) && i < 10; i++) {
      await new Promise(done => setTimeout(done, 500));
      n = await read();
    }
    process.stdout.write(String(n));
  })().catch(() => {});' "$PROXY_URL" "${PROXY_API_KEY:-unused}" "$usage_start" "$usage_end" "$1"; }

# Verdict du scénario web, sur la sortie de `pi --mode json` (JSONL, un
# événement par ligne) lue sur l'entrée standard. $1 : le texte que la
# réponse doit contenir (casse ignorée) ; $2 et $3 : le compteur du proxy
# avant et après (vides s'il n'est pas lisible). Imprime « OK … » ou « KO … ».
# La réponse ne prouve rien (un modèle l'écrit de mémoire). Les preuves :
#   1. dans la trace, un `tool_execution_end` de `mcp__llmproxy__web_search`
#      (le nom que pi donne à `web_search` du serveur `llmproxy`), sans
#      erreur, dont le résultat porte au moins une URL ;
#   2. aucun outil exécuté qui ne soit un outil MCP du proxy — --tools n'en
#      laisse pas d'autre ;
#   3. côté proxy, son compteur d'exécutions de web_search par /mcp a avancé
#      pendant l'appel (« au moins 1 » : un autre client peut l'avancer
#      aussi, il confirme la trace sans la remplacer). Illisible, il n'est
#      pas exigé (le libellé le dit).
# La réponse est le texte du dernier message `assistant`.
verdict() { node -e '
  const [needle, before, after] = process.argv.slice(1);
  const events = require("fs").readFileSync(0, "utf8").split("\n")
    .flatMap(l => { try { return [JSON.parse(l)]; } catch { return []; } });
  const TOOL = "mcp__llmproxy__web_search";
  const starts = events.filter(e => e.type === "tool_execution_start");
  const foreign = [...new Set(starts.map(e => String(e.toolName))
    .filter(n => !n.startsWith("mcp__llmproxy__")))];
  const calls = events.filter(e => e.type === "tool_execution_end" && e.toolName === TOOL);
  const text = r => JSON.stringify((r || {}).content || "");
  const good = calls.filter(e => e.isError !== true && text(e.result).includes("http"));
  const query = (starts.filter(e => e.toolName === TOOL)
    .map(e => (e.args || {}).query || "").pop() || "?").slice(0, 60);
  const last = events.filter(e => e.type === "message_end" && (e.message || {}).role === "assistant")
    .map(e => e.message).pop() || {};
  const answer = (Array.isArray(last.content) ? last.content : [])
    .filter(b => b.type === "text").map(b => b.text || "").join(" ").replace(/\s+/g, " ").trim();
  const counted = before !== "" && after !== "" ? Number(after) - Number(before) : null;
  const seen = good.length + " recherche(s) aboutie(s) sur " + calls.length + " (" + query + "), "
    + (counted === null ? "compteur du proxy illisible" : "compteur du proxy /mcp : +" + counted) + " — "
    + (answer.slice(0, 200) || "pas de réponse" + (last.errorMessage ? " : " + String(last.errorMessage).slice(0, 200) : ""));
  const ko = !events.length ? "aucun événement JSON rendu par pi"
    : foreign.length ? "un outil autre que ceux du proxy a été exécuté (" + foreign.join(", ") + ") : " + seen
    : !calls.length ? "aucun appel à " + TOOL + " dans la trace (serveur MCP non joint, ou outil non appelé) : " + seen
    : !good.length ? TOOL + " appelé mais sans résultat (" + text(calls[calls.length - 1].result).slice(0, 160) + ") : " + seen
    : counted !== null && counted < 1 ? "aucune exécution de web_search par /mcp comptée par le proxy pendant cet appel : " + seen
    : !answer.toLowerCase().includes(needle.toLowerCase()) ? "réponse sans « " + needle + " » : " + seen
    : "";
  process.stdout.write(ko ? "KO " + ko : "OK " + seen);' "$@"; }

version=$(pi --version 2>/dev/null | head -1)
summary=""

for MODEL in $MODELS; do
  echo
  echo "════ pi $version → $PROXY_URL | modèle $MODEL ════"
  rm -rf /work/* 2>/dev/null
  fails_before=$fails
  skips_before=$skips
  # </dev/null : sans terminal sur l'entrée standard (docker compose run -T,
  # CI), pi attend la fin d'une entrée qu'il préfixerait au prompt.
  run() { pi -p --no-session --provider llm-proxy --model "$MODEL" "$@" </dev/null 2>&1 | tail -n 20; }
  # Scénario web : --mode json pour la trace (voir verdict), dans un fichier
  # — le compteur du proxy se relit APRÈS l'appel, avant le verdict —, dans
  # le dossier d'agent qui déclare le serveur MCP du proxy. --tools (motif,
  # lu dans docs/mcp.md de pi 1.2) ne laisse au modèle QUE les outils MCP du
  # proxy : ni bash (un curl partirait du conteneur, pas du proxy), ni read.
  # stderr va dans un autre fichier, hors du JSONL : `diag` en rend la fin
  # sur un échec (pi y dit un serveur MCP injoignable ou une clé refusée).
  web() {
    PI_CODING_AGENT_DIR="$PI_CODING_AGENT_DIR-mcp" \
      pi --mode json --no-session --provider llm-proxy --model "$MODEL" \
      --tools 'mcp__llmproxy__*' "$@" </dev/null >/tmp/pi-web.jsonl 2>/tmp/pi-web.err
  }
  diag() { [ -s /tmp/pi-web.err ] && printf ' — stderr : %s' "$(tail -n 3 /tmp/pi-web.err | tr '\n' ' ')"; return 0; }

  echo "1. Réponse simple"
  out=$(run "Réponds en un seul mot : quelle est la capitale de la France ?")
  case "$out" in *Paris*) pass "$out" ;; *) fail "$out" ;; esac

  echo "2. Outils : write + bash + read"
  rm -f hello.txt
  out=$(run "Crée un fichier hello.txt contenant exactement le mot bonjour, affiche-le avec cat, puis relis-le avec l'outil read et confirme son contenu en une phrase.")
  if [ "$(cat hello.txt 2>/dev/null | tr -d '[:space:]')" = "bonjour" ]; then pass "hello.txt = bonjour — $(echo "$out" | tail -n 1)"; else fail "hello.txt absent ou différent — $out"; fi

  echo "3. Outils : edit"
  mkdir -p src && printf 'def add(a, b):\n    return a - b\n' > src/calc.py
  out=$(run "Lis src/calc.py, corrige le bug évident avec l'outil edit, puis affiche le fichier corrigé avec cat.")
  if grep -q 'return a + b' src/calc.py; then pass "src/calc.py corrigé — $(echo "$out" | tail -n 1)"; else fail "src/calc.py non corrigé — $out"; fi

  echo "4. Création de code : module Node + tests"
  rm -rf stats && mkdir stats && cd stats
  out=$(run "Crée un module CommonJS stats.js qui exporte mean(tableau) et median(tableau) (médiane correcte pour un nombre pair d'éléments), puis test.js qui les vérifie avec node:assert sur quatre cas, exécute node test.js jusqu'à ce qu'il passe.")
  if node test.js >/dev/null 2>&1 && node -e 'const s=require("./stats");process.exit(s.median([1,2,3,4])===2.5?0:1)'; then pass "stats.js + test.js — $(echo "$out" | tail -n 1)"; else fail "$out"; fi
  cd /work

  echo "5. Corriger un bug sans toucher au test"
  rm -rf slug && mkdir slug && cd slug
  cat > slugify.js <<'JS'
module.exports = function slugify(title) {
  return title.toLowerCase().replace(/ /g, "-");
};
JS
  cat > slugify.test.js <<'JS'
const assert = require("node:assert");
const slugify = require("./slugify");
assert.strictEqual(slugify("Hello World"), "hello-world");
assert.strictEqual(slugify("  Déjà   vu ! "), "deja-vu");
assert.strictEqual(slugify("--a--b--"), "a-b");
console.log("ok");
JS
  sum=$(cksum slugify.test.js)
  out=$(run "Lance node slugify.test.js : il échoue. Corrige slugify.js (accents retirés, tout caractère non alphanumérique devient un tiret, tirets fusionnés et retirés aux extrémités) sans modifier slugify.test.js, et relance jusqu'à ce que ça passe.")
  if [ "$(cksum slugify.test.js)" != "$sum" ]; then fail "test modifié — $out"
  elif node slugify.test.js >/dev/null 2>&1; then pass "slugify.js corrigé — $(echo "$out" | tail -n 1)"; else fail "$out"; fi
  cd /work

  echo "6. Recherche web hébergée (serveur MCP du proxy → POST /mcp, tools/call web_search)"
  # La réponse attendue est une URL que le nom du dépôt détermine : elle ne
  # dépend ni de l'actualité ni de la formulation (on ne cherche que
  # « github.com/ggml-org/llama.cpp », que toute bonne réponse contient,
  # /releases, /releases/latest ou lien Markdown compris).
  if ! hosts web_search; then
    skip "web_search n'est pas hébergé par ce proxy (/healthz : tools.enabled = [${hosted}])"
  elif [ "$mcp" != "true" ]; then
    skip "le serveur MCP du proxy est inactif (/healthz : mcp_server.enabled ; [mcp_server] enabled = true)"
  else
    before=$(searches 0)
    web "Trouve par une recherche web la page des releases du dépôt GitHub ggml-org/llama.cpp et réponds uniquement par son URL."
    after=""
    if [ -n "$before" ]; then after=$(searches $((before + 1))); fi
    out=$(verdict "github.com/ggml-org/llama.cpp" "$before" "$after" </tmp/pi-web.jsonl)
    case "$out" in "OK "*) pass "${out#OK }" ;; *) fail "${out#KO }$(diag)" ;; esac
  fi

  failed=$((fails - fails_before))
  skipped=$((skips - skips_before))
  played=$((6 - skipped))
  line="$((played - failed))/$played"
  [ "$skipped" -gt 0 ] && line="$line, $skipped sauté(s)"
  summary="$summary
  $MODEL : $line"
done

echo
echo "Résumé :$summary"
[ "$skips" -gt 0 ] && echo "$skips scénario(s) sauté(s) : outils web non hébergés, ou non présentés d'office, par le proxy."
[ "$fails" -eq 0 ] && echo "Tout passe." || { echo "$fails scénario(s) en échec."; exit 1; }
