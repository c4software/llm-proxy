# Vérification

Cette page rassemble les commandes pour vérifier qu'un proxy installé
répond, route bien et compte l'usage. Elle donne aussi les commandes des
tests automatiques. Les exemples supposent le proxy sur
`localhost:8000`.

## Santé et catalogue

```bash
curl http://localhost:8000/healthz       # {"status":"ok"} sans clé (auth active)
curl -H "Authorization: Bearer $CLE" http://localhost:8000/healthz   # le détail

curl -s http://localhost:8000/v1/models | jq '.data[].id'
```

## Tableau de bord

Ouvrez `http://localhost:8000/ui`.

- Avec `proxy.api_keys`, la page demande la clé, puis la garde en cookie.
- Avec `[oidc]`, un bouton « Se connecter » mène au fournisseur.
- Les touches D / W / A changent de période.

## Comptes OIDC

Si `[oidc]` est configuré, vérifiez ce que la page proposera. Puis testez
une clé créée dans *Configuration → Clés* :

```bash
curl -s http://localhost:8000/ui/auth          # {"mode":"oidc",…}
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/models \
  -H "Authorization: Bearer <uuid de la clé>"   # 200 ; 401 une fois révoquée
```

## Usage

```bash
# usage par modèle, tout l'historique (Usage API OpenAI)
curl -s "http://localhost:8000/v1/organization/usage/completions\
?start_time=0&bucket_width=all&group_by[]=model" \
  | jq '.data[0].results[] | {model, num_model_requests,
                              tokens: (.input_tokens + .output_tokens)}'

# les 7 derniers jours, un seau par jour
curl -s "http://localhost:8000/v1/organization/usage/completions\
?start_time=$(( $(date +%s) - 604800 ))&bucket_width=1d&limit=8" \
  | jq '.data[] | {jour: (.start_time | todate),
                   req: ([.results[].num_model_requests] | add // 0)}'

# outils hébergés : par outil et par route d'appel, tout l'historique
curl -s "http://localhost:8000/v1/organization/usage/tools\
?start_time=0&bucket_width=all&group_by[]=tool&group_by[]=endpoint" \
  | jq '.data[0].results[] | {tool, endpoint, num_requests, num_errors,
                              num_limited, avg_duration_seconds}'
```

Voir [Statistiques](statistiques.md).

## Appel d'outils et `tool_choice`

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"albert/deepseek-v4-flash",
       "messages":[{"role":"user","content":"Liste /tmp"}],
       "tools":[{"type":"function","function":{"name":"list_dir",
         "parameters":{"type":"object","properties":{"path":{"type":"string"}},
         "required":["path"]}}}]}' \
  | jq '.choices[0].finish_reason'
```

Résultat attendu : `"tool_calls"`. Si le backend a `force_tool_choice`,
les logs affichent :

```
tool_choice=auto injecté (backend=albert, model=albert/deepseek-v4-flash, 1 tools)
```

Le réglage de chaque backend est rappelé au démarrage et lisible dans
`/healthz` (`tool_choice: false` = aucune injection).

Même test en local avec `"model":"bigchuck/qwen3-32b"` : la requête part
vers llama.cpp. Machine éteinte : `503 backend_offline`.

## Surfaces Anthropic et Responses

```bash
# surface Anthropic (si [anthropic].enabled) : un Message
curl -s http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-opus-5","max_tokens":64,
       "messages":[{"role":"user","content":"Bonjour"}]}' \
  | jq '{model, stop_reason, text: .content[0].text}'

# surface Responses (si [responses].enabled)
curl -s http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"bigchuck/qwen3.8-flash-next","input":"Bonjour",
       "max_output_tokens":64}' \
  | jq '{model, status, text: .output[0].content[0].text}'
```

## Tests automatiques

Deux jeux séparés : celui du proxy (traducteurs, outils, stats) et celui
de l'exécuteur de code. Aucun n'importe le code de l'autre.

```bash
python -m venv .venv
.venv/bin/pip install -r proxy/requirements-dev.txt -r services/executor/requirements-dev.txt
(cd proxy && ../.venv/bin/python -m pytest -q)
(cd services/executor && ../../.venv/bin/python -m pytest -q)
```

Lancer le proxy hors conteneur. Il lit `data/config.toml` à la racine, ou
`CONFIG_PATH` :

```bash
cd proxy && ../.venv/bin/uvicorn main:app --port 8000
```

Des bancs de vrais clients, en conteneurs, sont décrits dans
[bench/README.md](../bench/README.md).
