"""
Les outils hébergés sur /v1/chat/completions, pour un client qui ne parle
que cette API : il déclare un outil que le proxy exécute, ou le reçoit
d'office, sans rien porter d'autre (ni extension, ni boucle). Voir
docs/outils-heberges.md.

DÉCLARATION, dans `tools` : la forme de l'API Responses, telle quelle —
`{"type": "web_search"}` (les types que déclarent les outils de tools/).
chat/completions n'a pas de forme standard pour cela ; un backend ne
connaît que `function` et refuse le reste, donc le proxy la reconnaît sans
ambiguïté. `web_search_options` (champ racine d'OpenAI, le seul que son SDK
sait écrire) en est un SYNONYME ; ses réglages sont ignorés. Le RETOUR suit
la convention commune : des annotations `url_citation`.

Ce module ne s'applique qu'à une requête à qui un outil hébergé est
présenté, sur un proxy où [chat].hosted_tools est vrai ; toute autre
requête est relayée octet pour octet (surfaces.chat_completions).

PRÉSENTATION D'OFFICE ([chat].always, `ALWAYS`, `unasked`) : pour une
interface qui ne déclare jamais rien, des outils du registre présentés à
toute requête, comme si elle les avait déclarés.
  * Par NOM d'outil, à la suite des outils du client ; `mcp:<serveur>` vaut
    tous les outils de ce serveur, ceux qu'il annoncera plus tard compris
    (`offered`). Pas de `mcp` nu : ce qui entre partout se nomme.
  * Un nom inconnu ou désactivé est ignoré, pas refusé : le client n'a rien
    demandé. Relu à chaque requête contre les outils actifs.
  * Rien d'office à `tool_choice: "none"`, à `n` > 1, ni à un modèle que
    le catalogue ne dit pas de conversation (catalog._converses) : relais
    brut.
  * Les autres `tool_choice` du client restent intacts (les outils d'office
    sont présentés quand même : le préfixe ne change pas d'une requête à
    l'autre).
  * Une requête de service (titre, tags) ne se distingue pas à son corps :
    seul son MODÈLE, s'il est dans [chat].always_except (`excepted`), la
    prive de la présentation d'office ; ce qu'on lui déclare reste honoré.

Le client reçoit UNE réponse chat/completions ordinaire, quel que soit le
nombre de tours upstream (`Translator`). Les appels hébergés ne lui
arrivent JAMAIS en `tool_calls` — il tenterait de les exécuter. Les
fichiers produits par un outil sont rangés au magasin (files.py) et leurs
liens ajoutés par le proxy à la fin du contenu.

MÉMOIRE DES ÉCHANGES CACHÉS (`Memory`, `restore`, [chat].memory). Le
client ne renvoie que ses messages et les réponses finales : sans rien de
plus, le modèle perdrait ce qu'il avait lu et le cache de préfixe du
backend ne servirait plus. Rien n'identifie la conversation : elle est
reconnue À SON CONTENU. Une réponse conclue après au moins un tour d'outils
hébergés range l'échange caché (messages assistant à `tool_calls` et
messages `tool`, tels qu'envoyés au backend) sous un condensé des messages
de la requête et de la réponse finale. À la requête suivante, l'échange
est réinséré avant chaque message assistant dont le condensé est connu, et
ce message reprend le texte du DERNIER tour : le backend reçoit à l'octet
près ce qu'il a vu pendant la boucle.
  * Le condensé (`_canon`) ne retient par message que le rôle, le texte
    (chaîne ou parties `text`, espaces de bord retirés), les identifiants
    de `tool_calls` et le `tool_call_id`. Les messages `system` /
    `developer` n'y entrent pas : bien des clients y écrivent l'heure.
  * Dans le doute, rien n'est réinséré — jamais pire que sans mémoire :
    réponse modifiée, historique tronqué, entrée expirée, redémarrage… Un
    condensé ne désigne que l'endroit exact de l'échange.
  * Une réponse que la limite dure a close ne range rien : ses derniers
    résultats n'ont jamais été lus.
  * L'entrée porte la SESSION des outils (tools.Call.session : le bac
    d'exécution de code) : c'est l'identifiant de conversation, repris du
    dernier échange reconnu, sinon neuf. Il ne sort jamais du proxy.
  * Le condensé porte la réponse telle que le client l'a reçue, liens des
    fichiers compris ; le texte rejoué au backend est sans eux.
  * Cloisonnée par client comme tools.Memory, en mémoire vive, bornée en
    entrées, en durée ET en caractères. Rien n'en est journalisé que des
    comptes.
  * Seule une requête à qui un outil hébergé est présenté est relue ainsi ;
    les entrées d'un client qui cesse de déclarer restent et resservent.

TOUR MIXTE (un outil hébergé et un outil du client dans le même tour) :
exécuter puis rendre la main perdrait le résultat, que le client ne peut
pas rejouer. Le PREMIER appel du tour décide :
  * hébergé d'abord : les appels hébergés sont exécutés, ceux du client ne
    lui sont pas transmis, le backend est relancé et les réémettra ;
  * client d'abord : ses appels sont partis au fil de l'eau (les retenir
    priverait le client du flux) ; les appels hébergés qui suivent ne sont
    pas exécutés, le modèle les redemandera.
Les historiques du client et du backend restent cohérents.

Même contrat de robinet que responses_api.Translator et
anthropic_api.Translator : tool_loop.hosted_loop mène la boucle. Ce module
ne connaît ni FastAPI ni httpx2.
"""

