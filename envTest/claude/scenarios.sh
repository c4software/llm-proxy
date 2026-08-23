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
  process.stdout.write(String(j.is_error ? "ERROR: " + j.result : j.result));'; }
run() { claude -p --dangerously-skip-permissions --output-format json "$@" 2>/dev/null | result; }

echo "Claude Code $(claude --version 2>/dev/null | head -1) → $ANTHROPIC_BASE_URL | modèle $ANTHROPIC_MODEL"

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

echo
[ "$fails" -eq 0 ] && echo "Tout passe." || { echo "$fails scénario(s) en échec."; exit 1; }
