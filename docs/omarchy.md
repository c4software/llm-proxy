# Omarchy

`omarchy models` (Omarchy) liste les modèles que partagent les machines du
tailnet et lance un agent (pi, OpenCode, Qwen Code) sur l'un d'eux. Le proxy
se présente comme une de ces machines : il publie la liste de ses modèles et
sert chacun à une base OpenAI-compatible. Il ne démarre ni n'arrête aucun
modèle : chacun y est annoncé en marche.

## Routes

| Route | Clé | Rôle |
|---|---|---|
| `GET /local-ai/models.json` | aucune | La liste qu'Omarchy lit sur chaque machine (schéma `1`) |
| `POST /local-ai/<id>/v1/chat/completions` | clé du proxy | `/v1/chat/completions`, le `model` du corps remplacé par le nom préfixé |
| `GET /local-ai/<id>/v1/models` | clé du proxy | Le modèle seul, au format OpenAI |

Sans `[omarchy].models`, ces routes répondent `404`. La liste est servie
sans clé à quiconque joint le proxy : `[omarchy].models` borne ce qu'elle
révèle. Elle ne porte que des noms, le contexte, le plafond de réponse
(`[backends.<nom>].max_tokens`, à défaut 16384, borné par le contexte) et
les entrées acceptées (`text`, `image` pour un modèle multimodal d'un
backend `images = true`) ; elle ne contient aucune clé. Le chat passe par
l'auth ordinaire : la clé employée garde les modèles permis de son compte
et ses statistiques.

## Identifiants

Omarchy nomme un modèle par un identifiant de recette,
`^[a-z0-9]+([.-][a-z0-9]+)*$`. Le nom préfixé s'y ramène : minuscules, `/`
devient `.`, tout autre caractère `-`, une suite de séparateurs se réduit à
un seul. `albert/Mistral_Small-3.2` devient `albert.mistral-small-3.2`. Deux
noms qui donnent le même identifiant sont tous deux écartés. Omarchy lit au
plus 64 modèles.

## Configuration

```toml
[omarchy]
models = ["albert/*", "local/qwen*"]
```

Voir [Configuration, `[omarchy]`](configuration.md#omarchy).

## Côté Omarchy

Omarchy lit la liste à `https://<nom MagicDNS>/local-ai/models.json`, avec
une vérification TLS ordinaire, et n'interroge que les pairs Linux en ligne
du tailnet. Deux cas :

- **Le proxy répond au nom MagicDNS de sa machine** avec un certificat
  valide (Tailscale : `tailscale serve --bg --set-path /local-ai
  http://127.0.0.1:8000/local-ai`). `omarchy models network` le liste ;
  une fois appairé, le panneau et le sélecteur `/model` de pi le
  proposent. L'appairage porte alors le nom court MagicDNS (`host`) et le
  nom complet (`dns`).
- **Le proxy répond à un autre nom** (`llm.exemple.fr`, derrière un reverse
  proxy). Seul le lancement nommé fonctionne, une fois appairé :
  `omarchy agent --local-model llm/albert.mistral-small-3.2`. Le panneau
  montre la machine hors ligne et le sélecteur de pi ne la propose pas :
  Omarchy ne sonde pas un hôte appairé hors du tailnet.

Le proxy ne doit pas tourner sur une machine Omarchy qui partage aussi ses
propres modèles : `omarchy models share` retire toute route `tailscale
serve` sous `/local-ai` qu'il n'a pas posée.

### Appairer

L'appairage range le nom et la clé dans
`~/.config/omarchy/agent-local/remotes/<hôte>.{json,key}`. La clé est une
clé du proxy (*Configuration → Clés*, ou `[proxy].api_keys`) ; Omarchy
n'accepte que `[A-Za-z0-9_-]`, 16 à 128 caractères, ce que respecte une
clé gérée (uuid). Ses modèles permis, ses statistiques et sa révocation
valent pour chaque poste qui l'emploie.

**À la main**, sur le poste Omarchy :

```bash
d=~/.config/omarchy/agent-local/remotes
install -d -m 700 "$d"
(umask 077
 printf '%s' "$CLE" > "$d/llm.key"
 printf '{"host": "llm", "dns": "llm.exemple.fr"}\n' > "$d/llm.json")
```

`host` est le nom du fichier, sans extension ; `dns` le nom servi par le
proxy.

**Par `omarchy models pair <hôte SSH>`** : Omarchy y lance
`omarchy-local-ai share --export` et lit son JSON. Sur la machine du proxy,
qui n'a pas Omarchy, un script à ce nom dans le `PATH` d'une session SSH
non interactive (`/usr/local/bin/omarchy-local-ai`) suffit ; il lit une clé
gérée rangée en mode `600` dans `~/.config/llm-proxy/omarchy.key`, la même
pour tous les postes appairés :

```sh
#!/bin/sh
# Lu par `omarchy models pair` ; aucune autre commande n'est appelée.
[ "$1 $2" = "share --export" ] || exit 1
printf '{"schema": 1, "host": "llm", "dns": "llm.exemple.fr", "key": "%s", "shared": []}\n' \
  "$(cat ~/.config/llm-proxy/omarchy.key)"
```

`omarchy models forget llm` retire l'appairage.
