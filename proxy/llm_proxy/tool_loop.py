"""
La boucle des outils hébergés, commune aux trois surfaces (/v1/messages,
/v1/responses, /v1/chat/completions avec [chat].hosted_tools) : le
modèle appelle un outil que le proxy exécute (tools.Hosted.run), le
résultat repart au backend, jusqu'à une réponse sans appel hébergé ou
le plafond d'appels (HOSTED_HARD_LIMIT). Pendant l'attente, des pings
gardent le flux du client ouvert.
"""

import asyncio

from fastapi import Request, Response

from . import albert
from . import anthropic_api
from . import relaying
from . import responses_api
from . import tools
from .backends import strip_backend_prefix
from .settings import log
from .auth import request_owner
from .httputil import parse_json
from .relaying import (
    CLIENT_CLOSED, Call, ERROR_EXCERPT, cap_max_tokens, gate,
    inject_tool_choice,
)


# ── Outils hébergés : la boucle ─────────────────────────────────────────

# Garde-fou de la boucle : au-delà de tools.MAX_CALLS le modèle ne reçoit
# plus que des erreurs qui lui demandent de conclure, et le tour suivant ne
# lui présente plus les outils à bout (_drop_exhausted) — il ne peut que
# répondre. S'il trouvait encore à appeler autant de fois (un outil à
# compte propre), la réponse est close sans lui.
HOSTED_EXTRA_CALLS = 4
HOSTED_HARD_LIMIT = tools.MAX_CALLS + HOSTED_EXTRA_CALLS


def _error_message(response: Response) -> str:
    """Le message d'une réponse d'erreur locale (gate, relaying.send_upstream),
    quelle que soit sa forme : OpenAI ou Anthropic, les deux le portent
    dans `error.message`."""
    return responses_api.error_body(
        parse_json(response.body), response.status_code)["error"]["message"]


def hosted_stream(call: Call, request: Request, payload: dict,
                  images: bool, hosted, robinet, upstream,
                  prompt_estimate: int):
    """La boucle des outils hébergés pour une réponse Responses.

    Chaque tour suivant est RECONSTRUIT par responses_api.to_chat depuis
    l'`input` d'origine suivi des éléments déjà rendus : le chemin même
    par lequel passera le client quand il rejouera ces éléments à sa
    prochaine requête. Le backend voit donc deux fois les mêmes octets —
    préfixe stable pour son cache — et il n'y a qu'une traduction à tenir."""
    base = payload.get("input")
    if isinstance(base, str):
        base = [{"type": "message", "role": "user", "content": base}]
    base = list(base) if isinstance(base, list) else []

    def rebuild(robinet) -> dict:
        return responses_api.to_chat(
            {**payload, "input": base + robinet.output},
            images=images, hosted=hosted, client=robinet.ctx.client)[0]

    # L'API Responses n'a pas d'événement `ping` : un commentaire SSE
    # (ligne «:», ignorée par tout lecteur de flux) tient la connexion
    # pendant une recherche ou l'attente d'un quota.
    return hosted_loop(call, request, hosted, robinet, upstream,
                       prompt_estimate, rebuild,
                       # ping_interval = 0 : pas de ping (sans ce garde
                       # l'attente aurait un délai nul et pingerait en boucle).
                       ping=b": ping\n\n" if robinet.sse
                       and anthropic_api.PING_INTERVAL > 0 else None)


async def _next_upstream(quiet: Call, request: Request, chat_payload: dict,
                         raw: bytes, cost: int):
    """Un tour de plus : la porte de quota, puis l'envoi. Rend la réponse
    upstream ouverte, ou la réponse d'erreur locale (429, 499, 502, 503)."""
    blocked = await gate(quiet, request, chat_payload, cost)
    if blocked is not None:
        return blocked
    return await relaying.send_upstream(quiet, request, "v1/chat/completions", raw)


