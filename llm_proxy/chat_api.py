"""
Les outils hébergés sur /v1/chat/completions : ce qu'il faut pour qu'un
client qui ne parle QUE cette API (pi, omp, Hermes, tout SDK OpenAI)
déclare un outil que le proxy exécute, sans rien porter d'autre — ni
extension, ni boucle. Les deux autres surfaces ont ça par leur API
(`{"type": "web_search"}` de Responses, outil serveur d'Anthropic) ;
chat/completions n'a PAS de forme standard pour le dire.

La déclaration retenue, dans `tools` : la forme de l'API Responses, telle
quelle — `{"type": "web_search"}` (tous les types que déclarent les
outils de tools/ : ceux de leur liaison Responses, ou leur nom pour un
outil qui n'en a pas). Un backend chat/completions ne
connaît que `function` dans `tools` et refuse le reste : le proxy peut
donc la reconnaître sans ambiguïté, et ce qu'il remplace n'aurait de
toute façon pas marché. Ce qui existe ailleurs, au 05/10/2026 :
  * OpenAI, chat/completions : `web_search_options` (champ racine), sur
    ses seuls modèles de recherche, et des annotations `url_citation` en
    retour. Accepté ici comme SYNONYME de `{"type": "web_search"}` —
    c'est le seul champ que le SDK OpenAI sait écrire pour cette API ;
    ses réglages (`search_context_size`, `user_location`) sont ignorés,
    et le modèle reste libre de ne pas chercher ;
  * OpenRouter : `plugins: [{"id": "web"}]`, le suffixe `:online`, et un
    outil serveur dans `tools`, `{"type": "openrouter:web_search"}` —
    même idée qu'ici (un type qui n'est pas `function`), sous un nom à
    lui ; retour en annotations `url_citation` ;
  * LiteLLM : relaie `web_search_options` aux modèles qui cherchent
    nativement ; son interception (boucle côté passerelle) vise l'outil
    serveur d'Anthropic, pas chat/completions.
Aucune convention commune pour la DÉCLARATION, donc ; une pour le RETOUR,
les annotations `url_citation`, reprise ici.

Ce module ne s'applique qu'à une requête qui DÉCLARE, sur un proxy où
[chat].hosted_tools est vrai. Toute autre requête ne passe pas par lui :
relais brut des octets, comme avant (app.chat_completions).

PRÉSENTATION D'OFFICE ([chat].always, `ALWAYS`, `unasked`). Une interface
de chat (Open WebUI) ne déclarera jamais rien : la liste nomme des outils
du registre présentés à TOUTE requête, comme si elle les avait déclarés —
même boucle, même réponse, même mémoire. Vide par défaut : rien ne change.
  * Par NOM d'outil (`web_fetch` n'a pas de type à lui), à la suite des
    outils du client. Un outil que la requête déclare aussi n'est
    présenté qu'une fois, à la place de sa déclaration ; une fonction du
    client garde son nom, comme pour une déclaration.
  * Un nom inconnu du registre ou d'un outil désactivé est ignoré, pas
    refusé : le client n'a rien demandé. La liste est relue à chaque
    requête contre les outils actifs à ce moment-là — un outil
    enregistré après le démarrage est présenté dès qu'il existe.
  * Rien n'est présenté d'office à une requête qui dit `tool_choice:
    "none"` (le client ne veut pas d'appel), ni à `n` > 1 (une
    déclaration y est refusée : ici le client n'a rien déclaré, il garde
    ses `n` réponses), ni à un modèle que le catalogue de son backend ne
    dit pas de conversation (app._converses). Une telle requête, et celle
    à qui il ne reste rien à présenter, repart en relais brut.
  * Les autres `tool_choice` sont ceux du client, intacts : une fonction
    forcée est appelée (les outils d'office sont présentés quand même —
    la liste d'outils, donc le préfixe, ne change pas d'une requête à
    l'autre) ; `required` sans outil du client force un outil hébergé au
    premier tour, `auto` ensuite (app.hosted_loop).
  * Le proxy ne sait PAS d'une requête qu'elle est de service (titre,
    tags, suggestions qu'une interface demande après chaque réponse) :
    rien dans son corps ne le dit. Elle reçoit les outils aussi.

Ce que le client reçoit : UNE réponse chat/completions ordinaire, quel
que soit le nombre de tours upstream (voir `Translator`). Les appels
hébergés ne lui arrivent JAMAIS en `tool_calls` — il tenterait de les
exécuter. Conséquence : son historique ne les contient pas. À la requête
suivante il renvoie `[…, user, assistant « réponse », user]` : sans rien
de plus le modèle retrouverait sa réponse, pas ce qu'il avait lu, et le
début de la conversation ne serait plus celui que le backend a vu pendant
la boucle (son cache de préfixe ne servirait que jusqu'au dernier message
d'avant la recherche).

MÉMOIRE DES ÉCHANGES CACHÉS (`Memory`, `restore`, [chat].memory). Rien
dans une requête chat/completions n'identifie la conversation : elle est
reconnue À SON CONTENU. Quand une réponse se conclut après au moins un
tour d'outils hébergés, l'échange caché — les messages assistant à
`tool_calls` et les messages `tool`, tels que le backend les a reçus —
est rangé sous un condensé de ce que le client en reverra : les messages
de SA requête (ceux d'avant, sans rien de réinséré) et la réponse finale.
À la requête suivante, pour chaque message assistant de l'historique, le
condensé est recalculé ; s'il est connu, l'échange est réinséré juste
avant ce message, dont le contenu redevient le texte du DERNIER tour (le
client, lui, a reçu les textes de tous les tours bout à bout : le premier
est déjà dans l'échange réinséré). Le backend reçoit alors, à l'octet
près, ce qu'il a reçu au dernier tour de la boucle, suivi de ce qu'il a
répondu — puis la suite.
  * Ce qui entre dans le condensé (`_canon`) : par message, le rôle, le
    TEXTE (une chaîne et une liste de parties `text` disent la même
    chose ; espaces de début et de fin retirés), les identifiants de ses
    `tool_calls`, son `tool_call_id`. Tout autre champ est ignoré
    (`annotations`, `reasoning_content`, `images`, `name`…) : un client
    qui les retire ou les ajoute retrouve son échange. Les messages
    `system` / `developer` n'y entrent PAS : bien des clients y écrivent
    l'heure, et ce que le modèle a lu ne dépend pas d'eux.
  * Dans le doute, rien n'est réinséré — c'est le comportement d'avant,
    jamais pire : texte de la réponse modifié (résumé, traduit, coupé,
    régénéré : la nouvelle réponse a son propre échange, ou aucun),
    contenu assistant qui porte autre chose que du texte, historique
    tronqué ou compacté (tout ce qui précède change, donc toutes les
    clés), entrée expirée ou sortie de la borne, proxy redémarré, autre
    client. Un condensé ne peut désigner que l'endroit exact où l'échange
    a eu lieu : pas de réinsertion « au mauvais endroit ».
  * Une réponse que la limite dure a close (le modèle n'a pas conclu) ne
    range rien : ses derniers résultats n'ont jamais été lus.
  * Cloisonnée par client comme tools.Memory (le condensé de la clé du
    proxy fait partie de la clé d'entrée), en mémoire vive seulement,
    bornée en entrées, en durée ET en caractères — un échange porte
    jusqu'à 8 résultats de 24 000 caractères. Rien n'en est journalisé
    que des comptes.
  * Seule une requête à qui un outil hébergé est PRÉSENTÉ — déclaré, ou
    d'office — est relue ainsi. Un
    client qui cesse de déclarer en cours de conversation repasse au
    relais brut : rien n'est réinséré (le modèle n'a plus que ses
    réponses), rien n'est perdu non plus — les entrées restent, et
    servent de nouveau s'il redéclare, les clés ne dépendant que de ce
    que LUI envoie.

Tour MIXTE (le modèle appelle dans un même tour un outil hébergé et un
outil du client) : la règle des deux autres surfaces — exécuter, puis
rendre la main — perdrait ici le résultat, que le client ne peut pas
rejouer. Le PREMIER appel du tour décide :
  * hébergé d'abord : les appels hébergés sont exécutés, ceux du client
    de ce tour ne lui sont pas transmis, et le backend est relancé ; le
    modèle les réémettra, résultat de la recherche sous les yeux. Coût :
    leurs arguments sont générés deux fois ;
  * client d'abord : ses appels sont déjà partis vers lui au fil de
    l'eau (les retenir jusqu'à la fin du tour priverait tous les clients
    du flux de leurs arguments, et les laisserait sans un octet le temps
    d'une longue écriture) ; les appels hébergés qui suivent ne sont pas
    exécutés, le modèle les redemandera à la requête suivante. Coût : une
    requête de recherche, quelques tokens.
Dans les deux cas le client et le backend gardent des historiques
cohérents, et rien d'exécuté n'est perdu.

Même contrat de robinet que responses_api.Translator et
anthropic_api.Translator : c'est app.hosted_loop qui mène la boucle.
Ce module ne connaît ni FastAPI ni httpx.
"""

