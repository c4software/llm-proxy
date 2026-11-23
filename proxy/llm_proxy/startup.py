"""
Le démarrage du proxy (lifespan de l'application, app.py) : ouverture des
statistiques et des clients upstream, partage équitable relu de la
dernière heure, découverte des serveurs MCP, ce que la configuration dit
d'elle-même au journal, rafraîchissement des limites des backends à
quotas ; et l'arrêt, dans l'ordre inverse.
"""

import asyncio
import fcntl
import os
import signal
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import tools
from .access import accounts, auth, fairshare, oidc
from .access.auth import account_owner
from .core import config, reload
from .core.settings import EXEMPT_SUFFIXES, is_exempt, log
from .storage import files, stats, webhooks
from .surfaces import anthropic_api, chat_api, responses_api
from .upstream import albert
from .upstream.backends import (BACKENDS, FALLBACK_BACKEND, close_clients, open_clients,
                                refresh, served)


def seed_fair_share() -> int:
    """La dernière heure des statistiques dans les fenêtres du partage
    équitable : chaque ligne au client de sa clé — son compte (clé gérée,
    clé du TOML rattachée, chat de la page), ou la clé elle-même."""
    owners = accounts.owners() if oidc.ENABLED else {}
    pages = {account_owner(uid): uid for uid in accounts.user_ids()} \
        if oidc.ENABLED else {}
    table = served()
    rows = []
    for ts, key_id, backend, endpoint, tokens in stats.recent(
            time.time() - fairshare.WINDOW):
        b = table.get(backend)
        if b is None or not b.fair_share or is_exempt(endpoint):
            continue
        uid = owners.get(key_id, pages.get(key_id))
        rows.append((ts, account_owner(uid) if uid is not None else key_id,
                     tokens))
    return fairshare.seed(rows)


# Le verrou du processus sur ses données (voir single_process) : gardé
# ouvert tant qu'il vit.
_HOLD = None


def single_process() -> None:
    """Refuse un deuxième processus sur les mêmes données : tout l'état
    vivant (partage équitable, limiteurs, mémoire des outils, fichiers,
    caches) est en mémoire du processus — deux workers uvicorn (--workers,
    WEB_CONCURRENCY) ou deux proxys sur le même dossier laisseraient
    chacun passer sa part de quota. Un verrou exclusif (flock) à côté de
    stats.db : le second s'arrête au démarrage, message clair."""
    global _HOLD
    if _HOLD is not None:
        return
    path = stats.DB_PATH + ".lock"
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit(
            f"un autre processus du proxy tient déjà {path} : un seul "
            "processus par dossier de données (pas de --workers / "
            "WEB_CONCURRENCY > 1 — l'état des quotas et du partage "
            "équitable est en mémoire)")
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    _HOLD = fh


def on_sighup() -> None:
    """SIGHUP (`docker compose kill -s HUP albert-proxy`) : la
    configuration relue, les réglages rechargeables appliqués à chaud."""
    log.info("SIGHUP : relecture de %s", config.CONFIG_PATH)
    try:
        reload.reload()
    except reload.ReloadError as exc:
        log.error("rechargement refusé, configuration inchangée : %s", exc)


def watch_sighup() -> bool:
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, on_sighup)
        return True
    except (NotImplementedError, RuntimeError, AttributeError):
        return False          # pas de signaux (Windows, boucle hors du fil principal)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("configuration : %s", config.CONFIG_PATH)
    single_process()
    if config.SETUP:
        log.warning("PREMIER DÉMARRAGE : %s absent — API fermée (503), /ui génère "
                    "la configuration ; la poser là puis redémarrer", config.CONFIG_PATH)
    if config.MISSING_VARS:
        log.warning(
            "variables d'environnement absentes ou vides, remplacées par "
            "\"\" dans la configuration : %s (une clé dans .env doit AUSSI "
            "être transmise au conteneur, `environment:` du compose)",
            ", ".join(sorted(config.MISSING_VARS)))
    stats.init()
    # Une fois par base : le prompt d'un webhook entrant que des
    # conversations avaient pris dans leurs réglages en est retiré.
    if tools.webhook.ENABLED:
        if (n := await asyncio.to_thread(webhooks.clear_inherited_prompts)):
            log.info("webhooks : prompt système d'un webhook retiré de %d conversation(s)", n)
        # Une fois aussi : le salon d'un sortant Slack passe dans son URL Apprise.
        if (n := await asyncio.to_thread(webhooks.move_channels_to_urls)):
            log.info("webhooks : salon passé dans l'URL Apprise de %d sortant(s)", n)
    # Avant le partage équitable : un backend partagé y compte.
    await open_clients()
    await refresh(auth.live_owners)
    seeded = await asyncio.to_thread(seed_fair_share)
    log.info("partage équitable : %s par heure glissante par défaut, "
             "réglable par compte ; %d requête(s) de la dernière heure "
             "relue(s)", f"{fairshare.DEFAULT_SHARE} tokens"
             if fairshare.DEFAULT_SHARE else "aucune limite", seeded)
    # Extensions ([extensions].path) : leurs outils entrent au registre
    # avant ceux des serveurs MCP. Au démarrage seulement.
    tools.extensions.load(tools)
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
    if serving := tools.serving():
        log.info(
            "outils hébergés ACTIFS (/v1/tools, et selon l'outil "
            "/v1/responses, /v1/messages, /v1/chat/completions ; le canal "
            "dit si seulement l'API ou la page) : %s | %d appels au plus "
            "par réponse",
            ", ".join(name if len(offers) == len(tools.Offer)
                      else f"{name} ({', '.join(offers)})"
                      for name, offers in serving.items()), tools.MAX_CALLS,
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
        shown = chat_api.always_shown()
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
                    "désactivé ou non offert à l'API ([tools.<nom>].enabled, "
                    "extension désactivée, offerte à la page seule)" if name in known
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
        if albert.LIMITS_REFRESH > 0:
            refresh_tasks.append(asyncio.create_task(
                b.quota_state.refresh_loop(b, loaded=bool(got_account))))
    log.info(
        "générique %d RPM / %d TPM | marge %.0f%% | attente max %ds | "
        "cartouche %.0fs | exemptés : %s",
        albert.GENERIC_RPM, albert.GENERIC_TPM, albert.MARGIN * 100,
        int(albert.MAX_QUEUE_SECONDS), albert.STATUS_INTERVAL,
        ", ".join(EXEMPT_SUFFIXES) or "aucun",
    )

    # La purge des caches en mémoire (pages, transcriptions, dépôts) :
    # une entrée expirée en sort même si personne ne la redemande.
    refresh_tasks.append(asyncio.create_task(tools.webcache.sweep()))

    hup = watch_sighup()
    # La lecture du source des modules (core/reload.py), une fois, hors de
    # la boucle : le premier rechargement n'a plus qu'à évaluer.
    await asyncio.to_thread(reload.prepare)

    yield
    if hup:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGHUP)
    for task in refresh_tasks:
        task.cancel()
    await tools.mcp.stop()
    await close_clients()
    stats.close()
