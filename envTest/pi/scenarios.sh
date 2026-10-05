#!/bin/sh
# Les scénarios de validation pi → proxy, par l'API OpenAI (provider
# llm-proxy), rejoués pour chaque modèle de MODELS. PASS/FAIL par
# scénario, sortie en erreur si l'un échoue. Tout se passe dans /work du
# conteneur. (Le provider llm-proxy-anthropic de models.json n'est pas
# joué ici — il reste disponible pour un essai à la main.)
#
# Scénario 6 : la recherche web HÉBERGÉE par le proxy (README principal,
# « Outils hébergés »). pi parle chat/completions : l'outil lui vient de
# l'extension /pi/extensions/llm-proxy-web.ts (posée par le Dockerfile), qui
# lit GET /v1/tools et exécute par POST /v1/tools/<nom>. Il est SAUTÉ (SKIP,
# ni PASS ni FAIL) quand le proxy visé n'héberge pas l'outil — lu dans
# /healthz.
set -u
cd /work
fails=0
skips=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails + 1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; skips=$((skips + 1)); }

# Les outils que le proxy héberge, séparés par des espaces (« web_search
# web_fetch »), lus dans /healthz — exempté de clé. Vide si aucun n'est
# activé, si le proxy est plus ancien que ces outils ou s'il ne répond pas.
hosted=$(node -e '
  fetch(process.argv[1] + "/healthz", {signal: AbortSignal.timeout(10000)})
    .then(r => r.json())
    .then(j => process.stdout.write(((j.tools || {}).enabled || []).join(" ")))
    .catch(() => {});' "$PROXY_URL")
hosts() { case " $hosted " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# Verdict du scénario web, sur la sortie de `pi --mode json` (JSONL, un
# événement par ligne) lue sur l'entrée standard. $1 : le texte que la
# réponse doit contenir (casse ignorée). Imprime « OK … » ou « KO … ».
# La PREUVE que le proxy a cherché n'est pas la réponse, qu'un modèle peut
# écrire de mémoire : c'est un événement `tool_execution_end` de l'outil
# `proxy_web_search`, sans erreur, dont le résultat porte au moins une URL —
# le texte rendu par POST /v1/tools/web_search. La réponse est le texte du
# dernier message `assistant` ; le message `user` (le prompt) est écarté.
verdict() { node -e '
  const [needle] = process.argv.slice(1);
  const events = require("fs").readFileSync(0, "utf8").split("\n")
    .flatMap(l => { try { return [JSON.parse(l)]; } catch { return []; } });
  const calls = events.filter(e => e.type === "tool_execution_end" && e.toolName === "proxy_web_search");
  const text = r => JSON.stringify((r || {}).content || "");
  const good = calls.filter(e => e.isError !== true && text(e.result).includes("http"));
  const query = (events.filter(e => e.type === "tool_execution_start" && e.toolName === "proxy_web_search")
    .map(e => (e.args || {}).query || "").pop() || "?").slice(0, 60);
  const last = events.filter(e => e.type === "message_end" && (e.message || {}).role === "assistant")
    .map(e => e.message).pop() || {};
  const answer = (Array.isArray(last.content) ? last.content : [])
    .filter(b => b.type === "text").map(b => b.text || "").join(" ").replace(/\s+/g, " ").trim();
  const seen = good.length + " recherche(s) aboutie(s) sur " + calls.length + " (" + query + ") — "
    + (answer.slice(0, 200) || "pas de réponse" + (last.errorMessage ? " : " + String(last.errorMessage).slice(0, 200) : ""));
  const ko = !events.length ? "aucun événement JSON en sortie de pi"
    : !calls.length ? "aucun appel à proxy_web_search dans la trace (extension non chargée, ou outil non appelé) : " + seen
    : !good.length ? "proxy_web_search appelé mais sans résultat (" + text(calls[calls.length - 1].result).slice(0, 160) + ") : " + seen
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
  # Scénario web : --mode json pour la trace (voir verdict) ; -e charge
  # l'extension pour ce seul appel ; --tools ne laisse au modèle QUE la
  # recherche du proxy — ni bash (un curl partirait du conteneur, pas du
  # proxy), ni les autres outils : c'est aussi ce qui a été validé à la main.
  # stderr va dans un fichier, hors du JSONL : `diag` en rend la fin sur un
  # échec (l'extension y écrit « découverte impossible » si /v1/tools ne
  # répond pas ou refuse la clé).
  web() {
    pi --mode json --no-session --provider llm-proxy --model "$MODEL" \
      -e /pi/extensions/llm-proxy-web.ts --tools proxy_web_search "$@" \
      </dev/null 2>/tmp/pi-web.err
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

  echo "6. Recherche web hébergée (extension llm-proxy-web.ts → GET /v1/tools, POST /v1/tools/web_search)"
  # La réponse attendue est une URL que le nom du dépôt détermine : elle ne
  # dépend ni de l'actualité ni de la formulation (on ne cherche que
  # « github.com/ggml-org/llama.cpp », que toute bonne réponse contient,
  # /releases, /releases/latest ou lien Markdown compris).
  if hosts web_search; then
    out=$(web "Trouve par une recherche web la page des releases du dépôt GitHub ggml-org/llama.cpp et réponds uniquement par son URL." | verdict "github.com/ggml-org/llama.cpp")
    case "$out" in "OK "*) pass "${out#OK }" ;; *) fail "${out#KO }$(diag)" ;; esac
  else
    skip "web_search n'est pas hébergé par ce proxy (/healthz : tools.enabled = [${hosted}])"
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
[ "$skips" -gt 0 ] && echo "$skips scénario(s) sauté(s) : outils web non hébergés par le proxy."
[ "$fails" -eq 0 ] && echo "Tout passe." || { echo "$fails scénario(s) en échec."; exit 1; }