import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict

from . import config
from .settings import log

# Absente du TOML = inactif : un déploiement existant ne change pas de
# comportement sans l'avoir demandé (un backend peut avoir SA lecture de
# `{"type": "web_search"}` dans `tools` — Zhipu en a une).
ENABLED = config.flag("chat.hosted_tools", False)
# Annotations `url_citation` en fin de réponse : les URL que les outils
# ont rendues ET que le modèle a écrites dans sa réponse.
ANNOTATIONS = config.flag("chat.annotations", True)
# La mémoire des échanges cachés (tête de module). false = le comportement
# d'avant : rien n'est gardé, rien n'est réinséré.
MEMORY_ENABLED = config.flag("chat.memory", True)
# Les outils présentés D'OFFICE, par nom, sans doublon (tête de module).
# Des NOMS seulement : ce qu'ils désignent se lit dans l'annuaire de
# chaque requête, pas ici — le registre peut encore grandir.
ALWAYS = list(dict.fromkeys(config.strings("chat.always")))
# Ses bornes en entrées et en durée sont celles de la mémoire des
# résultats ([tools], lues ici sans importer le paquet) ; celle-ci a en
# plus une borne en CARACTÈRES, toutes entrées confondues : une entrée de
# tools.Memory est UN résultat, une entrée d'ici peut en porter 8.
MEMORY_ENTRIES = config.integer("tools.cache_entries", 512)
MEMORY_TTL = config.num("tools.cache_ttl", 24 * 3600)
MEMORY_CHARS = config.integer("chat.memory_chars", 8_000_000)
CHARS_PER_TOKEN = 4
# Entre les textes de deux tours, que le client reçoit comme UN message.
GAP = "\n\n"


