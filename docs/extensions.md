# Extensions

Une extension est un sous-dossier, déposé par l'admin, qui ajoute des
outils au proxy, au contrat de tous les outils
([Écrire un outil](outils.md#écrire-un-outil)) : des outils que le proxy
exécute ([outils hébergés](outils-heberges.md)), écrits en Python, et des
outils que le navigateur exécute pour le chat de la page, déclarés en
Python et exécutés en JavaScript. Elle sert à un outil spécialisé, propre
à un déploiement, qui n'a pas sa place dans le code du proxy. Une fois
chargé, l'outil se comporte comme un outil natif, là où son `offered` le
dit (voir [Le .py](#le-py)) :
- il est présenté au modèle (`/v1/chat/completions`, et `/v1/responses`
  ou `/v1/messages` s'il a une liaison) ;
- il s'appelle directement par [`/v1/tools`](outils.md#v1tools--lenveloppe) et `/mcp` ;
- il se coche dans le menu Outils du chat et dans les outils d'un webhook entrant ;
- il se permet ou se refuse par les droits d'une clé ;
- un outil du navigateur entre au menu Outils du chat, dans sa famille.

Les extensions sont chargées **au démarrage**. Pour en ajouter, en
retirer ou en modifier une, il faut redémarrer le proxy. Une extension
déposée paraît alors dans la page, **désactivée** : un admin l'active, et
la désactive, à chaud. Voir [Activer et désactiver](#activer-et-désactiver).

## Activer

Monter un dossier **en lecture seule**, et le nommer dans la
configuration :

```yaml
# docker-compose.override.yml
services:
  albert-proxy:
    volumes:
      - ./extensions:/app/extensions:ro
```

```toml
[extensions]
path = "/app/extensions"

[extensions.horloge]       # facultatif : les réglages d'une extension
timezone = "Europe/Paris"
```

Le dossier, chaque sous-dossier et chaque fichier doivent être hors
d'atteinte du proxy : sur
un montage en lecture seule (`:ro`), ou à un autre utilisateur que celui
du proxy et sans droit d'écriture pour lui. Un fichier qui appartient au
proxy est refusé, même en `0444`, puisqu'il pourrait en changer les
droits. Sinon, une faille d'écriture deviendrait du code exécuté au
démarrage suivant. C'est pour cela que le dossier ne va pas dans `./data`,
où le proxy écrit.

Une extension est un sous-dossier, chargé dans l'ordre alphabétique :

```text
extensions/
  horloge/
    horloge.py     obligatoire : ses outils (et leurs familles)
    horloge.js     l'exécution de ses outils du navigateur, s'il en a
  pull_request/
    pull_request.py
```

Le nom du sous-dossier est celui de ses fichiers et de sa table
`[extensions.<nom>]` : des lettres minuscules, des chiffres et `_`, sauf
`path`. Les autres fichiers du sous-dossier ne sont pas lus. Un `.py`
posé à la racine du dossier est écarté. Ce qui commence par `_` ou `.`
est ignoré. `[extensions.<nom>].enabled = false` laisse de côté
l'extension sans l'effacer.

## Activer et désactiver

*Configuration → Extensions* (un admin ; sans comptes OIDC, la page
connectée) liste les extensions du dossier :
- les extensions chargées, avec leurs outils, où chacun s'exécute (proxy,
  navigateur), où il est offert et s'il est configuré (un outil à qui il manque un réglage n'est pas en
  service) ;
- celles qui sont écartées, avec leur raison.

*Désactiver* retire tout de suite les outils d'une extension, partout :
présentation au modèle, `/v1/tools`, `/mcp`, menu Outils du chat,
webhooks ; son `.js` n'est plus servi. *Activer* les rend (pour un outil
du navigateur, au prochain chargement de la page). L'état est gardé dans `data/extensions.json`
(dans la [sauvegarde](deploiement.md#sauvegarde-et-restauration)) et
survit aux redémarrages. Une extension nouvellement déposée est
**désactivée** : ses outils ne servent qu'une fois qu'un admin l'a activée.
Un fichier `extensions.json` illisible est dit au journal et laissé tel
quel (la première bascule le réécrit) ; aucune extension n'est alors
activée. Après une mise à jour depuis une version où une extension était
activée par défaut, réactiver dans la page celles qui servaient.

`[extensions.<nom>].enabled = false` est l'arrêt de l'opérateur :
l'extension n'est pas chargée, et la page ne peut pas la réactiver
(`409`).

```
GET  /ui/extensions                {path, extensions: [{name, enabled, tools: [{name,
                                   runs, offered, configured}]} | {name, refused, stopped}]}
POST /ui/extensions/<nom>          {"enabled": false}
GET  /ui/extensions/<nom>/<nom>.js le module de la page, pour toute page connectée ;
                                   404 : désactivée, inconnue ou sans .js
```

## Le `.py`

L'exemple complet est
[`examples/extensions/horloge/`](../examples/extensions/horloge/horloge.py),
avec un outil de chaque moteur :

```python
from llm_proxy import extension as ext

REQUIRES = 1                      # la version du contrat attendue


class Horloge(ext.Tool):
    name = "horloge"

    def prompt(self, present):
        return ("Give the current date and time in a time zone. Use it when the "
                "answer depends on the current date or time; never guess them.")

    def parameters(self, present):
        return {"type": "object", "properties": {
            "timezone": {"type": "string", "description": "IANA time zone; omit for the default one."}}}

    async def run(self, args, call):
        zone = args.get("timezone") or ext.settings("horloge").get("timezone") or "UTC"
        …
        return ext.Result(f"{now:%Y-%m-%d %H:%M:%S} ({zone})")


# Un outil du navigateur : sa déclaration ici, son exécution dans horloge.js.
LOCAL_TIME = ext.BrowserTool(
    "local_time",
    description="Donne la date et l'heure de l'appareil de l'utilisateur, et son fuseau.",
)

FAMILY = ext.Family("horloge", "Horloge", tools=("local_time",),
                    hint="l'heure de l'appareil", on=True)

TOOLS = [Horloge(), LOCAL_TIME]   # ou TOOL = un seul
```

Le `.py` expose `TOOL`, un outil, ou `TOOLS`, une liste d'outils. Chaque
outil suit le contrat des outils natifs, décrit dans
[Écrire un outil](outils.md#écrire-un-outil) :
- **un outil du proxy** (`ext.Tool`) a un nom, un prompt, un schéma et une
  exécution, `run`, et rend un `Result` ou lève un `ToolError` ;
- **un outil du navigateur** (`ext.BrowserTool`) a un nom, une
  description, des `params` (et `required`, `settings`) ; il n'a pas de
  `run` : son exécution est dans le [`.js`](#le-js). Il est rangé dans
  une famille du menu Outils, `FAMILY` ou `FAMILIES` (`ext.Family` : clé,
  libellé, outils, `hint`, `on`, `fixed`, `authored`, `guidance`, comme
  une [famille native](navigateur.md#la-déclaration-proxy)). Sans famille
  déclarée, l'extension en a une à son nom.

**Le prompt** est celui de la fonction présentée au modèle : `prompt()`
(ou la `description` d'un outil du navigateur) en donne la description,
et les `description` de ses paramètres complètent. Seule la `guidance`
d'une famille du navigateur entre au prompt système, quand elle est
active. Voir [Le prompt d'un outil](outils.md#le-prompt-dun-outil).

**Où l'outil est offert** : `offered`, un ensemble de `ext.Offer`. `api`
le présente aux clients de l'API (`/v1/*`, `/mcp`), `web` au chat de la
page et aux webhooks entrants ; les deux par défaut, `web` seulement pour
un outil du navigateur. C'est une
présentation, pas une autorisation : voir
[Sécurité](securite.md#extensions).

`call.client` identifie le compte qui appelle (le condensé de sa clé),
de quoi tenir un état par compte. Il vaut `""` sur un proxy ouvert, sans
clé.

Le nom d'un outil suit celui d'une fonction : `[A-Za-z0-9_-]`, 64
caractères au plus. Il ne peut être ni celui d'un outil natif (du proxy
ou du navigateur) ou déjà chargé, ni `function`, `custom` ou `mcp`. La
clé d'une famille suit la règle du nom d'une extension, et n'est pas
celle d'une famille native.

## Le `.js`

L'exécution, dans la page, des outils du navigateur de l'extension. Le
proxy le sert, la page l'importe et appelle sa fonction par défaut :

```js
export default function (ext) {
  class LocalTime extends ext.PageTool {
    constructor() { super({ name: "local_time" }); }

    run() {
      const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      return `${new Date().toLocaleString("fr-FR")} (${zone})`;
    }
  }
  return { tools: [new LocalTime()] };
}
```

- `ext` donne `PageTool`, `defineFamily`, `failure`, `refused`, `REFUSE`,
  `clip`, `oneLine`, `reasonOf` : le module n'importe rien de la page.
- Chaque outil suit l'interface des outils de la page
  ([`PageTool`](navigateur.md#lexécution--pagetool)) : `run`, ou une
  carte. Ses `settings` déclarés sont dans `this.settings`.
- `families` est facultatif : une famille n'y est utile que pour un
  comportement (`available`, `enabled`, `state`), avec `defineFamily`.
- Les noms doivent être ceux que le `.py` déclare. Sinon, ou si le module
  échoue, l'écart est dit à la console du navigateur et ses outils
  restent hors du menu.

## Le contrat public

Une extension n'importe que `llm_proxy.extension`. Le reste du code du
proxy est interne et peut changer d'une version à l'autre.

| Nom | Rôle |
|---|---|
| `Tool`, `Result`, `Call`, `ToolError`, `Source`, `Artifact`, `ERRORS`, `Offer`, `Runs` | Le contrat des outils ([docs/outils.md](outils.md)) |
| `BrowserTool`, `Family` | Un outil du navigateur et sa famille du menu Outils |
| `download`, `post` | Le réseau du proxy : listes de refus de [`[tools.net]`](configuration.md#toolsnet), adresses publiques seulement (sauf `allow_private`). `download` suit les redirections en les vérifiant et lève dès un statut d'erreur ; `post(…, method=, auth=, accept=)` n'en suit aucune et rend le statut, de quoi parler à une API à jeton |
| `settings(nom)` | La table `[extensions.<nom>]`, `${VAR}` résolus. Aucune autre table |
| `logger(nom)` | Le journal du proxy, au nom de l'extension |
| `API` | La version du contrat (`1`). Une extension qui déclare `REQUIRES` plus haut n'est pas chargée |

Les bornes communes s'appliquent comme aux outils natifs : appels par
réponse (`[tools].max_calls`, ou `max_calls` de l'outil), délai
(`[tools].run_timeout`, ou `timeout`), taille du résultat
(`[tools].max_result_chars`).

## Ce qui est refusé au chargement

Une extension en faute est écartée **entière**, avec une ligne au journal
(`extension <nom> écartée : <raison>`). Les autres sont chargées, et le
proxy démarre quand même. Elle est écartée si :
- elle lève une exception à l'import, `SystemExit` compris (la trace est
  au journal) ;
- elle n'a ni `TOOL` ni `TOOLS` ;
- le nom d'un de ses outils est invalide, celui d'un outil natif, ou un
  type déjà déclaré par un autre outil ;
- elle a un membre public hors du contrat de `Tool` (`extra` est réservé
  aux outils MCP) ;
- elle déclare `REQUIRES` plus haut que `API`. Il est lu après l'import :
  le code de premier niveau du fichier a déjà tourné ;
- elle est désactivée par `enabled = false` ;
- elle déclare un outil du navigateur sans `<nom>.js`, ou un `<nom>.js`
  sans outil du navigateur ; une famille dont la clé est prise ou dont
  les outils ne sont pas ses outils du navigateur ;
- son nom est invalide, son `<nom>.py` manque, c'est un lien qui sort du
  dossier, ou un de ses fichiers (ou dossiers) est à la portée du proxy.

Une extension chargée est dite au journal
(`extension <nom> : <outils>`). Ses outils du proxy apparaissent dans la
liste des « outils hébergés ACTIFS ».

## Extension ou serveur MCP

| | Extension | [Serveur MCP](outils.md#serveurs-mcp) |
|---|---|---|
| Où le code tourne | Dans le processus du proxy ; le `.js`, dans la page | Dans son propre service |
| Ce qu'il voit | Tout le proxy : clés, jetons des comptes, bases | Ce qu'il reçoit dans l'appel |
| Le compte appelant | `call.client` | Non (en-têtes statiques) |
| Le réseau | Celui du proxy, avec ses refus | Le sien |
| Pour | Un outil qui a besoin du compte, du réseau gardé ou de la configuration du proxy | Un outil qui se suffit à lui-même |

Une extension a la **confiance totale** : voir
[Sécurité](securite.md#extensions).

## `pull_request` : proposer une modification

[`examples/extensions/pull_request/`](../examples/extensions/pull_request/pull_request.py)
propose une modification d'un projet GitLab, GitHub ou Gitea/Forgejo
(1.20 au moins) : une branche neuve, un commit, une pull request (merge
request) en **brouillon**, jamais fusionnée.

```toml
[extensions.pull_request]
forge = "gitlab"                   # gitlab | github | gitea
url = "https://gitlab.exemple.fr"  # l'adresse de la forge
token = "${PR_TOKEN}"              # le jeton d'un compte de service
projects = ["equipe/*"]            # les projets permis ; aucun sinon
blocked_paths = ["deploy/*"]       # refusés, en plus des chemins de CI
allow_private = false              # true : forge sur le réseau interne
```

Sans `forge`, `url`, `token` ou `projects`, ou avec `projects` ou
`blocked_paths` qui ne sont pas des listes, l'outil n'est pas en service.

- **Qui** : le compte de service ouvre toutes les PR. L'outil est offert
  au chat de la page et aux webhooks (`web`), donc à tout compte qui a
  la page et à tout webhook entrant qui le coche. Chaque PR est dite au
  journal (projet, branche, compte) et son texte finit par le condensé
  court du compte.
- **Quoi** : le modèle donne le projet, la branche de départ (la branche
  par défaut sinon), un titre, une description et les **fichiers
  entiers** à écrire ou à supprimer. 20 fichiers et 1 Mo au plus.
- **Refusé avant tout appel** : un projet hors de `projects` (casse
  ignorée) ou mal formé (`..`), un chemin qui remonte ou touche `.git`,
  les chemins de CI usuels (`.gitlab-ci.yml`, `.github/workflows/*`,
  `Jenkinsfile`…) et ceux de `blocked_paths`. Sur GitHub, un fichier garde
  son mode (un script reste exécutable) ; un lien ou un sous-module n'est
  pas réécrit.
- **Bornes** : une PR par réponse, 5 par compte et 20 pour tout le proxy
  par heure glissante.
- **La branche** : `llm-proxy/<titre réduit>-<condensé>`. La même
  modification retombe sur la même branche, et elle est refusée. Un échec
  après la création de la branche la supprime.

**Sa sûreté** tient à `projects` (le plus étroit possible), à la branche
par défaut protégée sur la forge et à la relecture de la PR, jamais
fusionnée par l'outil. Le jeton sert à tout compte qui a la page et au
texte de tout webhook qui coche l'outil. Une branche poussée peut lancer
d'autres fichiers que la CI (scripts, `Makefile`) : à ajouter dans
`blocked_paths` si la CI les exécute.
