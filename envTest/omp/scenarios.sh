#!/bin/sh
# Les scénarios de validation omp → proxy, par l'API OpenAI
# (/v1/chat/completions), rejoués pour chaque modèle de MODELS. PASS/FAIL
# par scénario, sortie en erreur si l'un échoue. Tout se passe dans /work du
# conteneur.
#
# Le provider est celui de l'extension /omp/extensions/llm-proxy.ts (posée
# par le Dockerfile) : elle lit GET /v1/models du proxy et enregistre ses
# modèles sous le nom « albert » — d'où `--model albert/<modèle de MODELS>`.
# Le dossier d'agent est vide : rien d'autre n'est chargé (--no-extensions
# coupe la découverte, -e charge ce qu'on nomme).
#
# Scénario 6 : la recherche web HÉBERGÉE par le proxy (README principal,
# « Outils hébergés »). omp parle chat/completions et ne déclare rien : les
# outils lui viennent D'OFFICE, par [chat].always du proxy, qui les présente
# au modèle, les exécute dans sa boucle et rend UNE réponse — omp n'en voit
# ni l'appel ni le résultat. (Jusqu'au 07/10/2026 l'outil venait d'une
# extension, llm-proxy-web.ts, retirée depuis.) omp a aussi un `web_search`
# INTÉGRÉ, qui cherche depuis le conteneur (DuckDuckGo et d'autres, sans
# clé) : le scénario doit donc prouver que c'est le proxy qui a cherché —
# voir `verdict`. Il est SAUTÉ (SKIP, ni PASS ni FAIL) quand le proxy visé
# n'héberge pas web_search ou ne le présente pas d'office — lu dans
# /healthz. Avec [chat].always, les scénarios 1 à 5 reçoivent eux aussi ces
# outils, à la suite de ceux d'omp — sauf celui dont omp enverrait déjà le
# nom (son propre `web_search`, s'il le déclare ainsi : pas vérifié).
set -u
cd /work
fails=0
skips=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails + 1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; skips=$((skips + 1)); }