class Refused(Exception):
    """Déclaration qu'on ne sait pas honorer : 400, avec ce message."""


class Context:
    """Ce que la réponse doit savoir de la requête."""

    def __init__(self):
        # Fonctions exécutées par le proxy : nom → outil de tools/.
        self.hosted: dict = {}
        # Le client a-t-il demandé `stream_options.include_usage` ? Le
        # proxy, lui, le demande toujours au backend (stats exactes).
        self.include_usage = False
        self.annotations = ANNOTATIONS
        # Mémoire des échanges cachés, posés par restore() : où ranger,
        # pour quel client (tools.owner), et le condensé des messages de
        # la requête tels que le client les a envoyés. `state` None =
        # rien ne sera rangé.
        self.memory: Memory | None = None
        self.client = ""
        self.state = None


def declares(payload: dict, kinds) -> bool:
    """La requête déclare-t-elle un outil hébergé ? `kinds` : tous les
    types que le paquet tools/ connaît, actifs ou non. Le corps est déjà
    désérialisé par la route : un parcours de `tools`, rien de plus."""
    if "web_search_options" in payload:
        return True
    # Un `tool_choice` à la forme Responses (`{"type": "web_search"}`)
    # vise un outil hébergé : aucun backend ne le lirait, c'est à
    # prepare() de le traduire ou de le refuser.
    choice = payload.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") in kinds:
        return True
    tools = payload.get("tools")
    return isinstance(tools, list) and any(
        isinstance(t, dict) and t.get("type") in kinds for t in tools)


def unasked(payload: dict) -> bool:
    """Présenter à cette requête les outils de [chat].always, qu'elle
    n'a pas demandés ? Non si `tool_choice` vaut `none` ou si `n` > 1
    (tête de module). Ce que le proxy sait du MODÈLE est l'affaire de la
    route."""
    n = payload.get("n")
    return bool(ALWAYS) and payload.get("tool_choice") != "none" \
        and not (isinstance(n, int) and n > 1)


def prepare(payload: dict, hosted, kinds, always=()) -> Context:
    """Remplace, DANS `payload`, chaque déclaration d'outil hébergé par
    les fonctions du paquet tools/ et rend le contexte de la réponse.
    `hosted` : l'annuaire tools.Hosted (vide si aucun outil n'est actif).
    `always` : les noms des outils à présenter d'office à cette requête
    (tête de module), à la suite de ceux du client ; ceux que l'annuaire
    n'a pas, ou dont le nom est pris, sont passés. Une requête à qui rien
    n'est ajouté ni remplacé n'est pas touchée.

    Un outil déclaré que le proxy connaît mais n'a pas activé est REFUSÉ
    (400) : le retirer en silence ferait répondre le modèle sans
    recherche à un client qui l'a demandée, et le laisser passer rendrait
    l'erreur d'un backend qui ne dit rien de la cause. (La surface
    Responses, elle, l'ignore : Codex le déclare d'office.) Un type que
    le paquet ne connaît pas n'est pas touché : c'est l'affaire du
    backend.

    `tool_choice` à la forme Responses (`{"type": "web_search"}`) :
    traduit vers la fonction présentée pour ce type — la première,
    `web_search` pour la recherche. Il ne
    vaut que pour le premier tour (app.hosted_loop ramène un choix forcé
    à `auto` ensuite). S'il vise un outil désactivé, ou que la requête ne
    déclare pas (ou dont une fonction du client a pris le nom) : 400 —
    le backend, lui, refuserait sans dire pourquoi."""
    if isinstance(payload.get("n"), int) and payload["n"] > 1:
        raise Refused("`n` > 1 n'est pas pris en charge avec un outil "
                      "hébergé : une réponse, une boucle")
    ctx = Context()
    tools = payload.get("tools")
    tools = list(tools) if isinstance(tools, list) else []
    if "web_search_options" in payload:
        del payload["web_search_options"]
        if not any(isinstance(t, dict) and "web_search" == t.get("type")
                   for t in tools):
            tools.append({"type": "web_search"})
    # Une fonction du client garde son nom : l'outil hébergé homonyme
    # n'est alors pas présenté (même règle que la surface Responses).
    taken = {t["function"].get("name") for t in tools
             if isinstance(t, dict) and isinstance(t.get("function"), dict)}
    out: list = []
    slots: list[int] = []       # places des outils hébergés dans `out`
    for t in tools:
        kind = t.get("type") if isinstance(t, dict) else None
        if kind not in kinds:
            out.append(t)
            continue
        found = hosted.for_kind(kind) if hosted else []
        if not found:
            raise Refused(
                f"outil hébergé «{kind}» déclaré mais désactivé sur ce "
                f"proxy ([tools.<nom>].enabled dans config.toml)")
        for tool in found:
            if tool.name in taken:
                continue
            taken.add(tool.name)
            ctx.hosted[tool.name] = tool
            slots.append(len(out))
            out.append(tool)
    for name in always:
        tool = hosted.by_name.get(name) if hosted else None
        if tool is None or name in taken:
            continue
        taken.add(name)
        ctx.hosted[name] = tool
        slots.append(len(out))
        out.append(tool)
    # Chaque outil sait avec qui il est présenté : une description ne
    # renvoie pas à un outil absent (désactivé, ou nom pris par le client).
    present = frozenset(ctx.hosted)
    for at in slots:
        out[at] = out[at].spec(present)
    if not slots and len(out) == len(tools):
        pass        # rien de remplacé, rien d'ajouté : `tools` reste le sien
    elif out:
        payload["tools"] = out
    else:
        payload.pop("tools", None)
    choice = payload.get("tool_choice")
    kind = choice.get("type") if isinstance(choice, dict) else None
    if kind in kinds:
        found = hosted.for_kind(kind) if hosted else []
        if not found:
            raise Refused(
                f"`tool_choice` vise l'outil hébergé «{kind}», désactivé sur "
                f"ce proxy ([tools.<nom>].enabled dans config.toml)")
        name = next((t.name for t in found if t.name in ctx.hosted), None)
        if name is None:
            raise Refused(
                f"`tool_choice` vise l'outil hébergé «{kind}», que `tools` "
                f"ne déclare pas")
        payload["tool_choice"] = {"type": "function",
                                  "function": {"name": name}}
    if payload.get("stream") and ctx.hosted:
        options = payload.get("stream_options")
        options = dict(options) if isinstance(options, dict) else {}
        ctx.include_usage = bool(options.get("include_usage"))
        # Sans lui un flux ne porte aucun `usage` : la ligne de stats de
        # la réponse, cumulée sur les tours, retomberait sur l'estimation.
        payload["stream_options"] = {**options, "include_usage": True}
    return ctx


