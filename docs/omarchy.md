# Omarchy

`omarchy models` (Omarchy) liste les modèles que partagent les machines du
tailnet et lance un agent (pi, OpenCode, Qwen Code) sur l'un d'eux. Le proxy
se présente comme une de ces machines : il publie la liste de ses modèles et
sert chacun à une base OpenAI-compatible. Il ne démarre ni n'arrête aucun
modèle : chacun y est annoncé en marche.

## Routes

| Route | Clé | Rôle |
|---|---|---|
| `GET /local-ai/models.json` | facultative | La liste qu'Omarchy lit sur chaque machine (schéma `1`) : les modèles de conversation que voit la clé, aucun sans clé |
| `POST /local-ai/<id>/v1/chat/completions` | clé du proxy | `/v1/chat/completions`, le `model` du corps remplacé par le nom préfixé |
| `GET /local-ai/<id>/v1/models` | clé du proxy | Le modèle seul, au format OpenAI |

Sans `[omarchy].enabled = true`, ces routes répondent `404`. Proxy ouvert
(sans `[proxy].api_keys` ni comptes), la liste porte tous les modèles de
conversation. Elle ne porte que des noms, le contexte, le plafond de réponse
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
enabled = true
```

Ce qu'un poste voit se règle par sa clé : les modèles permis du compte
(*Configuration → Comptes*) et les backends que le compte voit.

Voir [Configuration, `[omarchy]`](configuration.md#omarchy).

## Côté Omarchy

Omarchy lit la liste à `https://<nom>/local-ai/models.json`, avec une
vérification TLS ordinaire : `<nom>` est le nom MagicDNS d'un pair Linux
en ligne du tailnet, ou le `dns` d'un hôte appairé. Avec l'auth active, la
liste n'est utile que lue avec la clé de l'appairage :
`omarchy-local-ai` doit l'envoyer (`Authorization: Bearer`) avec chaque
lecture de la liste d'un hôte appairé et sonder un hôte appairé à son
`dns` quand ce nom n'est pas celui du tailnet. La version publiée
d'`omarchy-local-ai` ne fait ni l'un ni l'autre : elle trouve une liste
vide.

Le proxy répond au nom MagicDNS de sa machine, avec un certificat valide
(Tailscale : `tailscale serve --bg --set-path /local-ai
http://127.0.0.1:8000/local-ai`), ou à un autre nom (`llm.exemple.fr`,
derrière un reverse proxy). Une fois appairé, `omarchy models network` le
liste, le panneau et le sélecteur `/model` de pi le proposent, et
`omarchy agent --local-model llm/albert.mistral-small-3.2` lance un agent
dessus. Sous son nom MagicDNS, l'appairage porte le nom court (`host`) et
le nom complet (`dns`).

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