# Ce que le proxy héberge et présente, lu dans /healthz — exempté de clé :
# `hosted`, ses outils actifs (« web_search web_fetch ») ; `offered`, ceux
# qu'il présente D'OFFICE à toute requête /v1/chat/completions
# ([chat].always, vide si [chat].hosted_tools est faux). Vides si rien n'est
# activé, si le proxy est plus ancien que ces réglages ou s'il ne répond pas.
health=$(node -e '
  fetch(process.argv[1] + "/healthz", {signal: AbortSignal.timeout(10000)})
    .then(r => r.json())
    .then(j => process.stdout.write(((j.tools || {}).enabled || []).join(" ") + "|"
      + ((j.chat || {}).hosted_tools ? (j.chat.always || []) : []).join(" ")))
    .catch(() => {});' "$PROXY_URL")
hosted=${health%%|*}
offered=${health#*|}
hosts() { case " $hosted " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
offers() { case " $offered " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# Le compteur du proxy : exécutions de web_search arrivées par
# /v1/chat/completions, lues sur son Usage API des outils (README principal,
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
        .filter(r => r.endpoint === "/v1/chat/completions" && r.tool === "web_search")
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

# Verdict du scénario web, sur la sortie de `omp --mode json` (JSONL, un
# événement par ligne) lue sur l'entrée standard. $1 : le texte que la
# réponse doit contenir (casse ignorée) ; $2 et $3 : le compteur du proxy
# avant et après (vides s'il n'est pas lisible). Imprime « OK … » ou « KO … ».
# La recherche est faite PAR LE PROXY, dans sa boucle : le client n'en voit
# rien — ni appel, ni résultat, c'est le principe des outils d'office. Sa
# trace ne peut donc prouver qu'une chose, et la réponse rien (un modèle
# l'écrit de mémoire) :
#   1. côté client, AUCUN outil exécuté (`tool_execution_start`) : ce n'est
#      pas omp qui a cherché — --no-tools ne lui en laisse d'ailleurs aucun ;
#   2. côté proxy, LA preuve : son compteur d'exécutions de web_search par
#      /v1/chat/completions a avancé pendant l'appel. « Au moins 1 » : le
#      compteur est celui du proxy entier, un autre client peut l'avancer
#      aussi — sur un proxy partagé, le scénario peut donc passer à tort.
#      Illisible, rien ne prouve la recherche : échec, et le libellé le dit.
# La réponse est le texte du dernier message `assistant` ; le message `user`
# (le prompt) est écarté.
verdict() { node -e '
  const [needle, before, after] = process.argv.slice(1);
  const events = require("fs").readFileSync(0, "utf8").split("\n")
    .flatMap(l => { try { return [JSON.parse(l)]; } catch { return []; } });
  const ran = [...new Set(events.filter(e => e.type === "tool_execution_start").map(e => String(e.toolName)))];
  const last = events.filter(e => e.type === "message_end" && (e.message || {}).role === "assistant")
    .map(e => e.message).pop() || {};
  const answer = (Array.isArray(last.content) ? last.content : [])
    .filter(b => b.type === "text").map(b => b.text || "").join(" ").replace(/\s+/g, " ").trim();
  const counted = before !== "" && after !== "" ? Number(after) - Number(before) : null;
  const seen = (counted === null ? "compteur du proxy illisible" : "compteur du proxy /v1/chat/completions : +" + counted) + " — "
    + (answer.slice(0, 200) || "pas de réponse" + (last.errorMessage ? " : " + String(last.errorMessage).slice(0, 200) : ""));
  const ko = !events.length ? "aucun événement JSON rendu par omp"
    : ran.length ? "un outil du client a été exécuté (" + ran.join(", ") + ") malgré --no-tools : " + seen
    : counted === null ? "rien ne prouve que le proxy a cherché (GET /v1/organization/usage/tools ne répond pas, ou refuse la clé) : " + seen
    : counted < 1 ? "aucune exécution de web_search comptée par le proxy sur /v1/chat/completions pendant cet appel : " + seen
    : !answer.toLowerCase().includes(needle.toLowerCase()) ? "réponse sans « " + needle + " » : " + seen
    : "";
  process.stdout.write(ko ? "KO " + ko : "OK " + seen);' "$@"; }

# Garde-fou : omp arrête une session au bout de cette durée (--max-time).
# Un modèle qui répète le même appel d'outil tournerait sinon sans fin (vu
# avec Claude Code : 754 tours, une heure de GPU). Le scénario le plus long
# d'un banc voisin prend quelques minutes.
MAX_TIME=${MAX_TIME:-20m}
version=$(omp --version 2>/dev/null | head -1)
summary=""

for MODEL in $MODELS; do
  echo
  echo "════ $version → $PROXY_URL | modèle $MODEL ════"
  rm -rf /work/* 2>/dev/null
  fails_before=$fails
  skips_before=$skips
  # Options communes, chacune lue dans `omp --help` (18.3.2) :
  #   -p                       répond et sort, sans interface (`--mode json`,
  #                            au scénario 6 : la même chose, en événements)
  #   --no-session             rien n'est gardé de la session
  #   --no-extensions, -e      pas de découverte d'extensions ; celles qu'on nomme
  #   --no-skills --no-rules --no-lsp --no-title
  #                            ni skills, ni règles, ni serveur de langage, ni
  #                            appel au modèle pour titrer la session
  #   --max-time               voir MAX_TIME
  #   --model albert/<modèle>  le provider de llm-proxy.ts
  # C'est la commande validée à la main le 05/10/2026, dans le même ordre
  # (le prompt en dernier, derrière -p), --max-time en plus. $1 : le
  # prompt ; la suite : les options propres à l'appel.
  agent() {
    prompt=$1
    shift
    omp --no-session --no-extensions -e /omp/extensions/llm-proxy.ts "$@" \
      --no-skills --no-rules --no-lsp --no-title --max-time "$MAX_TIME" \
      --model "albert/$MODEL" -p "$prompt"
  }
  # --auto-approve : aucun outil n'attend d'accord (c'est aussi le défaut
  # documenté d'omp, `tools.approvalMode: yolo` ; dit ici pour ne pas en
  # dépendre). </dev/null : sans terminal sur l'entrée standard, omp lit
  # l'entrée comme début de prompt. stderr est gardé avec la réponse : un
  # provider non enregistré (« [albert] découverte impossible ») n'est dit
  # que là.
  run() { agent "$1" --auto-approve </dev/null 2>&1 | tail -n 20; }
  # Scénario web : --mode json pour la trace (voir verdict), dans un fichier
  # — le compteur du proxy se relit APRÈS l'appel, avant le verdict.
  # --no-tools (« Disable all built-in tools », lu dans `omp --help`,
  # 18.6.3) ne laisse au modèle AUCUN outil d'omp : ni son `web_search`, ni
  # `read` (qui lit une URL), ni `bash` (un curl partirait du conteneur, pas
  # du proxy). La requête part donc sans `tools`, et ceux que le modèle
  # reçoit sont ceux du proxy. stderr va dans un autre fichier, hors du
  # JSONL : `diag` en rend la fin sur un échec.
  web() {
    agent "$1" --mode json --no-tools \
      </dev/null >/tmp/omp-web.jsonl 2>/tmp/omp-web.err
  }
  diag() { [ -s /tmp/omp-web.err ] && printf ' — stderr : %s' "$(tail -n 3 /tmp/omp-web.err | tr '\n' ' ')"; return 0; }

  echo "1. Réponse simple"
  out=$(run "Réponds en un seul mot : quelle est la capitale de la France ?")
  case "$out" in *Paris*) pass "$(echo "$out" | tail -n 1)" ;; *) fail "$out" ;; esac

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

  echo "6. Recherche web hébergée (outils d'office du proxy, [chat].always → boucle de /v1/chat/completions ; aucun outil côté omp)"
  # La réponse attendue est une URL que le nom du dépôt détermine : elle ne
  # dépend ni de l'actualité ni de la formulation (on ne cherche que
  # « github.com/ggml-org/llama.cpp », que toute bonne réponse contient,
  # /releases, /releases/latest ou lien Markdown compris).
  if ! hosts web_search; then
    skip "web_search n'est pas hébergé par ce proxy (/healthz : tools.enabled = [${hosted}])"
  elif ! offers web_search; then
    skip "web_search n'est pas présenté d'office par ce proxy (/healthz : chat.always = [${offered}] ; [chat] hosted_tools = true, always = [\"web_search\", \"web_fetch\"])"
  else
    before=$(searches 0)
    web "Trouve par une recherche web la page des releases du dépôt GitHub ggml-org/llama.cpp et réponds uniquement par son URL."
    after=""
    if [ -n "$before" ]; then after=$(searches $((before + 1))); fi
    out=$(verdict "github.com/ggml-org/llama.cpp" "$before" "$after" </tmp/omp-web.jsonl)
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