# ── mémoire des échanges cachés ─────────────────────────────────────────

class Memory:
    """Les échanges cachés, par client et condensé de contexte (tête de
    module) : la logique de tools.Memory — LRU bornée en entrées, durée,
    `owner` dans la clé — plus une borne en caractères, toutes entrées
    confondues. Un échange plus gros que la borne à lui seul n'est pas
    rangé."""

    def __init__(self, entries: int, ttl: float, chars: int):
        self.entries, self.ttl, self.chars = entries, ttl, chars
        self.size = 0       # caractères gardés
        self._data: OrderedDict[tuple[str, str], tuple[float, int, dict]] = \
            OrderedDict()

    def store(self, key: str, messages: list[dict], tail: str,
              owner: str = "") -> None:
        """`messages` : l'échange, tel qu'envoyé au backend ; `tail` : le
        texte du dernier tour, celui que le backend a répondu."""
        size = len(tail) + sum(_weight(m) for m in messages)
        self._drop((owner, key))
        if size > self.chars:
            return
        self._data[(owner, key)] = (time.monotonic(), size, {
            "messages": messages, "tail": tail})
        self.size += size
        while len(self._data) > self.entries or self.size > self.chars:
            self._drop(next(iter(self._data)))

    def recall(self, key: str, owner: str = "") -> dict | None:
        entry = self._data.get((owner, key))
        if entry is None:
            return None
        if time.monotonic() - entry[0] > self.ttl:
            self._drop((owner, key))
            return None
        self._data.move_to_end((owner, key))
        return entry[2]

    def _drop(self, key) -> None:
        entry = self._data.pop(key, None)
        if entry is not None:
            self.size -= entry[1]

    def __len__(self) -> int:
        return len(self._data)


def _weight(msg: dict) -> int:
    """Caractères d'un message de l'échange : son contenu, les arguments
    de ses appels."""
    return len(msg.get("content") or "") + sum(
        len(tc["function"]["arguments"]) for tc in msg.get("tool_calls", ()))


MEMORY = Memory(MEMORY_ENTRIES, MEMORY_TTL, MEMORY_CHARS)