async def _settled(task: asyncio.Future, ping: bytes | None) -> bool:
    """`task` est-elle finie ? Sans `ping`, n'en revient qu'à la fin ;
    avec, au plus tard après anthropic.ping_interval secondes — à
    l'appelant d'émettre son ping et de redemander."""
    done, _ = await asyncio.wait(
        {task}, timeout=anthropic_api.PING_INTERVAL if ping else None)
    return bool(done)


# Ce que lit le modèle quand ses outils lui sont retirés. Sans elle, un
# budget épuisé par un appel RÉUSSI (aucun refus «Answer now» lu) laisse
# le modèle chercher l'outil disparu : il écrit l'appel en texte
# (`<tool_call>…`), que le backend, sans outils déclarés, ne reconnaît pas.
BUDGET_SPENT = ("[No more tool calls are available for this answer: the "
                "budget is used up. Do not call any tool. Write your final "
                "answer now, with what you already have.]")


def _drop_exhausted(chat_payload: dict, names) -> bool:
    """Retire du corps du tour suivant les fonctions hébergées `names`,
    dont le budget d'appels est épuisé. Sans cela le modèle les voit
    encore, les rappelle, ne reçoit que des refus, et la limite dure
    close la réponse SANS réponse : le petit modèle qui cherche encore
    n'écrit jamais. Plus aucun outil : `tools` et `tool_choice` partent,
    et le tour ne peut qu'être une réponse. Rend vrai si quelque chose
    a été retiré."""
    tools_ = chat_payload.get("tools")
    if not isinstance(tools_, list):
        return False
    names = set(names)
    kept = [t for t in tools_ if not (
        isinstance(t, dict) and isinstance(t.get("function"), dict)
        and t["function"].get("name") in names)]
    if len(kept) == len(tools_):
        return False
    if kept:
        chat_payload["tools"] = kept
        choice = chat_payload.get("tool_choice")
        if isinstance(choice, dict) and (choice.get("function") or {}).get(
                "name") in names:
            chat_payload["tool_choice"] = "auto"
    else:
        for field in ("tools", "tool_choice", "parallel_tool_calls"):
            chat_payload.pop(field, None)
        _tell_budget_spent(chat_payload)
    return True


def _tell_budget_spent(chat_payload: dict) -> None:
    """Plus AUCUN outil au tour suivant : BUDGET_SPENT à la suite du
    DERNIER résultat d'outil du corps, s'il n'est pas déjà un refus — une
    copie du message : celui de l'historique du robinet reste intact (la
    mémoire des échanges cachés le range tel quel). Le début de la
    conversation ne change pas : le cache de préfixe du backend sert."""
    messages = chat_payload.get("messages")
    if not isinstance(messages, list):
        return
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "tool":
            # Déjà un refus par limite («Answer now») : le modèle le sait.
            if isinstance(msg.get("content"), str) \
                    and "is reached. Answer now" not in msg["content"]:
                messages[i] = {**msg, "content":
                               msg["content"] + "\n\n" + BUDGET_SPENT}
            return


