"""
Un SERVEUR MCP de la configuration : sa session (Streamable HTTP), la
découverte de ses outils, leurs appels, et sa veille (rafraîchissement,
reprise après panne).
"""

import asyncio
import fnmatch
import itertools
import json
import random
import time
from urllib.parse import urlsplit

import httpx2

from ...core import config
from ...core.settings import log

from .protocol import (
    CLIENT,
    HEADER_MISMATCH,
    INVALID_PARAMS,
    KIND,
    LEGACY,
    MAX_BYTES,
    MAX_PAGES,
    MAX_TOOLS,
    MIN_GAP,
    MISSING_CAPABILITY,
    MODERN,
    REFRESH,
    RESERVED,
    TIMEOUT,
    UNSUPPORTED_VERSION,
    USER_AGENT,
    definition,
    Failure,
    function_name,
    header_value,
    _META,
    _NAME,
    _clean,
)
from .tool import McpTool


def _setting(table: dict, key: str, default, cast, label: str, name: str):
    value = table.get(key, default)
    try:
        return cast(value)
    except (TypeError, ValueError):
        raise SystemExit(f"{config.CONFIG_PATH} : tools.mcp.{name}.{key} "
                         f"doit être {label} (reçu {value!r})")


def _names(table: dict, key: str, name: str) -> list[str]:
    value = table.get(key, [])
    if isinstance(value, str):
        value = [p.strip() for p in value.split(",")]
    if not isinstance(value, list):
        raise SystemExit(f"{config.CONFIG_PATH} : tools.mcp.{name}.{key} "
                         f"doit être une liste de chaînes")
    return [str(v) for v in value if str(v).strip()]