def _plain(content) -> str | None:
    """Le texte d'un contenu : une chaîne, ou une liste de parties `text`
    (les deux formes de l'API). None = autre chose que du texte."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
            isinstance(p, dict) and p.get("type") == "text"
            and isinstance(p.get("text"), str) for p in content):
        return "".join(p["text"] for p in content)
    return None


def _call_ids(msg: dict) -> list[str]:
    calls = msg.get("tool_calls")
    return [str(tc.get("id") or "") for tc in calls if isinstance(tc, dict)] \
        if isinstance(calls, list) else []


def _bytes(doc) -> bytes:
    return json.dumps(doc, sort_keys=True, ensure_ascii=False,
                      default=str).encode("utf-8", "surrogatepass")


def _canon(msg) -> bytes:
    """Ce qu'un message apporte au condensé (tête de module). Un contenu
    qui n'est pas que du texte (image d'un message user) y entre tel
    quel : seul son condensé est gardé."""
    if isinstance(msg, dict):
        text = _plain(msg.get("content"))
        msg = [msg.get("role"),
               text.strip() if text is not None else msg.get("content"),
               _call_ids(msg), msg.get("tool_call_id")]
    return _bytes(msg)


def _key(state, text: str, ids: list[str]) -> str:
    """La clé d'un échange : `state`, le condensé des messages d'avant,
    prolongé de la réponse — son texte, les identifiants de ses appels
    CLIENT (une réponse régénérée sans texte, aux appels différents, n'est
    pas la même réponse)."""
    h = state.copy()
    h.update(b"=" + _bytes([text.strip(), ids]))
    return h.hexdigest()


def restore(payload: dict, ctx: Context, client: str) -> int:
    """Réinsère, DANS `payload`, les échanges cachés que la mémoire
    reconnaît (tête de module), et pose sur `ctx` de quoi ranger celui de
    cette réponse. Rend le nombre d'échanges réinsérés. Sans effet si
    [chat].memory est faux. `client` : tools.owner() de la clé présentée,
    «» pour un proxy ouvert."""
    messages = payload.get("messages")
    if not MEMORY_ENABLED or not isinstance(messages, list):
        return 0
    state = hashlib.blake2b(digest_size=16)
    out: list = []
    found = 0
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else None
        sent = msg
        if role == "assistant":
            text = _plain(msg.get("content"))
            entry = MEMORY.recall(_key(state, text, _call_ids(msg)), client) \
                if text is not None else None
            if entry is not None:
                found += 1
                out += entry["messages"]
                # Le texte du dernier tour, pas celui de tous les tours
                # que le client a reçu ; ses autres champs sont les siens.
                sent = {**msg, "content": entry["tail"] or None}
        out.append(sent)
        if role not in ("system", "developer"):
            block = _canon(msg)
            state.update(b"%d:" % len(block) + block)
    payload["messages"] = out
    ctx.memory, ctx.client, ctx.state = MEMORY, client, state
    return found


# Ce qui, juste après une URL trouvée dans le texte, dit qu'elle CONTINUE :
# le modèle en a écrit une autre, plus longue. Une ponctuation de fin de
# phrase (ou un `/` final) n'en fait partie que suivie d'un caractère d'URL.
_MORE = re.compile(r"[.,;:!?)\]}'\"*>/]*[\w%~=&#+@-]")


def _id(prefix: str) -> str:
    return f"{prefix}_" + uuid.uuid4().hex[:24]


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _sum(a: dict, b: dict) -> dict:
    """Somme de deux `usage`, champ à champ, sous-objets compris
    (`prompt_tokens_details.cached_tokens`…) : chaque tour relit tout le
    préfixe, et c'est bien ce qui a été consommé."""
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict):
            out[k] = _sum(out[k] if isinstance(out.get(k), dict) else {}, v)
        elif _number(v):
            out[k] = (out[k] if _number(out.get(k)) else 0) + v
        else:
            out.setdefault(k, v)
    return out


def _sse(doc: dict) -> bytes:
    return b"data: " + json.dumps(doc, ensure_ascii=False).encode() + b"\n\n"


DONE = b"data: [DONE]\n\n"


