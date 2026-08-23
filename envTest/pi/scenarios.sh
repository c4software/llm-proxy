#!/bin/sh
# Les scénarios de validation pi → proxy, joués DEUX fois : par l'API
# OpenAI (provider llm-proxy) puis par l'API Anthropic (provider
# llm-proxy-anthropic) — un second client Anthropic, indépendant du SDK
# officiel, sur la même traduction. PASS/FAIL par scénario, sortie en
# erreur si l'un échoue. Tout se passe dans /work du conteneur.
set -u
cd /work
fails=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails + 1)); }

echo "pi $(pi --version 2>/dev/null | head -1) → $PROXY_URL | modèle $MODEL"

for provider in llm-proxy llm-proxy-anthropic; do
  echo
  echo "── provider $provider ──"
  run() { pi -p --no-session --provider "$provider" --model "$MODEL" "$@" 2>&1 | tail -n 20; }

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
done

echo
[ "$fails" -eq 0 ] && echo "Tout passe." || { echo "$fails scénario(s) en échec."; exit 1; }