class Server:
    """Un serveur MCP de la configuration : ses réglages, la connexion
    (ère, version, session), et les outils qu'il a apportés au registre."""

    def __init__(self, name: str, table: dict, transport=None):
        where = f"{config.CONFIG_PATH} : [tools.mcp.{name}]"
        if not _NAME.fullmatch(name):
            raise SystemExit(f"{where} : nom de serveur invalide (lettres, "
                             f"chiffres, «_», «-» ; 32 caractères au plus)")
        self.name = name
        self.enabled = bool(table.get("enabled", True))
        self.url = str(table.get("url") or "")
        try:
            parts = urlsplit(self.url)
            valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        except ValueError:
            parts, valid = None, False
        if not valid:
            raise SystemExit(f"{where} : `url` doit être une URL http(s) "
                             f"(reçu {self.url!r})")
        # Pour le journal : sans la requête, qui peut porter une clé.
        self.where = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}" \
                     f"{parts.path}"
        headers = table.get("headers", {})
        if not isinstance(headers, dict):
            raise SystemExit(f"{where} : `headers` doit être une table")
        # Un secret ${VAR} dont la variable manque rend «» ou «Bearer » :
        # l'en-tête n'est pas envoyé, et le démarrage le signale.
        self.headers, self.missing = {"User-Agent": USER_AGENT}, []
        for key, value in headers.items():
            if key.lower() in RESERVED or key.lower().startswith("mcp-"):
                raise SystemExit(f"{where} : l'en-tête «{key}» est posé par "
                                 f"le proxy, il ne se configure pas")
            value = str(value)
            if not value.strip() or value != value.rstrip():
                self.missing.append(key)
            else:
                self.headers[key] = value
        self.prefix = str(table.get("prefix", name))
        if self.prefix and not _NAME.fullmatch(self.prefix):
            raise SystemExit(f"{where} : `prefix` invalide (lettres, "
                             f"chiffres, «_», «-» ; ou «» pour aucun)")
        # Motifs sur le nom de l'outil CHEZ LE SERVEUR (`get_*`) : `tools`
        # non vide = seuls ceux-là ; `exclude` = jamais ceux-là.
        self.allow = _names(table, "tools", name)
        self.exclude = _names(table, "exclude", name)
        self.timeout = _setting(table, "timeout", TIMEOUT, float,
                                "un nombre", name)
        self.refresh_every = _setting(table, "refresh", REFRESH, float,
                                      "un nombre", name)
        self.max_tools = _setting(table, "max_tools", MAX_TOOLS, int,
                                  "un entier", name)
        self.verify_ssl = bool(table.get("verify_ssl", True))
        self.transport = transport

        self.client: httpx2.AsyncClient | None = None
        self.era: str | None = None       # None : pas encore sondée
        self.version = MODERN
        self.session = ""
        self.tools: dict[str, McpTool] = {}     # par nom chez le serveur
        self.up: bool | None = None       # issue de la dernière découverte
        self.error = ""
        self.ttl = 0.0                    # fraîcheur annoncée (`ttlMs`), en s
        self._ids = itertools.count(1)
        self._said: set = set()
        self._lock = self._stale = self.first = None

    # ── connexion ───────────────────────────────────────────────────────

    def open(self, transport=None) -> None:
        """Ouvre le client HTTP. Dans la boucle asyncio qui servira : les
        verrous lui sont liés."""
        self._lock, self._stale = asyncio.Lock(), asyncio.Event()
        self.first = asyncio.Event()
        self.era, self.session = None, ""
        # trust_env=False : une adresse du réseau du proxy, un HTTP_PROXY
        # d'environnement n'a pas à s'en mêler. Pas de redirection suivie :
        # les en-têtes d'authentification partiraient vers un autre hôte.
        self.client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(self.timeout,
                                  connect=min(self.timeout, 10)),
            transport=transport or self.transport, follow_redirects=False,
            trust_env=False, verify=self.verify_ssl)

    async def close(self) -> None:
        """Ferme la session (DELETE, ère héritée : le serveur peut la
        refuser par 405) puis le client."""
        if self.client is None:
            return
        if self.era == "legacy" and self.session:
            try:
                await asyncio.wait_for(self.client.delete(
                    self.url, headers={**self.headers, **self._head("", {})}), 2)
            except (httpx2.HTTPError, asyncio.TimeoutError):
                pass
        await self.client.aclose()
        self.client, self.era, self.session = None, None, ""
        for tool in self.tools.values():
            tool.live = False

    def changed(self) -> None:
        """La liste a peut-être changé : avance la prochaine découverte."""
        if self._stale is not None:
            self._stale.set()

    # ── un message ──────────────────────────────────────────────────────

    def _frame(self, method: str, params: dict | None = None) -> dict:
        """La requête JSON-RPC. Ère moderne : la version, le client et
        ses capacités (aucune) voyagent dans chaque requête."""
        params = dict(params or {})
        if self.era != "legacy":
            params["_meta"] = {
                _META + "protocolVersion": self.version,
                _META + "clientInfo": CLIENT,
                _META + "clientCapabilities": {},
            }
        message = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params:
            message["params"] = params
        return message

    def _head(self, method: str, params: dict, extra=None) -> dict:
        head = {"MCP-Protocol-Version": self.version}
        if self.era == "legacy":
            if self.session:
                head["Mcp-Session-Id"] = self.session
            return head
        head["Mcp-Method"] = method
        if isinstance(params.get("name"), str):
            head["Mcp-Name"] = header_value(params["name"])
        head.update(extra or {})
        return head

    async def _post(self, message: dict, head: dict):
        """Un POST = un message. Rend (statut HTTP, réponse JSON-RPC ou
        None, en-têtes). La réponse est celle qui porte l'`id` de la
        requête : le corps JSON, ou l'événement du flux SSE qui le clôt."""
        want = message.get("id")
        request = self.client.build_request(
            "POST", self.url, json=message, headers={
                **self.headers,
                "Accept": "application/json, text/event-stream", **head})
        try:
            r = await self.client.send(request, stream=True)
            try:
                kind = r.headers.get("content-type", "") \
                    .split(";")[0].strip().lower()
                if kind == "text/event-stream":
                    reply = await self._stream(r, want)
                else:
                    reply = self._json(await self._body(r))
            finally:
                # Fermer le flux, c'est aussi ANNULER la requête (ère
                # moderne) : un appel abandonné au délai ne court plus.
                await r.aclose()
        except httpx2.TimeoutException:
            raise Failure("timeout", f"the MCP server {self.name} did not "
                                     f"answer in {int(self.timeout)} s.")
        except httpx2.HTTPError as exc:
            raise Failure("unavailable", f"the MCP server {self.name} is "
                          f"unreachable ({type(exc).__name__}).")
        return r.status_code, reply, r.headers

    def _too_large(self) -> Failure:
        return Failure("unavailable", f"the MCP server {self.name} sent a "
                       f"response of more than {MAX_BYTES} bytes.")

    async def _body(self, r) -> bytes:
        body = bytearray()
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise self._too_large()
        return bytes(body)

    @staticmethod
    def _json(raw):
        try:
            message = json.loads(raw)
        except ValueError:
            return None
        return message if isinstance(message, dict) else None

    async def _stream(self, r, want):
        """Le flux SSE d'une requête : des notifications, puis la réponse.
        Lignes `data:` jointes jusqu'à la ligne vide ; commentaires («:»,
        le maintien de connexion) et autres champs ignorés. Un flux fermé
        avant la réponse n'est PAS repris (`Last-Event-ID`, ère héritée) :
        c'est un échec."""
        async def lines():
            async for line in r.aiter_lines():
                yield line
            yield ""                    # un flux fini sans ligne vide

        size, data = 0, []
        async for line in lines():
            size += len(line) + 1
            if size > MAX_BYTES:
                raise self._too_large()
            if line:
                field, _, value = line.partition(":")
                if field == "data":
                    data.append(value.removeprefix(" "))
                continue
            message, data = self._json("\n".join(data)) if data else None, []
            if message is None:
                continue
            if message.get("method") == "notifications/tools/list_changed":
                self.changed()
            if want is not None and message.get("id") == want and (
                    "result" in message or "error" in message):
                return message
        return None

    def _refused(self, status: int) -> Failure:
        """Un statut HTTP sans réponse JSON-RPC lisible."""
        if status in (401, 403):
            # Le détail (en-têtes, OAuth) regarde l'administrateur : il
            # est au journal, pas chez le modèle.
            return Failure("unavailable", f"the MCP server {self.name} "
                           f"refused the credentials of this proxy (HTTP "
                           f"{status}).")
        if status == 429:
            return Failure("too_many_requests", f"the MCP server "
                           f"{self.name} asks to slow down (HTTP 429).")
        if 200 <= status < 300:
            return Failure("unavailable", f"the MCP server {self.name} "
                           f"sent an unreadable response.")
        return Failure("unavailable", f"the MCP server {self.name} "
                                      f"returned HTTP {status}.")

    def _result(self, status: int, reply) -> dict:
        """Le `result` d'une réponse, ou l'échec qu'elle dit. Une erreur
        JSON-RPC est une erreur de PROTOCOLE (outil inconnu, requête mal
        formée, panne du serveur) — pas celle d'un outil, qui arrive dans
        un résultat (`isError`)."""
        if reply is not None and isinstance(reply.get("result"), dict):
            return reply["result"]
        error = reply.get("error") if reply is not None else None
        if not isinstance(error, dict):
            raise self._refused(status)
        code = error.get("code")
        if code == UNSUPPORTED_VERSION:
            self.era = None             # le serveur a changé : re-sonder
        raise Failure(
            "invalid_input" if code == INVALID_PARAMS else "unavailable",
            f"the MCP server {self.name} answered an error ({code}): "
            f"{_clean(error.get('message'), 300) or 'no message'}",
            code if isinstance(code, int) else None)

    # ── ère, session ────────────────────────────────────────────────────

    async def _connect(self, expired: str | None = None) -> None:
        """Sonde l'ère du serveur au premier échange ; `expired` : la
        session qu'un 404 vient de dire close — rouverte, sauf si un
        appel concurrent l'a déjà fait."""
        async with self._lock:
            if expired is not None:
                if self.session == expired:
                    self.session = ""
                    await self._initialize()
            elif self.era is None:
                await self._detect()

    async def _detect(self) -> None:
        """Une requête moderne d'abord (`server/discover`, que tout
        serveur moderne implémente). Il y répond : moderne. Il rend une
        erreur que seul un serveur moderne connaît : moderne aussi, la
        requête est à corriger. Tout autre refus : serveur hérité, on
        ouvre par `initialize`."""
        self.era, self.version, self.session = None, MODERN, ""
        status, reply, _ = await self._post(
            self._frame("server/discover"), self._head("server/discover", {}))
        result = reply.get("result") if reply else None
        error = reply.get("error") if reply else None
        code = error.get("code") if isinstance(error, dict) else None
        offered = None
        if isinstance(result, dict) and "resultType" in result:
            offered = result.get("supportedVersions")
            if not isinstance(offered, list) or MODERN in offered:
                self.era = "modern"
                return
        elif code == UNSUPPORTED_VERSION:
            data = error.get("data")
            offered = data.get("supported") if isinstance(data, dict) else None
        elif code in (HEADER_MISMATCH, MISSING_CAPABILITY):
            self._result(status, reply)
        elif status in (401, 403, 429) or status >= 500:
            raise self._refused(status)
        if isinstance(offered, list) and not set(offered) & set(LEGACY):
            raise Failure("unavailable", (
                f"the MCP server {self.name} speaks none of the protocol "
                f"versions of this proxy (it offers "
                f"{_clean(', '.join(map(str, offered)), 120)})."))
        await self._initialize()

    async def _initialize(self) -> None:
        """Ère héritée : `initialize` (sans session ni en-tête de
        version), la version retenue par le serveur, sa session s'il en
        donne une, puis `notifications/initialized`."""
        self.session = ""
        message = {"jsonrpc": "2.0", "id": next(self._ids),
                   "method": "initialize", "params": {
                       "protocolVersion": LEGACY[0], "capabilities": {},
                       "clientInfo": CLIENT}}
        status, reply, headers = await self._post(message, {})
        result = self._result(status, reply)
        version = result.get("protocolVersion")
        if version not in LEGACY:
            raise Failure("unavailable", (
                f"the MCP server {self.name} speaks a protocol version "
                f"this proxy does not ({_clean(str(version), 40)})."))
        self.era, self.version = "legacy", version
        self.session = headers.get("mcp-session-id", "")
        status, _, _ = await self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            self._head("", {}))
        if status >= 400:
            raise self._refused(status)

    async def request(self, method: str, params: dict | None = None,
                      extra: dict | None = None) -> dict:
        """Une requête, et son `result`. Lève Failure. Session expirée
        (404 sous un `Mcp-Session-Id`) : une nouvelle est ouverte, et la
        requête rejouée UNE fois — un 404 ne l'a pas exécutée."""
        await self._connect()
        for retry in (True, False):
            session = self.session
            status, reply, _ = await self._post(
                self._frame(method, params),
                self._head(method, params or {}, extra))
            if retry and status == 404 and session and self.era == "legacy":
                log.info("MCP %s : session expirée, rouverte", self.name)
                await self._connect(expired=session)
                continue
            return self._result(status, reply)

    # ── découverte ──────────────────────────────────────────────────────

    async def list_tools(self) -> list[dict]:
        """`tools/list`, toutes pages suivies par leur curseur (opaque).
        La fraîcheur retenue est la plus courte des pages."""
        found, cursor, seen, ttl = [], None, set(), None
        for _ in range(MAX_PAGES):
            result = await self.request(
                "tools/list", {"cursor": cursor} if cursor else None)
            page = result.get("tools")
            if not isinstance(page, list):
                raise Failure("unavailable", f"the MCP server {self.name} "
                                             f"sent an unreadable tool list.")
            found += [t for t in page if isinstance(t, dict)]
            fresh = result.get("ttlMs")
            fresh = fresh / 1000 if isinstance(fresh, (int, float)) \
                and not isinstance(fresh, bool) and fresh > 0 else 0.0
            ttl = fresh if ttl is None else min(ttl, fresh)
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                break
            seen.add(cursor)
        else:
            self._say("pages", "MCP %s : liste d'outils coupée à %d pages",
                      self.name, MAX_PAGES)
        self.ttl = ttl or 0.0
        return found

    def _say(self, key, message: str, *args) -> None:
        """Un avertissement, UNE fois : la découverte repasse à chaque
        période sur les mêmes outils écartés."""
        if key not in self._said:
            self._said.add(key)
            log.warning(message, *args)

    def _wanted(self, remote: str) -> bool:
        match = fnmatch.fnmatchcase
        return (not self.allow or any(match(remote, p) for p in self.allow)) \
            and not any(match(remote, p) for p in self.exclude)

    def publish(self, found: list[dict]) -> None:
        """La liste du serveur → le registre. Un outil nouveau est
        enregistré (à la suite : l'ordre des fonctions déjà présentées ne
        bouge pas), un outil connu reçoit sa définition du jour, un outil
        disparu est désactivé — et retrouve sa place s'il revient."""
        from ... import tools as registry  # à l'usage : pas de cycle

        kept = {}
        for entry in found:
            remote = entry.get("name")
            if not isinstance(remote, str) or not remote or remote in kept \
                    or not self._wanted(remote):
                continue
            if len(kept) >= self.max_tools:
                self._say("max", "MCP %s : plus de %d outils, les suivants "
                          "sont écartés (max_tools, ou `tools` pour choisir)",
                          self.name, self.max_tools)
                break
            try:
                described = definition(entry, self.name, self.era == "modern")
            except ValueError as exc:
                self._say(("def", remote), "MCP %s : outil %r écarté — %s",
                          self.name, remote[:80], exc)
                continue
            tool = self.tools.get(remote)
            if tool is None:
                tool = McpTool(self, remote, function_name(self.prefix, remote))
                try:
                    registry.register(tool, provider=KIND)
                except ValueError:
                    self._say(("name", remote), "MCP %s : outil %r écarté — "
                              "le nom de fonction %s est déjà pris",
                              self.name, remote[:80], tool.name)
                    continue
                self.tools[remote] = tool
            tool.define(*described)
            kept[remote] = tool
        for remote, tool in self.tools.items():
            if tool.live != (remote in kept):
                log.info("MCP %s : outil %s %s", self.name, tool.name,
                         "disponible" if remote in kept else "retiré par le "
                         "serveur")
            tool.live = remote in kept

    async def refresh(self) -> bool:
        """Une découverte. En échec, les outils déjà connus RESTENT
        présentés (une liste périmée vaut mieux qu'un prompt qui change à
        chaque coupure) : leur appel rendra l'erreur du moment."""
        try:
            async with asyncio.timeout(self.timeout):
                found = await self.list_tools()
        except (Failure, TimeoutError) as exc:
            error = getattr(exc, "message", "") or "no answer in time."
            if self.up is not False or error != self.error:
                log.warning("MCP %s (%s) injoignable — %s", self.name,
                            self.where, error)
            # L'ère et la session sont à re-sonder : le serveur a pu être
            # relancé, ou remplacé par une autre version.
            self.up, self.error, self.era, self.session = False, error, None, ""
            return False
        self.publish(found)
        if self.up is not True:
            log.info("MCP %s (%s) : protocole %s (%s), %d outil(s)%s",
                     self.name, self.where, self.version,
                     "sans état" if self.era == "modern" else "à session"
                     if self.session else "sans session",
                     len(self.live()), " — " + ", ".join(
                         t.name for t in self.live()) if self.live() else "")
        self.up, self.error = True, ""
        return True

    def live(self) -> list[McpTool]:
        return [t for t in self.tools.values() if t.live]

    def pause(self, failures: int) -> float | None:
        """Secondes avant la prochaine découverte ; None = pas de période
        (attendre un signal). Après un échec : 10 s, 20 s, 40 s… jusqu'à
        la période. Avec un aléa, pour que les proxys d'un même parc ne
        frappent pas le serveur ensemble."""
        if failures:
            pause = min(5 * 2 ** min(failures, 10),
                        max(self.refresh_every, 60))
        elif self.refresh_every <= 0:
            return None
        else:
            pause = max(self.refresh_every, self.ttl)
        return pause * random.uniform(0.9, 1.1)

    async def watch(self) -> None:
        """La tâche du serveur : découvrir, attendre la période ou un
        signal de changement, recommencer."""
        failures = 0
        while True:
            started = time.monotonic()
            try:
                ok = await self.refresh()
            except Exception:           # la tâche ne meurt jamais
                log.exception("MCP %s : découverte en échec", self.name)
                ok = False
            self.first.set()
            failures = 0 if ok else failures + 1
            try:
                await asyncio.wait_for(self._stale.wait(), self.pause(failures))
            except asyncio.TimeoutError:
                pass
            self._stale.clear()
            # Un signal répété (un modèle qui insiste sur un outil refusé)
            # ne fait pas une découverte par appel.
            await asyncio.sleep(max(0.0, started + MIN_GAP - time.monotonic()))