import fnmatch
import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict

from . import config, files
from .settings import log
from .tools.contract import ToolName

# Inactif par défaut : un backend peut avoir SA lecture de
# `{"type": "web_search"}` dans `tools` (Zhipu en a une).
ENABLED = config.flag("chat.hosted_tools", False)
# Annotations `url_citation` en fin de réponse : les URL que les outils
# ont rendues ET que le modèle a écrites dans sa réponse.
ANNOTATIONS = config.flag("chat.annotations", True)
# La mémoire des échanges cachés (tête de module).
MEMORY_ENABLED = config.flag("chat.memory", True)
# Les outils présentés D'OFFICE (tête de module) : des NOMS seulement, résolus
# dans l'annuaire de chaque requête — le registre peut encore grandir.
ALWAYS = list(dict.fromkeys(config.strings("chat.always")))
SERVER = "mcp:"
# Motifs (jokers shell, casse ignorée) sur «<backend>/<modèle>».
ALWAYS_EXCEPT = [p.lower() for p in config.strings("chat.always_except")]
# Bornes en entrées et en durée de la mémoire des résultats ([tools]), plus
# une borne en CARACTÈRES : une entrée d'ici peut porter 8 résultats.
MEMORY_ENTRIES = config.integer("tools.cache_entries", 512)
MEMORY_TTL = config.num("tools.cache_ttl", 24 * 3600)
MEMORY_CHARS = config.integer("chat.memory_chars", 8_000_000)
CHARS_PER_TOKEN = 4
# Entre les textes de deux tours, que le client reçoit comme UN message.
GAP = "\n\n"
# Début du résultat d'un outil annoncé au chat de la page (Translator.start).
PREVIEW_CHARS = 600


class Refused(Exception):
    """Déclaration qu'on ne sait pas honorer : 400, avec ce message."""


class Context:
    """Ce que la réponse doit savoir de la requête."""

    def __init__(self):
        # nom → outil de tools/
        self.hosted: dict = {}
        # Le client a-t-il demandé `stream_options.include_usage` ? Le
        # proxy, lui, le demande toujours au backend (stats exactes).
        self.include_usage = False
        self.annotations = ANNOTATIONS
        # Posés par restore() : où ranger, pour quel client (tools.owner),
        # et le condensé des messages de la requête. `state` None = rien ne
        # sera rangé.
        self.memory: Memory | None = None
        self.client = ""
        self.state = None
        # Identifiant de conversation (tools.Call.session) ; restore() le
        # remplace par celui du dernier échange reconnu.
        self.session = uuid.uuid4().hex
        # Annoncer l'exécution des outils hébergés dans le flux (le chat
        # du tableau de bord, ui_routes.ui_chat) : voir Translator.start.
        self.progress = False


def declares(payload: dict, kinds) -> bool:
    """La requête déclare-t-elle un outil hébergé ? `kinds` : tous les
    types que le paquet tools/ connaît, actifs ou non."""
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
    """Présenter à cette requête les outils de [chat].always ? Non si
    `tool_choice` vaut `none` ou si `n` > 1. Le MODÈLE est l'affaire de la
    route."""
    n = payload.get("n")
    return bool(ALWAYS) and payload.get("tool_choice") != "none" \
        and not (isinstance(n, int) and n > 1)