async def hosted_loop(call: Call, request: Request, hosted, robinet, upstream,
                      prompt_estimate: int, rebuild,
                      limit: int | dict | None = None,
                      options: dict | None = None, ping: bytes | None = None):
    """Une réponse avec outils hébergés, pour les deux surfaces traduites
    (/v1/responses, /v1/messages) : relaie le tour upstream ouvert,
    exécute les appels que le robinet a mis de côté (`pending`), en rend
    le compte au client, puis relance le backend — jusqu'à un tour sans
    appel hébergé, que le robinet clôt lui-même.

    Ce que la boucle demande à un robinet, et que responses_api.Translator
    et anthropic_api.Translator offrent tous deux : feed / finish,
    `pending` (appels à exécuter : `name`, `arguments`), `client_calls`,
    resolve(appel, résultat — un tools.Result), next_turn(), finalize(), fail(message,
    statut), `turns`, tokens / cached / sse. Ce qui diffère d'une surface
    à l'autre lui est passé :
      * `rebuild(robinet)` : le corps chat/completions du tour suivant,
        reconstruit depuis la requête d'origine et ce que le robinet a
        déjà rendu — par la traduction même que suivra le rejeu du client ;
      * `limit` / `options` : la limite d'appels et les réglages que le
        client a posés sur son outil (tools.Hosted.run). `limit` en
        dict : une limite PAR FONCTION (le `max_uses` de chaque outil
        serveur Anthropic), chacune comptée à part ;
      * `ping` : l'événement à émettre pendant les attentes (exécution
        d'un outil, quota d'un tour suivant), ou None.

    Un tour qui mêle appels hébergés et appels du client s'arrête après
    les premiers : la main revient au client, qui rejouera le tout.

    La réponse HTTP est partie avec le premier tour : une erreur ensuite
    (quota, backend éteint, statut ≥ 400) se dit par robinet.fail(). Les
    tours suivants passent par un `Call` MUET (sans modèle = rien n'est
    compté) : `gate` et `relaying.send_upstream` comptent eux-mêmes leurs échecs,
    or la ligne de stats de cette réponse s'écrit UNE fois, ici, avec
    l'usage cumulé. Un client qui raccroche annule ce générateur : le
    `finally` compte ce qui a été consommé et ferme l'upstream."""
    quiet = Call(call.backend, "", call.endpoint, call.dialect)
    status = upstream.status_code
    limits = limit if isinstance(limit, dict) else None
    budget = sum(limits.values()) if limits is not None else limit
    hard_limit = HOSTED_HARD_LIMIT if budget is None \
        else min(HOSTED_HARD_LIMIT, budget + HOSTED_EXTRA_CALLS)
    used = 0                          # appels comptés au plafond COMMUN
    ran = 0                           # tous les appels exécutés
    spent: dict[str, int] = {}        # appels exécutés, par fonction
    # Le client, pour l'outil (tools.Call) : le condensé de sa clé.
    client = request_owner(request)
    # L'attente en cours (exécution d'un outil, ou porte de quota + envoi
    # du tour suivant) : une tâche, pour pouvoir émettre des pings pendant
    # qu'elle dure, et l'annuler si le client raccroche.
    task: asyncio.Future | None = None
    try:
        while True:
            async for chunk in upstream.aiter_raw():
                out = robinet.feed(chunk)
                if out:
                    yield out
            out = robinet.finish()
            if out:
                yield out
            await upstream.aclose()
            upstream = None
            if not robinet.pending:
                return      # tour sans appel hébergé : le robinet a clos
            handback = robinet.client_calls > 0
            for pending in list(robinet.pending):
                name = pending["name"]
                count, cap = used, limit
                # Un outil qui a son propre compte (Tool.max_calls) : ses
                # appels, contre sa limite — ils ne pèsent pas sur le
                # plafond commun.
                own = hosted.own(name)
                if own:
                    count, cap = spent.get(name, 0), (limits or {}).get(name)
                elif limits is not None:
                    # Limites par fonction : c'est le compte de CETTE
                    # fonction que l'exécution compare à sa limite — tant
                    # que la borne du proxy, commune à toutes, n'est pas
                    # atteinte.
                    count, cap = (used, None) if used >= hosted.cap() \
                        else (spent.get(name, 0), limits.get(name))
                # Un robinet qui annonce l'exécution (le chat de la page).
                begin = getattr(robinet, "start", None)
                out = begin(pending) if begin else b""
                if out:
                    yield out
                task = asyncio.ensure_future(hosted.run(
                    name, pending["arguments"], count, cap,
                    options, endpoint=call.endpoint, model=call.model_key,
                    client=client,
                    # La conversation, pour un outil à état : seul le
                    # robinet de chat/completions en connaît une.
                    session=getattr(robinet, "session", "")))
                while not await _settled(task, ping):
                    yield ping
                result, task = task.result(), None
                ran += 1
                used += not own
                spent[name] = spent.get(name, 0) + 1
                out = robinet.resolve(pending, result)
                if out:
                    yield out
            # Même garde-fou pour un outil à compte propre : sa limite,
            # plus la même marge.
            stuck = used >= hard_limit or any(
                n >= hosted.cap((limits or {}).get(name), name)
                + HOSTED_EXTRA_CALLS
                for name, n in spent.items() if hosted.own(name))
            if handback or stuck:
                if not handback:
                    log.warning(
                        "%s : %d appels d'outils hébergés, le modèle "
                        "ne conclut pas — réponse close (model=%s)",
                        call.endpoint, ran, call.model_key)
                yield robinet.finalize()
                return

            try:
                chat_payload = rebuild(robinet)
            except responses_api.Refused as exc:
                status = 500
                yield robinet.fail(str(exc), status)
                return
            # Les outils dont le budget est épuisé ne sont plus présentés :
            # le modèle a lu « Answer now », il ne peut plus que le faire.
            # Même règle que Hosted.run, outil par outil.
            def exhausted(name: str) -> bool:
                if hosted.own(name):
                    return spent.get(name, 0) >= hosted.cap(
                        (limits or {}).get(name), name)
                if limits is not None:
                    return used >= hosted.cap() or spent.get(name, 0) \
                        >= hosted.cap(limits.get(name))
                return used >= hosted.cap(limit)
            spent_out = [n for n in hosted.by_name if exhausted(n)]
            if spent_out and _drop_exhausted(chat_payload, spent_out):
                log.info("%s : limite d'appels atteinte, tour suivant sans "
                         "%s (model=%s)", call.endpoint, ", ".join(spent_out),
                         call.model_key)
            # Un `tool_choice` forcé par le client (`required`, ou une
            # fonction nommée) vaut pour SA requête : réappliqué à chaque
            # tour, il obligerait le modèle à rappeler un outil sans jamais
            # pouvoir conclure, jusqu'à la limite dure.
            if chat_payload.get("tool_choice") not in (None, "auto", "none"):
                chat_payload["tool_choice"] = "auto"
            inject_tool_choice(chat_payload, call.backend)
            cap_max_tokens(chat_payload, call.backend)
            raw = strip_backend_prefix(chat_payload, call.backend)
            cost = albert.estimate_chat_cost(raw)
            prompt_estimate += cost
            task = asyncio.ensure_future(
                _next_upstream(quiet, request, chat_payload, raw, cost))
            while not await _settled(task, ping):
                yield ping
            blocked, task = task.result(), None
            if not isinstance(blocked, Response):
                upstream, blocked = blocked, None
            if blocked is not None:
                status = blocked.status_code
                if status != CLIENT_CLOSED:
                    yield robinet.fail(_error_message(blocked), status)
                return
            if upstream.status_code >= 400:
                status = upstream.status_code
                body = await upstream.aread()
                log.warning(
                    "[%s] %s → %d upstream au tour %d pour %s : %s",
                    call.backend.name, call.endpoint, status,
                    robinet.turns + 1, call.model_key,
                    body[:ERROR_EXCERPT].decode("utf-8", "replace")
                    .replace("\n", " "),
                )
                doc = parse_json(body)
                yield robinet.fail(responses_api.error_body(
                    doc if doc is not None else body.decode("utf-8", "replace"),
                    status)["error"]["message"], status)
                return
            robinet.next_turn()
    finally:
        if task is not None:
            # Client parti pendant une attente : la recherche s'arrête,
            # l'attente d'un quota le rend (`acquire`). Si la tâche venait
            # de finir, l'upstream qu'elle a ouvert est à fermer.
            if not task.done():
                task.cancel()
            elif not task.cancelled() and task.exception() is None \
                    and hasattr(task.result(), "aclose"):
                upstream = task.result()
        if ran:
            log.info(
                "%s : %d appel(s) d'outil hébergé en %d tour(s) "
                "upstream (model=%s)", call.endpoint, ran, robinet.turns,
                call.model_key)
        if anthropic_api.TRACE and hasattr(robinet, "summary"):
            log.info("[%s] %s → %s", call.backend.name, call.model_key,
                     robinet.summary())
        prompt, completion, exact = robinet.tokens(prompt_estimate)
        call.done(status, prompt, completion, exact, robinet.sse,
                  robinet.cached())
        if upstream is not None:
            await upstream.aclose()
