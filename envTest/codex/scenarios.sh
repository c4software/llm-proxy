#!/bin/sh
# Les scénarios de validation Codex CLI → proxy, par l'API Responses
# (POST /v1/responses, traduite vers /v1/chat/completions), rejoués pour
# chaque modèle de MODELS. PASS/FAIL par scénario, sortie en erreur si
# l'un échoue. Tout se passe dans /work du conteneur.
#
# --dangerously-bypass-approvals-and-sandbox : le bac à sable de Codex
# (bwrap) ne démarre pas dans un conteneur sans privilèges, et le conteneur
# jetable EST le bac à sable — même choix que Claude Code ici
# (--dangerously-skip-permissions). À ne pas reprendre sur un poste.
#
# Scénarios 6 et 7 : les outils web HÉBERGÉS par le proxy (README principal,
# « Outils hébergés »). Codex déclare lui-même `web_search` ; le proxy
# exécute recherche et lecture de page et rend des éléments
# `web_search_call`. Ils sont SAUTÉS (SKIP, ni PASS ni FAIL) quand le proxy
# visé ne les héberge pas — lu dans /healthz.
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

# Verdict d'un scénario web, sur la sortie de `codex exec --json` (JSONL,
# un événement par ligne) lue sur l'entrée standard. $1 : le texte que la
# réponse doit contenir (casse ignorée) ; $2 : 1 si une lecture de page est
# exigée en plus de la recherche. Imprime « OK … » ou « KO … ».
# La PREUVE que le proxy a travaillé n'est pas la réponse, qu'un modèle peut
# écrire de mémoire : ce sont les éléments `web_search` de la trace — action
# `open_page` (une page lue), toute autre action comptant pour une
# recherche —, que Codex ne construit que depuis les `web_search_call`
# rendus par le proxy. La réponse
# est le dernier `agent_message` ; le prompt n'est pas dans ce flux, il ne
# peut donc pas fournir le texte attendu à la place du modèle.
verdict() { node -e '
  const [needle, needPage] = process.argv.slice(1);
  const events = require("fs").readFileSync(0, "utf8").split("\n")
    .flatMap(l => { try { return [JSON.parse(l)]; } catch { return []; } });
  const items = events.filter(e => e.type === "item.completed" && e.item).map(e => e.item);
  const web = items.filter(i => i.type === "web_search");
  const kind = i => (i.action || {}).type;
  const searches = web.filter(i => kind(i) !== "open_page" && kind(i) !== "find_in_page")
    .map(i => (i.action || {}).query || i.query || "?");
  const pages = web.filter(i => kind(i) === "open_page").map(i => i.action.url || "?");
  const answer = (items.filter(i => i.type === "agent_message").map(i => i.text || "").pop() || "")
    .replace(/\s+/g, " ").trim();
  const error = events.filter(e => e.type === "error" || e.type === "turn.failed")
    .map(e => e.message || (e.error || {}).message || "").pop();
  const seen = searches.length + " recherche(s)" + (searches.length ? " (" + searches[0].slice(0, 60) + ")" : "")
    + (needPage === "1" ? ", " + pages.length + " page(s) lue(s)" + (pages.length ? " (" + pages[0].slice(0, 80) + ")" : "") : "")
    + " — " + (answer.slice(0, 200) || "pas de réponse" + (error ? " : " + error.slice(0, 200) : ""));
  const ko = !events.length ? "aucun événement JSON en sortie de codex exec"
    : !searches.length ? "aucune recherche dans la trace : " + seen
    : needPage === "1" && !pages.length ? "aucune lecture de page dans la trace : " + seen
    : !answer.toLowerCase().includes(needle.toLowerCase()) ? "réponse sans « " + needle + " » : " + seen
    : "";
  process.stdout.write(ko ? "KO " + ko : "OK " + seen);' "$@"; }

version=$(codex --version 2>/dev/null | tail -n 1)
summary=""

for MODEL in $MODELS; do
  echo
  echo "════ $version → $PROXY_URL | modèle $MODEL ════"
  rm -rf /work/* 2>/dev/null
  fails_before=$fails
  skips_before=$skips
  run() {
    codex exec --skip-git-repo-check --ephemeral \
      --dangerously-bypass-approvals-and-sandbox -m "$MODEL" "$@" \
      </dev/null 2>&1 | tail -n 20
  }
  # Scénarios web : --json pour la trace (voir verdict), et le bac à sable
  # en lecture seule plutôt que levé — une recherche n'exécute rien, et le
  # modèle n'a ainsi que le proxy pour atteindre le web (une commande curl
  # ne démarrerait pas, là où le conteneur, en réseau hôte, la laisserait
  # passer). stderr va dans un fichier, hors du JSONL : `diag` en rend la
  # fin sur un échec (une erreur de configuration de Codex n'est que là).
  web() {
    codex exec --skip-git-repo-check --ephemeral --sandbox read-only \
      --json -m "$MODEL" "$@" </dev/null 2>/tmp/codex-web.err
  }
  diag() { [ -s /tmp/codex-web.err ] && printf ' — stderr : %s' "$(tail -n 3 /tmp/codex-web.err | tr '\n' ' ')"; return 0; }

  echo "1. Réponse simple (POST /v1/responses, flux d'événements)"
  out=$(run "Réponds en un seul mot : quelle est la capitale de la France ?")
  case "$out" in *Paris*) pass "Paris" ;; *) fail "$out" ;; esac

  echo "2. Outils : écrire puis relire (function_call / function_call_output, plusieurs tours)"
  rm -f hello.txt
  out=$(run "Crée un fichier hello.txt contenant exactement le mot bonjour, affiche-le avec cat et confirme son contenu en une phrase.")
  if [ "$(cat hello.txt 2>/dev/null | tr -d '[:space:]')" = "bonjour" ]; then pass "hello.txt = bonjour — $(echo "$out" | tail -n 1)"; else fail "hello.txt absent ou différent — $out"; fi

  echo "3. Outils : modifier un fichier"
  mkdir -p src && printf 'def add(a, b):\n    return a - b\n' > src/calc.py
  out=$(run "Lis src/calc.py, corrige le bug évident, puis affiche le fichier corrigé avec cat.")
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

  echo "6. Recherche web hébergée (web_search déclaré par Codex, exécuté par le proxy)"
  # La réponse attendue est une URL que le nom du dépôt détermine : elle ne
  # dépend ni de l'actualité ni de la formulation (on ne cherche que
  # « github.com/ggml-org/llama.cpp », que toute bonne réponse contient,
  # /releases, /releases/latest ou lien Markdown compris). Demander la
  # dernière release aurait été fragile : le 05/10/2026 elle s'appelait
  # v0.6.0, après des années d'étiquettes bNNNN.
  if hosts web_search; then
    out=$(web "Trouve par une recherche web la page des releases du dépôt GitHub ggml-org/llama.cpp et réponds uniquement par son URL." | verdict "github.com/ggml-org/llama.cpp" 0)
    case "$out" in "OK "*) pass "${out#OK }" ;; *) fail "${out#KO }$(diag)" ;; esac
  else
    skip "web_search n'est pas hébergé par ce proxy (/healthz : tools.enabled = [${hosted}])"
  fi

  echo "7. Recherche puis lecture d'une page (web_search + web_fetch dans le même tour)"
  # La page lue est un fichier d'un dépôt public à une révision ÉPINGLÉE
  # (adresse par commit : son contenu ne changera jamais), écrit le
  # 05/10/2026 — aucun modèle ne l'a en mémoire. La ligne demandée contient
  # « proxy_ », qui ne s'écrit que d'une façon et que le prompt ne donne pas.
  if hosts web_search && hosts web_fetch; then
    out=$(web "Fais d'abord une recherche web sur le dépôt GitHub c4software/llmsetup. Ouvre ensuite la page https://raw.githubusercontent.com/c4software/llmsetup/93698d56c65d66cdf722473efcc8c8f9a668c19d/tools/llm-proxy-web.ts et réponds uniquement par la ligne qui définit la constante PREFIXE, recopiée telle quelle." | verdict "proxy_" 1)
    case "$out" in "OK "*) pass "${out#OK }" ;; *) fail "${out#KO }$(diag)" ;; esac
  else
    skip "web_search et web_fetch ne sont pas tous deux hébergés par ce proxy (/healthz : tools.enabled = [${hosted}])"
  fi

  failed=$((fails - fails_before))
  skipped=$((skips - skips_before))
  played=$((7 - skipped))
  line="$((played - failed))/$played"
  [ "$skipped" -gt 0 ] && line="$line, $skipped sauté(s)"
  summary="$summary
  $MODEL : $line"
done

echo
echo "Résumé :$summary"
[ "$skips" -gt 0 ] && echo "$skips scénario(s) sauté(s) : outils web non hébergés par le proxy."
[ "$fails" -eq 0 ] && echo "Tout passe." || { echo "$fails scénario(s) en échec."; exit 1; }
