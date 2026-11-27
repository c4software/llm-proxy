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

Le proxy est pour Omarchy une passerelle : un serveur qui n'est pas une
machine Omarchy mais publie la même liste. Il faut un `omarchy-local-ai`
qui sait appairer une passerelle (`omarchy models pair https://…`) ; sans
cela, Omarchy lit la liste sans clé et la trouve vide.

```bash
omarchy models pair https://llm.exemple.fr
```

- La commande demande la clé, sans écho (sur l'entrée standard hors
  terminal), et la vérifie en lisant la liste avec elle avant de la ranger
  dans `~/.config/omarchy/agent-local/remotes/llm.{json,key}`. Une clé
  refusée ne range rien. Proxy ouvert (sans `[proxy].api_keys` ni
  comptes) : Entrée, sans clé.
- La clé est une clé du proxy (*Configuration → Clés*). Omarchy n'accepte
  que `[A-Za-z0-9_-]`, 16 à 128 caractères, ce que respecte une clé gérée
  (uuid). Ses modèles permis, ses statistiques et sa révocation valent
  pour chaque poste qui l'emploie : une clé par poste les sépare.
- L'adresse est le nom seul, sans chemin ni port, servi avec un
  certificat valide ; le premier label (`llm`) nomme la passerelle et ne
  peut pas être celui d'une machine du tailnet.
- Omarchy lit la liste avec la clé, à cette adresse, tailnet ou non. Les
  modèles arrivent dans `omarchy models network`, sous + dans le panneau
  des agents et dans le sélecteur `/model` de pi ;
  `omarchy agent --local-model llm/albert.mistral-small-3.2` lance un
  agent dessus.
- `omarchy models pair llm` redemande la clé (pour en changer),
  `omarchy models forget llm` retire l'appairage.

Le proxy ne doit pas être publié par `tailscale serve` sous `/local-ai`
sur une machine Omarchy qui partage aussi ses propres modèles :
`omarchy models share` retire toute route sous `/local-ai` qu'il n'a pas
posée.