def offered(active) -> list[str]:
    """Les NOMS des outils que [chat].always présente à l'instant, parmi
    `active`, dans l'ordre de la liste. `mcp:<serveur>` désigne les outils
    dont c'est un type de déclaration (Tool.kinds)."""
    names: dict[str, None] = {}
    for entry in ALWAYS:
        for tool in active:
            if (entry in tool.kinds) if entry.startswith(SERVER) \
                    else tool.name == entry:
                names[tool.name] = None
    return list(names)


def excepted(model: str) -> bool:
    """Ce modèle (nom préfixé `<backend>/<modèle>`) est-il de ceux à qui
    rien n'est présenté d'office ([chat].always_except) ?"""
    model = model.lower()
    return any(fnmatch.fnmatchcase(model, p) for p in ALWAYS_EXCEPT)


def prepare(payload: dict, hosted, kinds, always=()) -> Context:
    """Remplace, DANS `payload`, chaque déclaration d'outil hébergé par
    les fonctions du paquet tools/ et rend le contexte de la réponse.
    `hosted` : l'annuaire tools.Hosted (vide si aucun outil n'est actif).
    `always` : les noms des outils à présenter d'office (offered), à la
    suite de ceux du client ; ceux que l'annuaire n'a pas, ou dont le nom
    est pris, sont passés.

    Un outil déclaré que le proxy connaît mais n'a pas activé est REFUSÉ
    (400) : le retirer en silence ferait répondre le modèle sans recherche,
    le laisser passer rendrait une erreur de backend muette sur la cause.
    (La surface Responses l'ignore : Codex le déclare d'office.) Un type
    inconnu du paquet est laissé au backend.

    `tool_choice` à la forme Responses : traduit vers la première fonction
    présentée pour ce type, au premier tour seulement (tool_loop.hosted_loop
    ramène un choix forcé à `auto` ensuite). S'il vise un outil désactivé
    ou non déclaré : 400."""
    if isinstance(payload.get("n"), int) and payload["n"] > 1:
        raise Refused("`n` > 1 n'est pas pris en charge avec un outil "
                      "hébergé : une réponse, une boucle")
    ctx = Context()
    tools = payload.get("tools")
    tools = list(tools) if isinstance(tools, list) else []
    if "web_search_options" in payload:
        del payload["web_search_options"]
        if not any(isinstance(t, dict) and ToolName.WEB_SEARCH == t.get("type")
                   for t in tools):
            tools.append({"type": ToolName.WEB_SEARCH})
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
                f"proxy ([tools.<nom>].enabled dans config.toml ; pour un "
                f"serveur MCP : désactivé, ou pas encore joint)")
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
    """Les échanges cachés, par client et condensé (tête de module) : la
    LRU de tools.Memory plus une borne en caractères, toutes entrées
    confondues. Un échange plus gros que la borne n'est pas rangé."""

    def __init__(self, entries: int, ttl: float, chars: int):
        self.entries, self.ttl, self.chars = entries, ttl, chars
        self.size = 0       # caractères gardés
        self._data: OrderedDict[tuple[str, str], tuple[float, int, dict]] = \
            OrderedDict()

    def store(self, key: str, messages: list[dict], tail: str,
              owner: str = "", session: str = "") -> None:
        """`messages` : l'échange, tel qu'envoyé au backend ; `tail` : le
        texte du dernier tour, celui que le backend a répondu ; `session` :
        l'identifiant de conversation sous lequel les outils ont tourné."""
        size = len(tail) + sum(_weight(m) for m in messages)
        self._drop((owner, key))
        if size > self.chars:
            return
        self._data[(owner, key)] = (time.monotonic(), size, {
            "messages": messages, "tail": tail, "session": session})
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
    """Réinsère, DANS `payload`, les échanges cachés reconnus (tête de
    module) et pose sur `ctx` de quoi ranger celui de cette réponse. Rend
    le nombre d'échanges réinsérés. `client` : tools.owner() de la clé,
    «» pour un proxy ouvert."""
    messages = payload.get("messages")
    if not MEMORY_ENABLED or not isinstance(messages, list):
        return 0
    state = hashlib.blake2b(digest_size=16)
    out: list = []
    found = 0
    session = ""
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
                # Le DERNIER échange reconnu dit la session : c'est le
                # plus proche de la suite que le client demande.
                session = entry.get("session") or session
                # Le texte du dernier tour, pas celui de tous les tours
                # que le client a reçu ; ses autres champs sont les siens.
                sent = {**msg, "content": entry["tail"] or None}
        out.append(sent)
        if role not in ("system", "developer"):
            block = _canon(msg)
            state.update(b"%d:" % len(block) + block)
    payload["messages"] = out
    ctx.memory, ctx.client, ctx.state = MEMORY, client, state
    if session:
        ctx.session = session
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
    """Le robinet de réponse de /v1/chat/completions avec outils hébergés :
    même interface que responses_api.Translator, menée par
    tool_loop.hosted_loop.

    UNE réponse pour PLUSIEURS tours upstream. En flux :
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
        # Statut HTTP de l'issue, pour la route en JSON.
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
        # Liens des fichiers rendus par les outils, écrits à la fin.
        self._links: list[str] = []
        # JSON : dernier corps upstream, appels client et raisonnement.
        self._doc: dict = {}
        self._client: list[dict] = []
        self._reasoning: dict[str, list[str]] = {}

    # ── interface robinet ──
    def start(self, call: dict) -> bytes:
        """L'appel hébergé que la boucle va exécuter. Rien pour un client
        ordinaire (il n'a pas à savoir qu'un outil tourne) ; avec
        `ctx.progress` — le chat de la page, qui l'affiche —, un bloc
        dont le delta porte `x_hosted_tool` : un champ hors schéma, que
        seule la page demande et lit."""
        return self._progress(call, "running")

    def _progress(self, call: dict, status: str, result=None) -> bytes:
        if not (self.ctx.progress and self.sse) or self._finished:
            return b""
        event = {"id": call["id"], "name": call["name"],
                 "arguments": call["arguments"] or "{}", "status": status}
        if result is not None:
            event["chars"] = len(result.text)
            event["preview"] = result.text[:PREVIEW_CHARS]
            if result.error:
                # Un refus par limite d'appels n'est pas une panne.
                event["status"] = "limit" if result.error == "limit" \
                    else "error"
        return self._block({"x_hosted_tool": event})

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

    @property
    def session(self) -> str:
        """tools.Call.session"""
        return self.ctx.session

    def resolve(self, call: dict, result) -> bytes:
        """Le résultat (tools.Result) d'un appel de `pending` : texte gardé
        pour le tour suivant, sources pour les annotations, fichiers rangés
        au magasin. Le client n'en voit rien, sauf le chat de la page."""
        self.pending = [c for c in self.pending if c is not call]
        self._done.append((call, result.text))
        self._links += [stored.markdown for stored in
                        files.keep(result.files, self.ctx.client)]
        for source in result.sources:
            if source.url:
                self._sources.setdefault(source.url, source.title)
        return self._progress(call, "done", result)

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
        if self._finished:
            return b""
        if self.sse:
            return self._end()
        self._finished = True
        links = self._attachments()
        self._remember()
        doc = {**self._doc, **(self._head or {})}
        choice = dict(doc["choices"][0])
        msg = choice.get("message")
        msg = dict(msg) if isinstance(msg, dict) else {"role": "assistant"}
        if self.turns > 1 or links:
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

    def _attachments(self) -> str:
        """Les liens des fichiers rendus, à ajouter au contenu — une
        fois, à la clôture. Ils entrent dans `_all`, donc dans le condensé
        de la mémoire (le client renverra la réponse AVEC eux) ; pas dans
        `_text` : au rejeu le backend retrouve le texte qu'il a écrit,
        sans eux (`tail`)."""
        if not self._links:
            return ""
        whole = "".join(self._all)
        text = ("" if not whole or whole.endswith("\n\n")
                else "\n" if whole.endswith("\n") else GAP) \
            + GAP.join(self._links)
        self._links = []
        self._all.append(text)
        self.out_chars += len(text)
        return text

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
                         list(self._history), tail, ctx.client, ctx.session)

    def _annotations(self) -> list[dict]:
        """Annotations `url_citation` : une par OCCURRENCE, dans le contenu
        rendu (tous les tours, GAP compris), d'une URL rendue par un outil.
        Une source que le modèle n'a pas écrite n'est pas citée.

        Une URL n'est citée qu'écrite EN ENTIER (`…/llama.cpp` ne doit pas
        matcher au début de `…/llama.cpp/releases`) : les plus longues
        d'abord, une occurrence ne servant qu'une fois, et une occurrence
        qui continue (`_MORE`) est une autre URL."""
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
        # AVANT la mémoire : son condensé porte la réponse telle que le
        # client la reçoit, liens compris.
        links = self._attachments()
        self._remember()
        out = bytearray()
        if links:
            out += self._block({"content": links})
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
