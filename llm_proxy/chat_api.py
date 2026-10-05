"""
Les outils hébergés sur /v1/chat/completions : ce qu'il faut pour qu'un
client qui ne parle QUE cette API (pi, omp, Hermes, tout SDK OpenAI)
déclare un outil que le proxy exécute, sans rien porter d'autre — ni
extension, ni boucle. Les deux autres surfaces ont ça par leur API
(`{"type": "web_search"}` de Responses, outil serveur d'Anthropic) ;
chat/completions n'a PAS de forme standard pour le dire.

La déclaration retenue, dans `tools` : la forme de l'API Responses, telle
quelle — `{"type": "web_search"}`, `{"type": "image_generation"}` (tous
les `KINDS` des modules de tools/). Un backend chat/completions ne
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

Ce que le client reçoit : UNE réponse chat/completions ordinaire, quel
que soit le nombre de tours upstream (voir `Translator`). Les appels
hébergés ne lui arrivent JAMAIS en `tool_calls` — il tenterait de les
exécuter. Conséquence, à connaître : son historique ne les contient pas.
À la requête suivante le modèle retrouve sa réponse, pas ce qu'il avait
lu, et le début de la conversation n'est plus celui que le backend a vu
pendant la boucle (son cache de préfixe ne sert que jusqu'au dernier
message d'avant la recherche). Aucune mémoire côté proxy pour ça : rien
dans une requête chat/completions n'identifie la conversation, il
faudrait la reconnaître à son contenu. Un client qui veut garder ce que
le modèle a lu déclare l'outil lui-même et l'exécute par /v1/tools.

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

import json
import uuid

from . import config
from .settings import log

# Absente du TOML = inactif : un déploiement existant ne change pas de
# comportement sans l'avoir demandé (un backend peut avoir SA lecture de
# `{"type": "web_search"}` dans `tools` — Zhipu en a une).
ENABLED = config.flag("chat.hosted_tools", False)
# Annotations `url_citation` en fin de réponse : les URL que les outils
# ont rendues ET que le modèle a écrites dans sa réponse.
ANNOTATIONS = config.flag("chat.annotations", True)
CHARS_PER_TOKEN = 4
# Entre les textes de deux tours, que le client reçoit comme UN message.
GAP = "\n\n"


class Refused(Exception):
    """Déclaration qu'on ne sait pas honorer : 400, avec ce message."""


class Context:
    """Ce que la réponse doit savoir de la requête."""

    def __init__(self):
        # Fonctions exécutées par le proxy : nom → module de tools/.
        self.hosted: dict = {}
        # Par nom de fonction, ce que le client a réglé sur sa déclaration
        # (`size` d'`image_generation`) : passé à l'exécution.
        self.options: dict[str, dict] = {}
        # Le client a-t-il demandé `stream_options.include_usage` ? Le
        # proxy, lui, le demande toujours au backend (stats exactes).
        self.include_usage = False
        self.annotations = ANNOTATIONS


def declares(payload: dict, kinds) -> bool:
    """La requête déclare-t-elle un outil hébergé ? `kinds` : tous les
    types que le paquet tools/ connaît, actifs ou non. Le corps est déjà
    désérialisé par la route : un parcours de `tools`, rien de plus."""
    if "web_search_options" in payload:
        return True
    tools = payload.get("tools")
    return isinstance(tools, list) and any(
        isinstance(t, dict) and t.get("type") in kinds for t in tools)


