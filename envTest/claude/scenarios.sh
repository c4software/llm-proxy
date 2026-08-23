#!/bin/sh
# Les scénarios de validation Claude Code → proxy. Chacun imprime PASS ou
# FAIL avec ce qu'il a vu ; le script sort en erreur si l'un échoue.
# Tout s'exécute ICI, dans /work du conteneur — rien n'est écrit ailleurs.
set -u
cd /work
fails=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails + 1)); }

# Sortie JSON de `claude -p` → le champ `result` (dernier objet de la
# sortie ; les avertissements «unrecognized_model» précèdent).
result() { node -e '
  const raw = require("fs").readFileSync(0, "utf8");
  const j = JSON.parse(raw.slice(raw.indexOf("{"), raw.lastIndexOf("}") + 1));
  const tag = j.subtype === "error_max_turns" ? "ERROR: plafond de tours atteint (boucle du modèle) — "
            : j.is_error ? "ERROR: " : "";
  process.stdout.write(tag + String(j.result ?? j.subtype ?? ""));'; }
# --max-turns : en mode -p, Claude Code ne plafonne PAS les tours. Un
# modèle qui s'emballe (même appel d'outil répété à l'infini — vu sur
# un 27B : 755 tours, contexte à 114 k tokens, GPU occupé une heure)
# tournerait jusqu'à ce qu'on le tue. Le scénario le plus long en prend
# une vingtaine ; au-delà de 40, c'est un échec, dit comme tel.
MAX_TURNS=${MAX_TURNS:-40}
run() { claude -p --dangerously-skip-permissions --max-turns "$MAX_TURNS" --output-format json "$@" 2>/dev/null | result; }

version=$(claude --version 2>/dev/null | head -1)
summary=""

for model in $MODELS; do
export ANTHROPIC_MODEL="$model"
fails_before=$fails
echo
echo "════ Claude Code $version → $ANTHROPIC_BASE_URL | modèle $model ════"
rm -rf /work/* 2>/dev/null

echo "1. Réponse simple (POST /v1/messages, flux SSE)"
out=$(run "Réponds en un seul mot : quelle est la capitale de la France ?")
case "$out" in *Paris*) pass "$out" ;; *) fail "$out" ;; esac

echo "2. Outils : Write + Bash + Read (tool_use / tool_result, plusieurs tours)"
rm -f hello.txt
out=$(run "Crée un fichier hello.txt contenant exactement le mot bonjour, affiche-le avec cat, puis relis-le avec l'outil Read et confirme son contenu en une phrase.")
if [ "$(cat hello.txt 2>/dev/null | tr -d '[:space:]')" = "bonjour" ]; then pass "hello.txt = bonjour — $out"; else fail "hello.txt absent ou différent — $out"; fi

echo "3. Outils : Glob + Grep + Edit (arguments JSON fragmentés en flux)"
mkdir -p src && printf 'def add(a, b):\n    return a - b\n' > src/calc.py
out=$(run "Cherche les fichiers Python de ce dossier, lis-les, corrige le bug évident dans src/calc.py avec l'outil Edit, puis affiche le fichier corrigé avec cat.")
if grep -q 'return a + b' src/calc.py; then pass "src/calc.py corrigé — $out"; else fail "src/calc.py non corrigé — $out"; fi

echo "4. Image dans un tool_result (Read d'un .png) : relayée ou remplacée, jamais un 500"
printf 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==' | base64 -d > dot.png
out=$(run "Utilise l'outil Read sur /work/dot.png et dis en une phrase ce que tu as obtenu.")
case "$out" in ERROR:*) fail "$out" ;; *) pass "$out" ;; esac

echo "5. count_tokens (exact via tokenize_path, sinon estimation)"
out=$(node -e '
  fetch(process.env.ANTHROPIC_BASE_URL + "/v1/messages/count_tokens", {
    method: "POST",
    headers: {"content-type": "application/json", "anthropic-version": "2023-06-01",
              "x-api-key": process.env.ANTHROPIC_API_KEY},
    body: JSON.stringify({model: process.env.ANTHROPIC_MODEL,
      messages: [{role: "user", content: "Bonjour le monde, ceci est un test."}]}),
  }).then(r => r.text()).then(t => process.stdout.write(t));')
case "$out" in *input_tokens*) pass "$out" ;; *) fail "$out" ;; esac

echo "6. GET /v1/models à la forme Anthropic (en-tête anthropic-version)"
out=$(node -e '
  fetch(process.env.ANTHROPIC_BASE_URL + "/v1/models", {
    headers: {"anthropic-version": "2023-06-01", "x-api-key": process.env.ANTHROPIC_API_KEY},
  }).then(r => r.json()).then(j => process.stdout.write(
    (j.data || []).some(m => m.type === "model" && m.id === process.env.ANTHROPIC_MODEL)
      ? "modèle présent, " + j.data.length + " entrées" : "ABSENT: " + JSON.stringify(j).slice(0, 200)));')
case "$out" in ABSENT*) fail "$out" ;; *) pass "$out" ;; esac

echo "7. Erreur au dialecte Anthropic (corps invalide → 400 {\"type\": \"error\"})"
# Un modèle inconnu ne ferait pas l'affaire : [anthropic.model_map] a
# sans doute un «default» qui l'attrape.
out=$(node -e '
  fetch(process.env.ANTHROPIC_BASE_URL + "/v1/messages", {
    method: "POST",
    headers: {"content-type": "application/json", "anthropic-version": "2023-06-01",
              "x-api-key": process.env.ANTHROPIC_API_KEY},
    body: "ceci n est pas du JSON",
  }).then(r => r.text().then(t => process.stdout.write(r.status + " " + t)));')
case "$out" in "400 "*'"type":"error"'*) pass "$out" ;; *) fail "$out" ;; esac

echo "8. Création de code : module Node + tests, exécutés par le modèle"
rm -rf stats && mkdir stats && cd stats
out=$(run "Crée un module CommonJS stats.js qui exporte mean(tableau) et median(tableau) (médiane correcte pour un nombre pair d'éléments), puis un fichier test.js qui les vérifie avec node:assert sur au moins quatre cas (dont un tableau pair pour median), exécute node test.js et ne t'arrête que quand il passe. Termine par une phrase.")
if node test.js >/dev/null 2>&1 && node -e 'const s=require("./stats");process.exit(s.mean([1,2,3,4])===2.5&&s.median([1,2,3,4])===2.5&&s.median([3,1,2])===2?0:1)'; then pass "stats.js correct, test.js passe — $out"; else fail "$out"; fi
cd /work

echo "9. Corriger un bug pour faire passer un test existant, sans toucher au test"
rm -rf slug && mkdir slug && cd slug
cat > slugify.js <<'JS'
// Transforme un titre en identifiant d'URL.
module.exports = function slugify(title) {
  return title.toLowerCase().replace(/ /g, "-");
};
JS
cat > slugify.test.js <<'JS'
const assert = require("node:assert");
const slugify = require("./slugify");
assert.strictEqual(slugify("Hello World"), "hello-world");
assert.strictEqual(slugify("  Déjà   vu ! "), "deja-vu");
assert.strictEqual(slugify("C'est l'été"), "c-est-l-ete");
assert.strictEqual(slugify("--a--b--"), "a-b");
console.log("ok");
JS
sum=$(cksum slugify.test.js)
out=$(run "Lance node slugify.test.js : il échoue. Corrige slugify.js pour qu'il passe (accents retirés, tout caractère non alphanumérique devient un tiret, tirets fusionnés et retirés aux extrémités). Interdiction de modifier slugify.test.js. Relance le test jusqu'à ce qu'il passe.")
if [ "$(cksum slugify.test.js)" != "$sum" ]; then fail "le test a été modifié — $out"
elif node slugify.test.js >/dev/null 2>&1; then pass "slugify.js corrigé, test intact — $out"; else fail "test toujours en échec — $out"; fi
cd /work

echo "10. Refactor multi-fichiers : extraire une fonction partagée, tests inchangés"
rm -rf shop && mkdir shop && cd shop
cat > cart.js <<'JS'
function cartTotal(items) {
  let t = 0;
  for (const i of items) t += Math.round(i.price * i.qty * 100) / 100;
  return t;
}
module.exports = { cartTotal };
JS
cat > invoice.js <<'JS'
function invoiceTotal(lines) {
  let t = 0;
  for (const l of lines) t += Math.round(l.price * l.qty * 100) / 100;
  return t;
}
module.exports = { invoiceTotal };
JS
cat > shop.test.js <<'JS'
const assert = require("node:assert");
const { cartTotal } = require("./cart");
const { invoiceTotal } = require("./invoice");
const lines = [{ price: 1.1, qty: 3 }, { price: 2.05, qty: 2 }];
assert.strictEqual(cartTotal(lines), 7.4);
assert.strictEqual(invoiceTotal(lines), 7.4);
assert.ok(require("fs").existsSync("./money.js"), "money.js attendu");
console.log("ok");
JS
out=$(run "cart.js et invoice.js dupliquent le même calcul de total. Extrais-le dans un nouveau module money.js (fonction sumLines(lines)) et fais-le utiliser par les deux fichiers avec l'outil Edit. Ne modifie pas shop.test.js. Lance node shop.test.js et assure-toi qu'il passe.")
if node shop.test.js >/dev/null 2>&1 && grep -q "require(\"./money\")\|require('./money')" cart.js invoice.js; then pass "money.js extrait, tests verts — $out"; else fail "$out"; fi
cd /work

echo "11. Plusieurs lectures dans un tour (outils en parallèle) et réponse factuelle"
rm -rf docs && mkdir docs
printf 'Le port SSH est 22.\n' > docs/a.txt; printf 'Le port HTTPS est 443.\n' > docs/b.txt; printf 'Le port Postgres est 5432.\n' > docs/c.txt
out=$(run "Lis les trois fichiers du dossier docs (tu peux les lire en parallèle) et réponds uniquement par le numéro du port Postgres.")
case "$out" in *5432*) pass "$out" ;; *) fail "$out" ;; esac

echo "12. Write : accents, guillemets, antislashs — l'échappement JSON des arguments d'outil"
rm -f exact.txt
out=$(run "Crée le fichier exact.txt contenant ces trois lignes, caractères compris (guillemets, antislashs, accents) :
« élève » et l'été
\"guillemets\" et \\antislash\\
fin")
# Ce qu'on vérifie, c'est que les caractères délicats traversent la
# traduction intacts — pas que le modèle recopie mot pour mot.
if grep -q 'élève' exact.txt 2>/dev/null && grep -q "l'été" exact.txt && grep -q '"guillemets"' exact.txt && grep -q '\\antislash\\' exact.txt; then pass "exact.txt : $(tr '\n' '|' < exact.txt)"; else fail "contenu : $(cat exact.txt 2>/dev/null | head -5 | tr '\n' '|') — $out"; fi

echo "13. Bash : pipeline et chiffre vérifiable"
out=$(run "Avec une seule commande shell, compte le nombre total de lignes des fichiers du dossier docs et réponds uniquement par ce nombre.")
case "$out" in *3*) pass "$out" ;; *) fail "$out" ;; esac

failed=$((fails - fails_before))
summary="$summary
  $model : $((13 - failed))/13"
done

echo
echo "Résumé :$summary"
[ "$fails" -eq 0 ] && echo "Tout passe." || { echo "$fails scénario(s) en échec."; exit 1; }
