# llm-proxy

Passerelle LLM qui met **plusieurs backends derrière un seul endpoint** et
laisse **chaque client parler sa propre API**. Les backends sont des
serveurs compatibles OpenAI : machines locales (llama.cpp, gufo), service
hébergé à quotas (Albert, de la DINUM), ou tout autre. Le client vise une
seule URL et choisit le backend par le **préfixe du nom de modèle**
(`bigchuck/qwen3.8-flash-next`, `albert/deepseek-v4-flash`).

Trois API côté client, une seule côté backend (`/v1/chat/completions`) :

- **OpenAI** (`/v1/chat/completions`) : relayée telle quelle.
- **Anthropic Messages** (`/v1/messages`) : traduite.
- **OpenAI Responses** (`/v1/responses`) : traduite.

Autour du relais : un catalogue `/v1/models` unifié, des **outils web
hébergés** (recherche par une instance SearXNG, lecture de page) que le
proxy exécute pour le modèle, un limiteur de quotas pour les backends qui
en ont, des statistiques persistantes à la forme de l'Usage API d'OpenAI
et un tableau de bord.

Le tableau de bord a son **chat**. Le modèle y sollicite aussi le
navigateur de l'utilisateur, toujours avec son accord : position,
presse-papiers, caméra, capture d'écran, agenda… Il fait tourner du
**JavaScript, du TypeScript, du PHP, du SQL (SQLite, PostgreSQL) et du bash dans une VM
Linux** dans un bac isolé, sans réseau, sur
les fichiers d'un dossier ouvert, qui ne quittent pas l'appareil : seul
le résultat des requêtes revient au modèle.

**En vidéo** (1 min 20, sans son) : synthèse, recherche web sourcée,
questions, artifact interactif retouché, graphique, SQL sur un dossier,
agenda et rappels, `/compact`. Les réponses y sont celles d'un modèle de
démonstration scénarisé ; l'interface, les outils et les calculs sont réels.

[![Présentation du chat : un tableau de bord de budget construit par le modèle, ses curseurs déplacés, les barres recalculées — cliquer pour lire la vidéo](docs/presentation.jpg)](docs/presentation.mp4)

![Dans le chat, un dossier ouvert : run_sql importe un CSV de 2 400 ventes et l'agrège par région, run_javascript en dessine le graphique, le modèle résume](docs/navigateur-sql-graphique.png)

Voir [Les outils du navigateur](docs/navigateur.md), chaque outil avec sa
capture.

![Tableau de bord /ui, vue Statistiques sur la semaine : menu latéral (chat et conversations, statistiques, configuration, thème), cartes de synthèse (requêtes, tokens, modèles actifs, erreurs), trafic par jour empilé par modèle, répartition des tokens, détail par modèle et par outil hébergé](docs/preview.jpg)

## Démarrage rapide

    cp .env.example .env        # y mettre ALBERT_API_KEY et SEARXNG_SECRET
    docker compose up -d --build
    curl -s http://localhost:8000/v1/models | jq '.data[].id'

La configuration vit dans un seul fichier, `data/config.toml`, créé au
premier démarrage depuis `data/config.example.toml` (documenté) ;
l'environnement ne sert qu'aux secrets, injectés par `${VAR}`. Le
tableau de bord est sur `http://localhost:8000/ui`.

## Documentation

| Sujet | Fichier |
|---|---|
| Ce que fait le proxy, fonction par fonction | [Fonctionnalités](docs/fonctionnalites.md) |
| Le dépôt, ses dossiers et ses fichiers | [Architecture](docs/architecture.md) |
| Docker Compose, Coolify, sauvegarde | [Déploiement](docs/deploiement.md) |
| `data/config.toml`, table par table | [Configuration](docs/configuration.md) |
| Clés, comptes OIDC, ce qui se sert sans clé | [Sécurité](docs/securite.md) |
| `/ui` : statistiques, partage équitable, comptes, configuration | [Tableau de bord](docs/tableau-de-bord.md) |
| Le chat de la page et ses outils (questions, dossier, artifacts) | [Le chat](docs/chat.md) |
| Position, caméra, presse-papiers… et JavaScript, TypeScript, PHP, SQLite, PostgreSQL dans le navigateur | [Les outils du navigateur](docs/navigateur.md) |
| Une VM Linux dans l'onglet (bash, python3), partagée avec un terminal : construction et limites | [La VM Linux du navigateur](docs/linux.md) |
| L'Usage API d'OpenAI servie par le proxy | [Statistiques](docs/statistiques.md) |
| `/v1/messages` : traduction, `model_map`, `[anthropic]` | [API Anthropic Messages](docs/api-anthropic.md) |
| `/v1/responses` : traduction, outils, `[responses]` | [API OpenAI Responses](docs/api-responses.md) |
| Recherche et lecture web, OCR, transcription, images, mémoire, MCP | [Outils hébergés](docs/outils-heberges.md) |
| Synthèse vocale, conception et clonage de voix (Qwen3-TTS), avec curl | [Voix](docs/voix.md) |
| Le bac à sable de `code_execution` | [Exécution de code](docs/execution-de-code.md) |
| Écrire un outil hébergé : le contrat | [docs/outils.md](docs/outils.md) |
| Vérifier un déploiement | [Vérification](docs/verification.md) |
| Ce qui ne marche pas, ou pas encore | [Limites connues](docs/limites.md) |
| Les bancs de vrais clients | [bench/README.md](bench/README.md) |
