# Déploiement

Le proxy se déploie avec Docker Compose ou Coolify. Les données peuvent
être sauvegardées et restaurées. Voir [Configuration](configuration.md)
pour les réglages.

## Docker Compose

```bash
cp .env.example .env        # y mettre ALBERT_API_KEY et SEARXNG_SECRET
docker compose up -d --build
```

`SEARXNG_SECRET` est la clé du service `searxng`, le métamoteur des
[outils hébergés](outils-heberges.md). Générez-la avec `openssl rand -hex 32`.
Sans cette variable, Docker Compose avertit mais démarre.

Au premier démarrage, si `data/config.toml` est absent,
`http://localhost:8000/ui` affiche la page d'installation. Celle-ci génère
`.env`, `data/config.toml` et `docker-compose.override.yml` à partir des
fournisseurs sélectionnés. Créez ces fichiers, renseignez les clés dans
`.env`, puis exécutez `docker compose up -d`.

`./data` est monté comme volume. Il contient la configuration
(`config.toml`) et les bases, dont `stats.db`. Ces données sont conservées
après les redémarrages et les reconstructions de l'image.

### L'override local

Le `docker-compose.yml` du dépôt définit uniquement le proxy et SearXNG.
Les autres services et réglages se définissent dans
`docker-compose.override.yml`, non versionné et fusionné automatiquement
par Docker Compose. Il peut notamment contenir l'exécuteur de code, un
`container_name`, un port non publié ou une base commune à étendre.

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
# n'y garder que les blocs voulus, puis :
docker compose config       # affiche la pile fusionnée
docker compose up -d --build
```

Le service `executor` ([exécution de code](execution-de-code.md)) est
défini dans cet override. Sans lui, il n'est ni construit ni lancé. Pour
l'activer :

1. Lire [Ce que le bac à sable vaut](execution-de-code.md#ce-que-le-bac-à-sable-vaut).
2. Copier le bloc 1 de l'exemple.
3. Définir `EXECUTOR_TOKEN` dans `.env`.
4. Suivre la [procédure de validation](execution-de-code.md#valider-lexécuteur-sur-le-déploiement).

Détails des deux fichiers :
[Le compose de base et son override](execution-de-code.md#le-compose-de-base-et-son-override).

### L'image publiée

Chaque tag `v*` publie l'image du proxy sur
`ghcr.io/c4software/llm-proxy`, avec trois étiquettes : le tag
(`v2026.11.14.1`), `latest` et `sha-xxxxxxx`. L'image est publiée par le workflow
`.github/workflows/proxy.yml`. Pour l'utiliser au lieu de construire
l'image localement, définir `image:` dans l'override (bloc 2 de
l'exemple), puis :
```bash
git pull                    # compose et documentation
docker compose pull albert-proxy && docker compose up -d albert-proxy
```

Pour revenir à une version précédente, définir son étiquette dans `image:`
(`ghcr.io/c4software/llm-proxy:v2026.11.12.6`) et réexécuter les deux
commandes. Si le paquet est privé, exécuter `docker login ghcr.io` sur la
machine avec un jeton doté de `read:packages`. La commande
`docker compose up -d --build` reconstruit toujours l'image localement,
par exemple pour une version modifiée.

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

Seuls `./data` (configuration et bases) et `/tmp` sont accessibles en
écriture. `/tmp` est utilisé par les outils : conversions audio de
`transcribe` et fetch git de l'outil [`repository`](depots.md). Voir
[Sécurité](securite.md#le-conteneur-du-proxy).

Un déploiement qui recopie le `docker-compose.yml` sans `git pull` doit
reprendre ces lignes. Un override qui monte d'autres dossiers en écriture
doit les déclarer dans `volumes:`.

## Coolify

1. Créer une ressource Dockerfile pointant sur ce dépôt.
   - Répertoire de base : `/` (la racine est le contexte de build).
   - Emplacement du Dockerfile : `/proxy/Dockerfile`.
   - Port : 8000.
2. Déclarer un **volume persistant sur `/app/data`** (configuration et bases).
3. Définir les secrets comme variables d'environnement (`ALBERT_API_KEY`…).
4. Exposer le service via Nginx Proxy Manager sur un sous-domaine interne.

Les réglages se modifient ensuite dans `data/config.toml`, dans le volume.
Certains s'appliquent sans redémarrage (`docker compose kill -s HUP albert-proxy`
ou « Recharger » dans la page) : voir [Rechargement à chaud](configuration.md#rechargement-à-chaud).

Le Dockerfile seul ne livre pas :

- **SearXNG** : pour la recherche web, déployer une instance à part et y
  pointer `tools.web_search.searxng_url`.
- **L'exécuteur de code** : c'est un second service, avec ses propres
  options de sécurité.

## Sauvegarde et restauration

`data/` contient les données qui ne peuvent pas être reconstruites :

- les bases `stats.db`, `memory.db`, `accounts.db`, `conversations.db` ;
- **`client.secret`** : sa perte ou son remplacement invalide toutes les
  mémoires, les clés gérées et les sessions ;
- `config.toml` ;
- `extensions.json` : les extensions et les outils activés dans la page.

`conversations.db` libère l'espace disque occupé par les conversations et
images supprimées (auto_vacuum incrémental). Pour les bases créées avant
ce réglage, l'activer une fois avec le proxy arrêté : `VACUUM` réécrit
toute la base, il faut donc prévoir un espace libre au moins égal à sa
taille.

```bash
sqlite3 data/conversations.db "PRAGMA auto_vacuum=INCREMENTAL; VACUUM;"
```

Les bases utilisent le mode WAL : une copie avec `cp` pendant une écriture
du proxy n'est pas fiable. Utilisez `python -m llm_proxy.backup`, qui
effectue une copie cohérente à chaud avec l'API de sauvegarde SQLite et
regroupe les données dans une archive datée unique, en mode `600` car elle
contient le secret.

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
un administrateur télécharge la même archive depuis *Configuration → Comptes →
Sauvegarde* (`GET /ui/admin/backup`). L'archive est créée au moment de la
demande dans un dossier temporaire, supprimé après l'envoi. Elle peut ainsi
être conservée **hors du disque de `data/`**, à l'abri de sa perte. Sans
comptes, il n'y a ni administrateur ni bouton ; utilisez la commande.

Le fichier **`.env`** (clé Albert, `OIDC_CLIENT_SECRET`…) n'est pas inclus
dans l'archive. Il se trouve hors de `data/` et doit être sauvegardé à part.

### Restaurer

Arrêtez d'abord le proxy pour éviter de modifier une base ouverte.

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

**Vérifiez périodiquement la restauration hors production** : utilisez
`--check` sur la dernière archive, ou `--restore … --to /tmp/essai`, puis
examinez les fichiers.
