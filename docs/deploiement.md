# Déploiement

Cette page explique comment lancer le proxy avec Docker Compose ou
Coolify, et comment sauvegarder et restaurer ses données. Les réglages
eux-mêmes sont décrits dans [Configuration](configuration.md).

## Docker Compose

```bash
cp .env.example .env        # y mettre ALBERT_API_KEY et SEARXNG_SECRET
docker compose up -d --build
```

`SEARXNG_SECRET` est la clé du service `searxng`, le métamoteur des
[outils hébergés](outils-heberges.md). Générez-la avec `openssl rand -hex 32`.
Sans elle, `docker compose` avertit mais démarre.

`./data` est monté comme volume. Il porte la configuration
(`config.toml`, créée au premier démarrage depuis l'exemple) et les
bases, dont `stats.db`. Elles survivent aux redémarrages et aux
reconstructions d'image.

### L'override local

Le `docker-compose.yml` du dépôt ne contient que le proxy et SearXNG. Le
reste va dans un `docker-compose.override.yml`, non versionné, que Docker
Compose fusionne automatiquement : l'exécuteur de code, un
`container_name`, un port à ne pas publier, une base commune à étendre.

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
# n'y garder que les blocs voulus, puis :
docker compose config       # affiche la pile fusionnée
docker compose up -d --build
```

Le service `executor` ([exécution de code](execution-de-code.md)) vit
dans cet override. Sans lui, il n'est ni construit ni lancé. Pour
l'activer :

1. Lire [Ce que le bac à sable vaut](execution-de-code.md#ce-que-le-bac-à-sable-vaut).
2. Copier le bloc 1 de l'exemple.
3. Poser `EXECUTOR_TOKEN` dans `.env`.
4. Suivre la [procédure de validation](execution-de-code.md#valider-lexécuteur-sur-le-déploiement).

Détail des deux fichiers :
[Le compose de base et son override](execution-de-code.md#le-compose-de-base-et-son-override).

## Coolify

1. Nouvelle ressource → Dockerfile, pointée sur ce dépôt.
   - Répertoire de base : `/` (la racine est le contexte de build).
   - Emplacement du Dockerfile : `/proxy/Dockerfile`.
   - Port : 8000.
2. Déclarer un **volume persistant sur `/app/data`** (configuration et bases).
3. Ajouter les secrets en variables d'environnement (`ALBERT_API_KEY`…).
4. Exposer le service via Nginx Proxy Manager sur un sous-domaine interne.

Les réglages se modifient ensuite dans `data/config.toml`, dans le volume.

Le Dockerfile seul ne livre pas :

- **SearXNG** : pour la recherche web, déployer une instance à part et y
  pointer `tools.web_search.searxng_url`.
- **L'exécuteur de code** : c'est un second service, avec ses propres
  options de sécurité.

## Sauvegarde et restauration

`data/` contient ce qu'on ne peut pas reconstruire :

- les bases `stats.db`, `memory.db`, `accounts.db`, `conversations.db` ;
- **`client.secret`** : le perdre ou le remplacer fait perdre toutes les
  mémoires, toutes les clés gérées et toutes les sessions ;
- `config.toml`.

Les bases sont en WAL : un `cp` pendant que le proxy écrit ne donne pas
une copie fiable. Utilisez `python -m llm_proxy.backup`. Il copie les
bases à chaud de façon cohérente (API de sauvegarde SQLite) et met le
tout dans **une** archive datée, en mode `600` car elle contient le
secret.

### Sauvegarder

```bash
# dans data/backups/, les 14 plus récentes gardées
docker compose exec albert-proxy python -m llm_proxy.backup
docker compose exec albert-proxy python -m llm_proxy.backup /app/data/backups --keep 30

# vérifier une archive sans rien toucher :
# chaque base rouverte (integrity_check, lignes par table), TOML relu, taille du secret
docker compose exec albert-proxy python -m llm_proxy.backup \
  --check /app/data/backups/llm-proxy-20261017-030000.tar.gz
```

**Depuis la page.** Avec les [comptes OIDC](tableau-de-bord.md#comptes-oidc),
un admin télécharge la même archive dans *Configuration → Comptes →
Sauvegarde* (`GET /ui/admin/backup`). Elle est faite à l'instant, dans un
dossier temporaire effacé après l'envoi. L'admin la garde ainsi **hors du
disque de `data/`**, à l'abri de sa perte. Sans comptes, pas d'admin ni
de bouton : utilisez la commande.

Le **`.env`** (clé Albert, `OIDC_CLIENT_SECRET`…) n'est pas dans
l'archive. Il vit hors de `data/` : sauvegardez-le à part.

### Restaurer

Arrêtez le proxy d'abord : une base ouverte ne doit pas changer sous lui.

```bash
cd ~/stacks
docker compose stop albert-proxy
docker compose run --rm --no-deps -T albert-proxy python -m llm_proxy.backup \
  --restore /app/data/backups/llm-proxy-20261017-030000.tar.gz
docker compose start albert-proxy
```

- L'archive est vérifiée d'abord. Abîmée, rien n'est touché.
- Chaque fichier revient à son chemin configuré.
- Ce qui s'y trouvait part dans `data/pre-restore-<date>/`. Rien n'est écrasé.

**Testez la restauration** de temps en temps, sans toucher la
production : `--check` sur la dernière archive, ou `--restore … --to
/tmp/essai` puis un coup d'œil aux fichiers.