def prepare(payload: dict, hosted, kinds) -> Context:
    """Remplace, DANS `payload`, chaque déclaration d'outil hébergé par
    les fonctions du paquet tools/ et rend le contexte de la réponse.
    `hosted` : l'annuaire tools.Hosted (vide si aucun outil n'est actif).

    Un outil déclaré que le proxy connaît mais n'a pas activé est REFUSÉ
    (400) : le retirer en silence ferait répondre le modèle sans
    recherche à un client qui l'a demandée, et le laisser passer rendrait
    l'erreur d'un backend qui ne dit rien de la cause. (La surface
    Responses, elle, l'ignore : Codex le déclare d'office.) Un type que
    le paquet ne connaît pas n'est pas touché : c'est l'affaire du
    backend."""
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
    for t in tools:
        kind = t.get("type") if isinstance(t, dict) else None
        if kind not in kinds:
            out.append(t)
            continue
        modules = hosted.for_kind(kind) if hosted else []
        if not modules:
            raise Refused(
                f"outil hébergé «{kind}» déclaré mais désactivé sur ce "
                f"proxy ([tools.<nom>].enabled dans config.toml)")
        modules = [m for m in modules if m.NAME not in taken]
        # web_search renvoie à web_fetch dans sa description : sans lui
        # (désactivé, ou nom pris par le client), la variante qui n'en
        # parle pas.
        fetch = any(m.NAME == "web_fetch" for m in modules)
        for m in modules:
            taken.add(m.NAME)
            ctx.hosted[m.NAME] = m
            if hasattr(m, "options"):
                ctx.options[m.NAME] = m.options(t)
            out.append(m.definition(fetch=fetch) if hasattr(m, "definition")
                       else m.DEFINITION)
    if out:
        payload["tools"] = out
    else:
        payload.pop("tools", None)
    if payload.get("stream") and ctx.hosted:
        options = payload.get("stream_options")
        options = dict(options) if isinstance(options, dict) else {}
        ctx.include_usage = bool(options.get("include_usage"))
        # Sans lui un flux ne porte aucun `usage` : la ligne de stats de
        # la réponse, cumulée sur les tours, retomberait sur l'estimation.
        payload["stream_options"] = {**options, "include_usage": True}
    return ctx


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
        cumulé si le client a demandé `include_usage`, `[DONE]` ;
      * une image générée, dès qu'elle l'est : `delta.images`.
    En JSON, rien ne part avant la fin : le dernier corps upstream, avec
    le contenu de tous les tours, l'usage cumulé, `annotations`, `images`.

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
        self._sources: dict[str, str] = {}        # URL → titre
        self._images: list[dict] = []
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

    def resolve(self, call: dict, result: str) -> bytes:
        """Le résultat d'un appel de `pending`, exécuté par la boucle :
        gardé pour le tour suivant (son TEXTE seul, `str` — pas l'image
        qu'il porterait), ses URL notées pour les annotations. Le client
        n'en voit rien, sauf une image générée."""
        self.pending = [c for c in self.pending if c is not call]
        self._done.append((call, str(result)))
        module = self.ctx.hosted[call["name"]]
        if hasattr(module, "parse"):
            for entry in module.parse(str(result)):
                self._sources.setdefault(entry["url"], entry["title"])
        elif hasattr(module, "action") and not str(result).startswith("Error:"):
            try:
                args = json.loads(call["arguments"] or "{}")
            except ValueError:
                args = None
            url = module.action(args if isinstance(args, dict) else {}).get("url")
            if url:
                self._sources.setdefault(url, url)
        b64 = getattr(result, "b64", None)
        if not b64:
            return b""
        image = {"type": "image_url", "image_url": {
            "url": f"data:image/{getattr(result, 'format', 'png')};base64,{b64}"}}
        self._images.append(image)
        return self._block({"images": [image]}) if self.sse else b""

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
        if self._images:
            msg["images"] = self._images
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

    def _annotations(self) -> list[dict]:
        """Annotations `url_citation`, la forme d'OpenAI : une par URL
        rendue par un outil ET présente dans le contenu — où elle l'est,
        en caractères. Une source que le modèle n'a pas écrite n'est pas
        une citation : rien n'est inventé."""
        if not self.ctx.annotations or not self._sources:
            return []
        text = "".join(self._all)
        found = []
        for url, title in self._sources.items():
            at = text.find(url)
            if at >= 0:
                found.append({"type": "url_citation", "url_citation": {
                    "start_index": at, "end_index": at + len(url),
                    "url": url, "title": title}})
        return sorted(found, key=lambda a: a["url_citation"]["start_index"])

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