class Translator:
    """Le robinet de réponse de /v1/chat/completions quand un outil
    hébergé est déclaré : même interface que responses_api.Translator
    (feed / finish / tokens / cached / sse / ok, puis `pending`,
    `client_calls`, resolve, next_turn, finalize, fail, `turns`), menée
    par app.hosted_loop. Les octets rendus restent du chat/completions.

    UNE réponse pour PLUSIEURS tours upstream. En flux, le client lit un
    flux continu :
      * l'`id` et le `created` du premier tour sur tous les blocs ;
      * les deltas de contenu (et de raisonnement) des tours successifs à
        la suite, GAP entre deux textes ; `role` une seule fois ;
      * les appels hébergés retirés des deltas — jamais de `tool_calls`
        pour eux ; ceux du client passent, renumérotés (voir le tour
        mixte, en tête de module) ;
      * `finish_reason`, le bloc `usage` et `[DONE]` RETENUS à chaque
        tour, émis une fois à la fin : annotations éventuelles, UN
        `finish_reason` (`stop` quand le dernier tour s'arrête sur des
        appels hébergés que la limite dure a coupés), UN bloc `usage`
        cumulé si le client a demandé `include_usage`, `[DONE]`.
    En JSON, rien ne part avant la fin : le dernier corps upstream, avec
    le contenu de tous les tours, l'usage cumulé, `annotations`.

    Un échec à un tour ultérieur (fail) : en flux, un bloc
    `{"error": …}` puis `[DONE]` — le 200 est parti ; en JSON, le corps
    d'erreur OpenAI, que la route rend avec `status`, le vrai statut."""

    def __init__(self, status: int, content_type: str, ctx: Context):
        ct = (content_type or "").lower()
        self.ok = 200 <= status < 300
        self.sse = self.ok and "text/event-stream" in ct
        self.ctx = ctx
        # Statut HTTP de l'issue, pour la route en JSON : 200, ou celui
        # que fail() a reçu.
        self.status = 200
        self.usage: dict | None = None    # usage upstream du tour en cours
        self._past: list[dict] = []       # usages des tours précédents
        self.out_chars = 0
        # Appels hébergés du tour, à exécuter : {id, name, arguments}.
        self.pending: list[dict] = []
        # Appels CLIENT transmis pendant le tour. Toujours 0 quand
        # `pending` n'est pas vide (le premier appel du tour décide) :
        # la boucle ne rend donc jamais la main sur un tour mixte.
        self.client_calls = 0
        self.turns = 1
        self._buf = bytearray()
        self._finished = False
        self._head: dict | None = None    # id / created du premier tour
        self._tail: dict | None = None    # le bloc de fin retenu, sans choix
        self._finish: str | None = None
        self._lead: str | None = None     # "hosted" | "client"
        # Index upstream d'un appel → son appel hébergé (dict), son
        # nouvel index côté client (int), ou None s'il est écarté.
        self._slots: dict = {}
        self._text: list[str] = []        # texte du tour en cours
        self._all: list[str] = []         # tout le contenu rendu, GAP compris
        self._gap = ""
        self._done: list[tuple[dict, str]] = []   # (appel, résultat) du tour
        self._history: list[dict] = []    # messages des tours clos
        self._ids: list[str] = []         # id des appels client du tour
        self._sources: dict[str, str] = {}        # URL → titre
        # JSON : dernier corps upstream, appels client et raisonnement.
        self._doc: dict = {}
        self._client: list[dict] = []
        self._reasoning: dict[str, list[str]] = {}

    # ── interface robinet ──
    def feed(self, chunk: bytes) -> bytes:
        self._buf += chunk
        if not self.sse:
            return b""
        out = bytearray()
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl]).rstrip(b"\r")
            del self._buf[:nl + 1]
            if line.startswith(b"data:"):
                out += self._data(line[5:].strip())
        return bytes(out)

    def finish(self) -> bytes:
        if self._finished:
            return b""
        if self.sse:
            self._buf.clear()
            self._dropped()
            # Appels hébergés en attente : ce n'est que la fin d'un TOUR.
            return b"" if self.pending else self._end()
        body = bytes(self._buf)
        self._buf.clear()
        try:
            doc = json.loads(body) if body else None
        except ValueError:
            doc = None
        choices = doc.get("choices") if isinstance(doc, dict) else None
        if not isinstance(choices, list) or not choices \
                or not isinstance(choices[0], dict):
            if self.turns > 1:
                return self.fail("réponse upstream illisible", 502)
            # Premier tour, forme inattendue : rendue telle quelle.
            self._finished = True
            return body
        if isinstance(doc.get("usage"), dict):
            self.usage = doc["usage"]
        if self._head is None:
            self._head = {k: doc[k] for k in ("id", "created", "model")
                          if k in doc}
        self._doc = doc
        msg = choices[0].get("message")
        msg = msg if isinstance(msg, dict) else {}
        self._content(msg.get("content"))
        for key in ("reasoning_content", "reasoning"):
            if isinstance(msg.get(key), str) and msg[key]:
                self._reasoning.setdefault(key, []).append(msg[key])
        calls = msg.get("tool_calls")
        self._client = [kept for kept in (
            self._tool({**tc, "index": i}, whole=True)
            for i, tc in enumerate(calls if isinstance(calls, list) else [])
            if isinstance(tc, dict)) if kept is not None]
        self._finish = choices[0].get("finish_reason")
        self._dropped()
        return b"" if self.pending else self.finalize()

    # ── outils hébergés : plusieurs tours upstream pour une réponse ──
    @property
    def history(self) -> list[dict]:
        """Ce que la boucle ajoute aux messages d'origine pour le tour
        suivant : par tour, le message assistant (son texte, ses appels
        HÉBERGÉS — ceux du client écartés d'un tour mixte n'y sont pas :
        le modèle les réémettra) puis un message `tool` par résultat."""
        return self._history + self._turn_messages()

    def _turn_messages(self) -> list[dict]:
        if not self._done:
            return []
        out = [{"role": "assistant", "content": "".join(self._text) or None,
                "tool_calls": [
                    {"id": call["id"], "type": "function", "function": {
                        "name": call["name"],
                        "arguments": call["arguments"] or "{}"}}
                    for call, _ in self._done]}]
        return out + [{"role": "tool", "tool_call_id": call["id"],
                       "content": result} for call, result in self._done]

    def resolve(self, call: dict, result) -> bytes:
        """Le résultat (tools.Result) d'un appel de `pending`, exécuté par
        la boucle : son TEXTE est gardé pour le tour suivant, ses sources
        notées pour les annotations. Le client n'en voit rien."""
        self.pending = [c for c in self.pending if c is not call]
        self._done.append((call, result.text))
        for source in result.sources:
            if source.url:
                self._sources.setdefault(source.url, source.title)
        return b""

    def next_turn(self) -> None:
        """Avant de recevoir le flux upstream suivant : l'état propre au
        tour repart de zéro (les index d'outils recommencent à 0),
        l'identité de la réponse reste."""
        if isinstance(self.usage, dict):
            self._past.append(self.usage)
        self.usage = None
        self._history += self._turn_messages()
        self._done, self._text = [], []
        self._buf.clear()
        self._slots, self._lead = {}, None
        self._finish, self._tail = None, None
        self._client = []
        self._ids = []
        self.client_calls = 0
        if self._all and not self._all[-1].endswith("\n"):
            self._gap = GAP
        self.turns += 1

    def finalize(self) -> bytes:
        """Clôt la réponse. Sans effet si elle l'est déjà."""
        if self._finished:
            return b""
        if self.sse:
            return self._end()
        self._finished = True
        self._remember()
        doc = {**self._doc, **(self._head or {})}
        choice = dict(doc["choices"][0])
        msg = choice.get("message")
        msg = dict(msg) if isinstance(msg, dict) else {"role": "assistant"}
        if self.turns > 1:
            msg["content"] = "".join(self._all) or None
            for key, texts in self._reasoning.items():
                msg[key] = GAP.join(texts)
        if self._client:
            msg["tool_calls"] = self._client
        else:
            msg.pop("tool_calls", None)
        annotations = self._annotations()
        if annotations:
            msg["annotations"] = annotations
        choice["message"] = msg
        choice["finish_reason"] = self._final_reason()
        doc["choices"] = [choice]
        total = self._total()
        if total is not None:
            doc["usage"] = total
        return json.dumps(doc, ensure_ascii=False).encode()

    def fail(self, message: str, status: int = 500) -> bytes:
        """Échec d'un tour ULTÉRIEUR (quota, backend injoignable, statut
        d'erreur), ou erreur dite dans le flux par l'upstream."""
        if self._finished:
            return b""
        self._finished = True
        self.pending = []
        self.status = status
        body = {"error": {"message": message, "type": "upstream_error",
                          "code": status}}
        if self.sse:
            return _sse(body) + DONE
        return json.dumps(body, ensure_ascii=False).encode()

    def _total(self) -> dict | None:
        """L'usage de la réponse : celui de l'upstream s'il n'y a eu
        qu'un tour, la SOMME sinon."""
        turns = self._past + ([self.usage] if isinstance(self.usage, dict) else [])
        total = None
        for u in turns:
            total = u if total is None else _sum(total, u)
        return total

    def cached(self) -> int:
        details = (self._total() or {}).get("prompt_tokens_details")
        if isinstance(details, dict) and _number(details.get("cached_tokens")):
            return max(int(details["cached_tokens"]), 0)
        return 0

    def tokens(self, fallback_prompt: int) -> tuple[int, int, bool]:
        u = self._total() or {}
        p, c = u.get("prompt_tokens"), u.get("completion_tokens")
        if isinstance(p, int) or isinstance(c, int):
            return (p if isinstance(p, int) else fallback_prompt,
                    c if isinstance(c, int) else _est(self.out_chars), True)
        return fallback_prompt, _est(self.out_chars), False

    # ── commun aux deux modes ──
    def _content(self, text) -> str:
        """Un morceau de contenu du tour : noté, et rendu tel que le
        client doit le lire (GAP devant le premier d'un tour suivant)."""
        if not isinstance(text, str) or not text:
            return ""
        self._text.append(text)
        text, self._gap = self._gap + text, ""
        self._all.append(text)
        self.out_chars += len(text)
        return text

    def _tool(self, tc: dict, whole: bool = False) -> dict | None:
        """Un appel (ou un fragment d'appel, en flux) : rendu s'il va au
        client, None s'il est hébergé — rangé dans `pending` — ou écarté.
        Le premier appel du tour décide de qui l'emporte (tête de module)."""
        idx = tc.get("index", 0)
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        if idx not in self._slots:
            name = str(fn.get("name") or "")
            slot = None
            if name in self.ctx.hosted:
                if self._lead != "client":
                    self._lead = "hosted"
                    slot = {"id": str(tc.get("id") or _id("call")),
                            "name": name, "arguments": ""}
                    self.pending.append(slot)
            elif self._lead != "hosted":
                self._lead = "client"
                slot = self.client_calls
                self.client_calls += 1
                self._ids.append(str(tc.get("id") or ""))
            self._slots[idx] = slot
        slot = self._slots[idx]
        if slot is None:
            return None
        if isinstance(slot, dict):
            if isinstance(fn.get("arguments"), str):
                slot["arguments"] += fn["arguments"]
            return None
        if whole:       # JSON : un appel entier n'a pas d'`index`
            return {k: v for k, v in tc.items() if k != "index"}
        tc["index"] = slot
        return tc

    def _dropped(self) -> None:
        dropped = sum(1 for slot in self._slots.values() if slot is None)
        if dropped:
            log.info(
                "chat/completions : tour mixte, %d appel(s) %s écarté(s) — "
                "le modèle les réémettra", dropped,
                "du client" if self._lead == "hosted" else "hébergé(s)")

    def _final_reason(self) -> str:
        # `tool_calls` sans appel pour le client (appels hébergés que la
        # limite dure a coupés) : il attendrait des appels qui n'existent pas.
        if self._finish == "tool_calls" and not self.client_calls:
            return "stop"
        return self._finish or "stop"

    def _remember(self) -> None:
        """Range l'échange caché de cette réponse (tête de module), à sa
        CONCLUSION : au moins un tour d'outils hébergés, puis un tour sans
        eux. Pas après fail(), ni quand la limite dure a clos la réponse
        (`_done` non vide : des résultats que le modèle n'a pas lus), ni
        pour un dernier tour vide (ni texte, ni appel client)."""
        ctx = self.ctx
        tail = "".join(self._text)
        if ctx.memory is None or ctx.state is None or not self._history \
                or self._done or not (tail or self._ids):
            return
        ctx.memory.store(_key(ctx.state, "".join(self._all), self._ids),
                         list(self._history), tail, ctx.client)

    def _annotations(self) -> list[dict]:
        """Annotations `url_citation`, la forme d'OpenAI : une par
        OCCURRENCE, dans le contenu, d'une URL rendue par un outil — où
        elle l'est, en caractères du contenu rendu (tous les tours, GAP
        compris). Une source que le modèle n'a pas écrite n'est pas une
        citation : rien n'est inventé.

        Une URL n'est citée que si elle est écrite EN ENTIER : `text.find`
        seul trouvait aussi `…/llama.cpp` (la page du dépôt, rendue par la
        recherche) au début de `…/llama.cpp/releases`, d'où deux
        annotations au même endroit pour une URL écrite une fois. Les plus
        longues d'abord, une occurrence ne servant qu'une fois ; et une
        occurrence qui continue (`_MORE`) est une autre URL."""
        if not self.ctx.annotations or not self._sources:
            return []
        text = "".join(self._all)
        spans: list[tuple[int, int, str]] = []
        for url in sorted(self._sources, key=len, reverse=True):
            at = text.find(url)
            while at >= 0:
                end = at + len(url)
                if not _MORE.match(text, end) \
                        and not any(s < end and at < e for s, e, _ in spans):
                    spans.append((at, end, url))
                at = text.find(url, end)
        return [{"type": "url_citation", "url_citation": {
            "start_index": at, "end_index": end, "url": url,
            "title": self._sources[url]}} for at, end, url in sorted(spans)]

    # ── flux ──
    def _block(self, delta: dict, finish: str | None = None) -> bytes:
        """Un bloc du proxy lui-même, à l'identité de la réponse."""
        base = self._tail or {"object": "chat.completion.chunk",
                              **(self._head or {"id": _id("chatcmpl")})}
        return _sse({**base, "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish}]})

    def _data(self, payload: bytes) -> bytes:
        if self._finished or payload == b"[DONE]":
            return b""      # la fin se décide dans finish()
        try:
            doc = json.loads(payload)
        except ValueError:
            return b""
        if not isinstance(doc, dict):
            return b""
        if "error" in doc and "choices" not in doc:
            err = doc["error"]
            return self.fail(str(err.get("message") if isinstance(err, dict)
                                 else err) or "erreur upstream")
        if isinstance(doc.get("usage"), dict):
            self.usage = doc["usage"]
        if "usage" in doc:
            # Retenu : un seul bloc `usage`, cumulé, à la fin. OpenAI pose
            # `usage: null` sur les autres blocs quand il est demandé.
            if self.ctx.include_usage:
                doc["usage"] = None
            else:
                del doc["usage"]
        if self._head is None:
            self._head = {k: doc[k] for k in ("id", "created", "model")
                          if k in doc}
        doc.update(self._head)
        choices = doc.get("choices")
        keep = False
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = choice["delta"] = {}
            if self.turns > 1:
                delta.pop("role", None)
            text = self._content(delta.get("content"))
            if text:
                delta["content"] = text
            if isinstance(delta.get("tool_calls"), list):
                calls = [kept for kept in (
                    self._tool(tc) for tc in delta["tool_calls"]
                    if isinstance(tc, dict)) if kept is not None]
                if calls:
                    delta["tool_calls"] = calls
                else:
                    del delta["tool_calls"]
            if choice.get("finish_reason"):
                # Retenu avec son bloc (un backend y joint parfois des
                # mesures, `timings` chez llama.cpp) : il clora la réponse
                # si ce tour est le dernier.
                self._finish = choice["finish_reason"]
                choice["finish_reason"] = None
                self._tail = {k: v for k, v in doc.items() if k != "choices"}
            keep = keep or any(delta.values())
        return _sse(doc) if keep else b""

    def _end(self) -> bytes:
        self._finished = True
        self._remember()
        out = bytearray()
        annotations = self._annotations()
        if annotations:
            out += self._block({"annotations": annotations})
        out += self._block({}, self._final_reason())
        total = self._total()
        if self.ctx.include_usage and total is not None:
            out += _sse({"object": "chat.completion.chunk",
                         **(self._head or {}), "choices": [], "usage": total})
        return bytes(out) + DONE


def _est(chars: int) -> int:
    return max(chars // CHARS_PER_TOKEN, 1) if chars else 0
