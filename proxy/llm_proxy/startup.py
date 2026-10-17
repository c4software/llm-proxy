"""
Le démarrage du proxy (lifespan de l'application, app.py) : ouverture des
statistiques et des clients upstream, partage équitable relu de la
dernière heure, découverte des serveurs MCP, ce que la configuration dit
d'elle-même au journal, rafraîchissement des limites des backends à
quotas ; et l'arrêt, dans l'ordre inverse.
"""

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import accounts
from . import albert
from . import anthropic_api
from . import auth
from . import chat_api
from . import config
from . import fairshare
from . import files
from . import oidc
from . import responses_api
from . import stats
from . import tools
from .auth import account_owner
from .backends import BACKENDS, FALLBACK_BACKEND, close_clients, open_clients
from .catalog import always_shown
from .settings import EXEMPT_SUFFIXES, is_exempt, log


def seed_fair_share() -> int:
    """La dernière heure des statistiques dans les fenêtres du partage
    équitable : chaque ligne au client de sa clé — son compte (clé gérée,
    clé du TOML rattachée, chat de la page), ou la clé elle-même."""
    owners = accounts.owners() if oidc.ENABLED else {}
    pages = {account_owner(uid): uid for uid in accounts.user_ids()} \
        if oidc.ENABLED else {}
    rows = []
    for ts, key_id, backend, endpoint, tokens in stats.recent(
            time.time() - fairshare.WINDOW):
        b = BACKENDS.get(backend)
        if b is None or not b.fair_share or is_exempt(endpoint):
            continue
        uid = owners.get(key_id, pages.get(key_id))
        rows.append((ts, account_owner(uid) if uid is not None else key_id,
                     tokens))
    return fairshare.seed(rows)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("configuration : %s", config.CONFIG_PATH)
    stats.init()
    seeded = await asyncio.to_thread(seed_fair_share)
    log.info("partage équitable : %s par heure glissante par défaut, "
             "réglable par compte ; %d requête(s) de la dernière heure "
             "relue(s)", f"{fairshare.DEFAULT_SHARE} tokens"
             if fairshare.DEFAULT_SHARE else "aucune limite", seeded)
    await open_clients()
    # Serveurs MCP ([tools.mcp.<serveur>]) : découverte de leurs outils,
    # attendue quelques secondes au plus — un serveur éteint ne retient
    # pas le démarrage, ses outils s'enregistrent quand il répond.
    await tools.mcp.start()

    for name, b in BACKENDS.items():
        log.info(
            "backend %s%s → %s | %s | clé : %s",
            name, " (fallback sans modèle)" if b is FALLBACK_BACKEND else "",
            b.url,
            "quotas (limiteur)" if b.quotas
            else "sans quota, contacté à la demande",
            "injectée" if b.api_key else "aucune (Authorization du client "
            "transmis tel quel)",
        )
        log.info(
            "backend %s : %s%s",
            name,
            f"tool_choice={b.tool_choice!r} injecté si absent"
            if b.tool_choice else "aucune injection de tool_choice",
            f" | max_tokens plafonné à {b.max_tokens}" if b.max_tokens else "",
        )
    if auth.PROXY_API_KEYS:
        log.info(
            "auth proxy ACTIVE : %d clé(s) acceptée(s), /healthz exempté",
            len(auth.PROXY_API_KEYS),
        )
    elif not oidc.ENABLED:
        log.info("auth proxy inactive (proxy.api_keys vide) : proxy ouvert")
    if oidc.ENABLED:
        log.info("comptes OIDC ACTIFS (%s) : /ui par session OIDC seulement, "
                 "clés gérées par compte (%s)", oidc.ISSUER, accounts.DB_PATH)
    if anthropic_api.ENABLED:
        log.info(
            "surface Anthropic ACTIVE : POST /v1/messages, "
            "/v1/messages/count_tokens, GET /v1/models (anthropic-version) "
            "| model_map : %s",
            ", ".join(f"{k} → {v}" for k, v in anthropic_api.MODEL_MAP.items())
            or "vide (seuls les noms préfixés passent)",
        )
    else:
        log.info("surface Anthropic inactive ([anthropic].enabled absent "
                 "ou false) : /v1/messages → 404")
    if responses_api.ENABLED:
        log.info("surface Responses ACTIVE : POST /v1/responses, traduit "
                 "vers /v1/chat/completions (Codex CLI)")
    else:
        log.info("surface Responses inactive ([responses].enabled absent "
                 "ou false) : /v1/responses → 404")
    if tools.enabled():
        log.info(
            "outils hébergés ACTIFS (/v1/tools, et selon l'outil "
            "/v1/responses, /v1/messages, /v1/chat/completions) : %s | "
            "%d appels au plus par réponse",
            ", ".join(t.name for t in tools.enabled()), tools.MAX_CALLS,
        )
    else:
        log.info("aucun outil hébergé ([tools.<nom>].enabled absent ou "
                 "false%s) : `web_search` d'un client Responses ou Anthropic "
                 "est ignoré", ", aucun serveur MCP joint"
                 if tools.mcp.SERVERS else "")
    log.info(
        "outils hébergés sur /v1/chat/completions : %s",
        "ACTIFS — une requête qui déclare `{\"type\": \"web_search\"}` "
        "dans `tools` est bouclée par le proxy" if chat_api.ENABLED
        else "inactifs ([chat].hosted_tools absent ou false) : relais brut")
    if chat_api.ALWAYS:
        known = {t.name for t in tools.REGISTRY}
        servers = {f"{chat_api.SERVER}{s.name}" for s in tools.mcp.SERVERS}
        shown = always_shown()
        for name in chat_api.ALWAYS:
            if name in servers:
                # Un serveur éteint ou sans outil pour l'instant : ses
                # outils seront présentés dès leur découverte.
                continue
            if name.startswith(chat_api.SERVER) or name == "mcp":
                log.warning(
                    "[chat].always : «%s» ne désigne aucun serveur de "
                    "[tools.mcp.<serveur>] (forme admise : «mcp:<serveur>», "
                    "serveur par serveur) — ignoré", name)
            elif name not in shown:
                log.warning(
                    "[chat].always : outil «%s» %s — ignoré tant qu'il "
                    "l'est", name,
                    f"désactivé ([tools.{name}].enabled)" if name in known
                    else "inconnu du registre")
        if chat_api.ENABLED:
            log.info(
                "présentés D'OFFICE à toute requête /v1/chat/completions, "
                "sans déclaration ([chat].always) : %s%s",
                ", ".join(shown) or "aucun pour l'instant",
                f" — sauf aux modèles {', '.join(chat_api.ALWAYS_EXCEPT)} "
                f"([chat].always_except)" if chat_api.ALWAYS_EXCEPT else "")
        else:
            log.warning("[chat].always sans effet : [chat].hosted_tools "
                        "absent ou false")
    # Garde-fou de téléchargement ([tools.net]) : ses clés encore écrites
    # sous [tools.web_fetch] sont lues, avec un avertissement.
    if tools.net.LEGACY:
        log.warning(
            "[tools.web_fetch].%s : ces clés sont celles du garde-fou "
            "commun à tous les outils qui téléchargent — lues ici faute de "
            "[tools.net], à déplacer dans [tools.net]",
            ", ".join(tools.net.LEGACY))
    if tools.net.SHADOWED:
        log.warning(
            "[tools.web_fetch].%s IGNORÉ : [tools.net] règle déjà ces clés",
            ", ".join(tools.net.SHADOWED))
    if tools.net.ALLOW_PRIVATE:
        log.warning("[tools.net].allow_private = true : les outils qui "
                    "téléchargent joignent AUSSI les adresses privées")
    # Exécution de code : l'exécuteur n'est PAS sondé au démarrage (le
    # proxy ne dépend pas de lui) ; seule la configuration est dite.
    ce = tools.code_execution
    if ce.ENABLED:
        if not ce.URL or not ce.TOKEN:
            log.warning(
                "code_execution actif mais NON configuré ([tools."
                "code_execution].url et .token) : l'outil répondra au "
                "modèle que l'exécution n'est pas disponible")
        else:
            log.info(
                "code_execution : exécuteur %s (joint à la demande ; "
                "injoignable, l'outil rend une erreur au modèle), %d s par "
                "programme, %d appels par réponse", ce.URL, int(ce.TIMEOUT),
                ce.MAX_CALLS)
        if not files.PUBLIC_URL:
            log.warning(
                "code_execution : [files].public_url est vide — les "
                "fichiers produits ne seront PAS remis au client (aucun "
                "lien ne peut être écrit), et le modèle en est prévenu")
    # Conversion audio de `transcribe` : ffmpeg, s'il est là.
    tr = tools.transcribe
    if tr.ENABLED and len(tr.ACCEPTED) < len(tr.FORMATS):
        if tr.FFMPEG:
            log.info("transcribe : hors %s, l'audio est converti en WAV par "
                     "%s", ", ".join(tr.ACCEPTED), tr.FFMPEG)
        elif tr.CONVERT and "wav" in tr.ACCEPTED:
            log.warning(
                "transcribe : ffmpeg introuvable — l'audio qui n'est pas du "
                "%s sera REFUSÉ (l'image Docker du proxy l'embarque ; hors "
                "conteneur, l'installer ou régler [tools.transcribe].ffmpeg)",
                ", ".join(tr.ACCEPTED))
        else:
            log.info("transcribe : pas de conversion — seul l'audio en %s "
                     "est transcrit", ", ".join(tr.ACCEPTED))
    if albert.ROUTER_MODELS:
        log.info("mapping manuel ROUTER_MODELS actif : %s", albert.ROUTER_MODELS)

    refresh_tasks = []
    for b in BACKENDS.values():
        if not b.quotas:
            continue
        if not b.api_key:
            log.warning(
                "backend %s sans api_key : 401 upstream probables", b.name,
            )
        got_account = await b.quota_state.refresh(b)
        if not got_account:
            log.info(
                "[%s] limites du compte indisponibles — familles "
                "statiques : %s",
                b.name,
                ", ".join(
                    f"{fam}({cfg['rpm']}rpm/{cfg['tpm']}tpm)"
                    for fam, cfg in albert.FAMILY_LIMITS.items()
                ),
            )
        elif albert.LIMITS_REFRESH > 0:
            refresh_tasks.append(
                asyncio.create_task(b.quota_state.refresh_loop(b))
            )
    log.info(
        "générique %d RPM / %d TPM | marge %.0f%% | attente max %ds | "
        "cartouche %.0fs | exemptés : %s",
        albert.GENERIC_RPM, albert.GENERIC_TPM, albert.MARGIN * 100,
        int(albert.MAX_QUEUE_SECONDS), albert.STATUS_INTERVAL,
        ", ".join(EXEMPT_SUFFIXES) or "aucun",
    )

    yield
    for task in refresh_tasks:
        task.cancel()
    await tools.mcp.stop()
    await close_clients()
    stats.close()
