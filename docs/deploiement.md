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

Au premier démarrage, sans `data/config.toml`, la page
`http://localhost:8000/ui` est celle de l'**installation** : elle génère
`.env`, `data/config.toml` et `docker-compose.override.yml` d'après les
fournisseurs cochés. Les poser, compléter les clés dans le `.env`, puis
`docker compose up -d`.

`./data` est monté comme volume. Il porte la configuration
(`config.toml`) et les bases, dont `stats.db`. Elles survivent aux redémarrages et aux
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

### L'image publiée

Chaque tag `v*` publie l'image du proxy sur
`ghcr.io/c4software/llm-proxy`, avec trois étiquettes : le tag
(`v2026.11.14.1`), `latest` et `sha-xxxxxxx`. C'est le workflow
`.github/workflows/proxy.yml`. Pour l'utiliser au lieu de construire sur
place, poser `image:` dans l'override (bloc 2 de l'exemple), puis :

```bash
git pull                    # compose et documentation
docker compose pull albert-proxy && docker compose up -d albert-proxy
```

Revenir à une version précédente : mettre son étiquette dans `image:`
(`ghcr.io/c4software/llm-proxy:v2026.11.12.6`) et relancer les deux
commandes. Si le paquet est privé, `docker login ghcr.io` sur la machine,
avec un jeton qui a `read:packages`. `docker compose up -d --build`
reconstruit toujours sur place, par exemple pour une version modifiée.

### Durcissement du conteneur

Le service `albert-proxy` du `docker-compose.yml` tourne avec :

```yaml
user: "${PUID:-1000}:${PGID:-1000}"   # pas root
read_only: true                        # l'image en lecture seule
tmpfs:
  - /tmp:size=512m,mode=1777           # fichiers temporaires des outils
cap_drop: [ALL]                        # aucune capacité Linux
security_opt:
  - no-new-privileges:true             # pas d'élévation (setuid…)
```

Seuls `./data` (configuration, bases) et `/tmp` s'écrivent. `/tmp` sert aux
outils : conversions audio de `transcribe`, fetch git de l'outil
[`repository`](depots.md). Ce que le proxy décode d'un tiers (pages, PDF,
audio, packs git) a ainsi le moins de prise possible : une faille dans un
de ces décodeurs ne pourrait ni modifier l'image, ni gagner de droits.

Un déploiement qui recopie le `docker-compose.yml` (sans `git pull`) doit
y reporter ces lignes. Un override qui monte d'autres dossiers en
écriture les ajoute en `volumes:`.

## Coolify

1. Nouvelle ressource → Dockerfile, pointée sur ce dépôt.
   - Répertoire de base : `/` (la racine est le contexte de build).
   - Emplacement du Dockerfile : `/proxy/Dockerfile`.
   - Port : 8000.
2. Déclarer un **volume persistant sur `/app/data`** (configuration et bases).
3. Ajouter les secrets en variables d'environnement (`ALBERT_API_KEY`…).
4. Exposer le service via Nginx Proxy Manager sur un sous-domaine interne.

Les réglages se modifient ensuite dans `data/config.toml`, dans le volume.
Une partie s'applique sans redémarrer (`docker compose kill -s HUP albert-proxy`,
ou « Recharger » dans la page) : voir [Rechargement à chaud](configuration.md#rechargement-à-chaud).

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

`conversations.db` rend au disque la place des conversations et des
images supprimées (auto_vacuum incrémental). Une base créée avant ce
réglage le prend une fois, proxy arrêté (le `VACUUM` réécrit toute la
base : prévoir sa taille en espace libre) :

```bash
sqlite3 data/conversations.db "PRAGMA auto_vacuum=INCREMENTAL; VACUUM;"
```

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
