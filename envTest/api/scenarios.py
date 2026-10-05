#!/usr/bin/env python3
"""Les scénarios de validation « client HTTP nu » → proxy, rejoués pour
chaque modèle de MODELS. PASS/FAIL par scénario, sortie en erreur si l'un
échoue. Bibliothèque standard seule (urllib) : rien à installer.

Ce banc couvre ce qu'aucun client agentique ne fait de lui-même : DÉCLARER
un outil hébergé sur /v1/chat/completions (README principal, « Client
chat/completions : déclarer l'outil »). La requête porte
`{"type": "web_search"}` dans `tools` ; le proxy cherche, relance le backend
et rend UNE réponse chat/completions ordinaire. Ce n'est donc pas un agent :
aucun outil n'est exécuté ici, il n'y a que des requêtes et ce qu'on y lit.
S'y ajoutent l'appel direct (/v1/tools, le chemin de pi et d'omp), la même
déclaration sur /v1/responses hors de Codex, et la preuve CÔTÉ PROXY : le
compteur de l'Usage API des outils (README principal, « Usage des outils
hébergés »), lu avant et après, par route d'appel.

Un scénario est SAUTÉ (SKIP, ni PASS ni FAIL) quand le proxy visé n'a pas
ce qu'il vérifie — lu dans /healthz, exempté de clé : `tools.enabled` sans
`web_search`, `chat.hosted_tools` faux, `responses.enabled` faux — ou, pour
le compteur, quand la route d'usage des outils n'existe pas.
"""

import http.client
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROXY_URL = os.environ.get("PROXY_URL", "").rstrip("/")
KEY = os.environ.get("PROXY_API_KEY") or "unused"
MODELS = os.environ.get("MODELS", "").split()
# Secondes sans un octet avant d'abandonner une requête. Large : une réponse
# JSON avec recherche n'envoie rien avant la fin (plusieurs tours du modèle
# et une recherche) ; en flux, les `: ping` du proxy tiennent la connexion.
TIMEOUT = float(os.environ.get("TIMEOUT") or 600)

# La réponse attendue est une URL que le nom du dépôt détermine : elle ne
# dépend ni de l'actualité ni de la formulation (toute bonne réponse la
# contient : /releases, /releases/latest, lien Markdown). « Recopiée telle
# quelle » : une annotation `url_citation` n'existe que pour une URL rendue
# par l'outil ET écrite à l'identique par le modèle.
NEEDLE = "github.com/ggml-org/llama.cpp"
PROMPT = ("Cherche sur le web la page des releases du dépôt GitHub "
          "ggml-org/llama.cpp. Réponds uniquement par son URL complète, "
          "recopiée telle qu'elle apparaît dans les résultats de la recherche.")
QUERY = "ggml-org llama.cpp releases github"

# L'exemple en deux tours (scénario 8) : une page LONGUE — plus d'un morceau
# de web_fetch (20 000 caractères par défaut) — puis une question dont la
# réponse n'est que dans la page. Par défaut une page de cours du
# propriétaire du dépôt (environ 51 000 caractères le 05/10/2026), dont le
# TP interdit de supprimer une tâche non terminée. Autre page : LONG_URL,
# LONG_NEEDLE (dans le résumé), LONG_QUESTION, LONG_ANSWER (dans la réponse).
LONG_URL = os.environ.get("LONG_URL") or \
    "https://cours.brosseau.ovh/tp/laravel/base_de_donnees.html"
LONG_NEEDLE = os.environ.get("LONG_NEEDLE") or "eloquent"
LONG_QUESTION = os.environ.get("LONG_QUESTION") or \
    "Et quelle règle métier s'applique à la suppression ?"
LONG_ANSWER = os.environ.get("LONG_ANSWER") or "termin"

# Fenêtre du compteur : la MÊME à chaque lecture, pour que deux lectures ne
# diffèrent que par ce qui s'est exécuté entre elles. Large des deux côtés
# (un jour avant, deux jours après) : l'horloge du proxy n'est pas celle
# d'ici, et sans `end_time` le proxy s'arrête à SA seconde courante, qui
# exclut une exécution de cette même seconde.
START = int(time.time()) - 86_400
END = START + 3 * 86_400
ROUTE_CHAT, ROUTE_TOOLS, ROUTE_RESPONSES = \
    "/v1/chat/completions", "/v1/tools", "/v1/responses"

fails = 0
skips = 0


def passed(text):
    print(f"  \033[32mPASS\033[0m {text}", flush=True)
    return True


def failed(text):
    global fails
    fails += 1
    print(f"  \033[31mFAIL\033[0m {text}", flush=True)
    return False


def skipped(text):
    global skips
    skips += 1
    print(f"  \033[33mSKIP\033[0m {text}", flush=True)
    return False


def short(text, n=200):
    return " ".join(str(text).split())[:n]


def http_call(method, path, body=None, timeout=None):
    """→ (statut, type de contenu, corps en texte). Statut 0 : pas de
    réponse HTTP (proxy injoignable, délai dépassé, connexion coupée), le
    « corps » dit alors pourquoi. Un flux SSE est lu en entier puis
    découpé : rien ici ne dépend de l'instant où un bloc arrive."""
    data = None if body is None \
        else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        PROXY_URL + path, data=data, method=method,
        headers={"Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
            return (r.status, r.headers.get("content-type", ""),
                    r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return (e.code, e.headers.get("content-type", ""),
                e.read().decode("utf-8", "replace"))
    except (OSError, ValueError, http.client.HTTPException) as e:
        return 0, "", f"{type(e).__name__} : {e}"


def refused(status, text):
    """Le libellé d'une réponse qui n'est pas un 200."""
    if not status:
        return f"pas de réponse du proxy ({short(text, 300)})"
    return f"HTTP {status} : {short(text, 300)}"


def as_json(text):
    try:
        doc = json.loads(text)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def counts():
    """Les exécutions d'outils que le proxy a comptées dans la fenêtre,
    par (route d'appel, outil) → nombre. None si la route d'usage des
    outils ne répond pas (proxy antérieur au 05/10/2026, clé refusée)."""
    query = urllib.parse.urlencode([
        ("start_time", START), ("end_time", END), ("bucket_width", "all"),
        ("group_by[]", "endpoint"), ("group_by[]", "tool")])
    status, _, text = http_call(
        "GET", "/v1/organization/usage/tools?" + query, timeout=30)
    doc = as_json(text)
    if status != 200 or doc.get("object") != "page":
        return None
    out = {}
    for bucket in doc.get("data") or []:
        for r in bucket.get("results") or []:
            key = (r.get("endpoint"), r.get("tool"))
            out[key] = out.get(key, 0) + int(r.get("num_requests") or 0)
    return out


def delta(before, route, want=1):
    """Exécutions de web_search comptées par le proxy sur `route` depuis
    la lecture `before`. None sans compteur. Le proxy écrit ses lignes
    hors de la requête (un thread, par lots) : on relit jusqu'à 5 s tant
    que `want` n'y est pas."""
    if before is None:
        return None
    key = (route, "web_search")
    got = 0
    for _ in range(11):
        now = counts()
        if now is None:
            return None
        got = now.get(key, 0) - before.get(key, 0)
        if got >= want:
            break
        time.sleep(0.5)
    return got


def counter(route, n):
    return f"compteur {route} : " + ("indisponible" if n is None else f"+{n}")


def citations(annotations, content):
    """Les annotations `url_citation` d'une réponse → (URL citées et bien
    placées, premier défaut ou ""). Une citation est bien placée quand ses
    indices, en caractères, découpent exactement son URL dans le contenu :
    `content[start_index:end_index] == url`. Une annotation d'un autre type
    n'est pas celle du proxy : ignorée."""
    good, bad = [], ""
    for a in annotations:
        if not isinstance(a, dict) or a.get("type") != "url_citation":
            continue
        c = a.get("url_citation")
        c = c if isinstance(c, dict) else {}
        s, e, url = c.get("start_index"), c.get("end_index"), c.get("url")
        if isinstance(s, int) and isinstance(e, int) and isinstance(url, str) \
                and url and content[s:e] == url:
            good.append(url)
        elif not bad:
            at = content[s:e] if isinstance(s, int) and isinstance(e, int) \
                else ""
            bad = (f"annotation mal placée : indices {s}–{e} pour « {url} », "
                   f"le contenu y porte « {short(at, 120)} »")
    return good, bad


def chat_body(model, stream, declare):
    body = {"model": model, "stream": stream,
            "messages": [{"role": "user", "content": PROMPT}]}
    if stream:
        body["stream_options"] = {"include_usage": True}
    if declare:
        body["tools"] = [{"type": "web_search"}]
    return body


def usage_text(usage):
    if not isinstance(usage, dict):
        return "pas de bloc usage"
    return (f"usage {usage.get('prompt_tokens', '?')} + "
            f"{usage.get('completion_tokens', '?')} tokens")


def judge(content, tool_calls, finishes, annotations, seen):
    """Ce que le flux et le JSON ont en commun, dans l'ordre où un défaut
    est le plus parlant. → le défaut, ou "" si tout y est. La PREUVE de la
    recherche, côté client, est l'annotation : le proxy n'en pose que pour
    une URL qu'un outil a rendue."""
    good, bad = citations(annotations, content)
    if tool_calls:
        return (f"{tool_calls} `tool_calls` rendu(s) au client : un appel "
                f"hébergé ne doit jamais lui arriver : {seen}")
    if len(finishes) != 1:
        return f"{len(finishes)} `finish_reason` ({finishes}), 1 attendu : {seen}"
    if not content.strip():
        return f"réponse sans contenu : {seen}"
    if NEEDLE.lower() not in content.lower():
        return f"réponse sans « {NEEDLE} » : {seen}"
    if bad:
        return f"{bad} : {seen}"
    if not good:
        return ("aucune annotation `url_citation` (le modèle n'a pas "
                "cherché, n'a pas recopié une URL des résultats, ou "
                f"[chat].annotations = false sur le proxy) : {seen}")
    return ""


def chat_stream(model):
    """1. Flux : un seul `id`, aucun `tool_calls`, un seul `finish_reason`,
    au plus un bloc `usage`, `[DONE]` une fois et en dernier."""
    before = counts()
    status, ctype, text = http_call(
        "POST", ROUTE_CHAT, chat_body(model, True, True))
    if status != 200:
        return failed(refused(status, text))
    if "text/event-stream" not in ctype.lower():
        return failed(f"réponse qui n'est pas un flux SSE ({ctype}) : "
                      f"{short(text, 300)}")
    datas = [line[5:].strip() for line in text.split("\n")
             if line.startswith("data:")]
    docs = [d for d in (as_json(x) for x in datas if x != "[DONE]") if d]
    choices = [c for d in docs for c in (d.get("choices") or [])
               if isinstance(c, dict)]
    deltas = [c["delta"] for c in choices if isinstance(c.get("delta"), dict)]
    content = "".join(x["content"] for x in deltas
                      if isinstance(x.get("content"), str))
    ids = sorted({str(d["id"]) for d in docs if d.get("id")})
    usages = [d["usage"] for d in docs if isinstance(d.get("usage"), dict)]
    finishes = [c["finish_reason"] for c in choices if c.get("finish_reason")]
    annotations = [a for x in deltas
                   for a in (x.get("annotations") or [])]
    n = delta(before, ROUTE_CHAT)
    good, _ = citations(annotations, content)
    seen = (f"{len(docs)} blocs, {len(good)} citation(s)"
            + (f" ({good[0]})" if good else "")
            + f", {usage_text(usages[-1] if usages else None)}, "
            + f"{counter(ROUTE_CHAT, n)} — {short(content) or 'pas de texte'}")
    errors = [d["error"] for d in docs if "error" in d and "choices" not in d]
    if errors:
        return failed(f"bloc d'erreur dans le flux ({short(errors[0], 200)}) : {seen}")
    if datas.count("[DONE]") != 1 or datas[-1] != "[DONE]":
        return failed(f"`[DONE]` {datas.count('[DONE]')} fois, ou pas en "
                      f"dernier : {seen}")
    if len(ids) != 1:
        return failed(f"{len(ids)} `id` dans le flux ({', '.join(ids[:3])}), "
                      f"1 attendu : {seen}")
    if len(usages) > 1:
        return failed(f"{len(usages)} blocs `usage`, 1 au plus attendu : {seen}")
    ko = judge(content, sum(1 for x in deltas if x.get("tool_calls")),
               finishes, annotations, seen)
    return failed(ko) if ko else passed(seen)


def chat_json(model):
    """2. JSON : la même réponse, d'un bloc."""
    before = counts()
    status, _, text = http_call(
        "POST", ROUTE_CHAT, chat_body(model, False, True))
    if status != 200:
        return failed(refused(status, text))
    doc = as_json(text)
    choices = [c for c in (doc.get("choices") or []) if isinstance(c, dict)]
    msg = choices[0].get("message") if choices else None
    msg = msg if isinstance(msg, dict) else {}
    content = msg["content"] if isinstance(msg.get("content"), str) else ""
    annotations = msg.get("annotations") or []
    n = delta(before, ROUTE_CHAT)
    good, _ = citations(annotations, content)
    seen = (f"{len(good)} citation(s)" + (f" ({good[0]})" if good else "")
            + f", {usage_text(doc.get('usage'))}, {counter(ROUTE_CHAT, n)} — "
            + (short(content) or "pas de texte : " + short(text)))
    if doc.get("error"):
        return failed(f"corps d'erreur ({short(doc['error'], 200)}) : {seen}")
    if len(choices) != 1:
        return failed(f"{len(choices)} choix dans la réponse, 1 attendu : {seen}")
    ko = judge(content, len(msg.get("tool_calls") or []),
               [c["finish_reason"] for c in choices if c.get("finish_reason")],
               annotations, seen)
    return failed(ko) if ko else passed(seen)


def chat_plain(model):
    """3. La même requête sans déclaration : relais ordinaire. Le modèle
    répond de mémoire, juste ou non — ce n'est pas ce qu'on regarde : une
    réponse chat/completions valide, et rien de ce que la boucle du proxy
    y ajoute (pas d'annotation)."""
    status, _, text = http_call(
        "POST", ROUTE_CHAT, chat_body(model, False, False))
    if status != 200:
        return failed(refused(status, text))
    doc = as_json(text)
    choices = [c for c in (doc.get("choices") or []) if isinstance(c, dict)]
    msg = choices[0].get("message") if choices else None
    msg = msg if isinstance(msg, dict) else {}
    content = msg["content"] if isinstance(msg.get("content"), str) else ""
    seen = (f"finish_reason {choices[0].get('finish_reason') if choices else None}"
            f", {usage_text(doc.get('usage'))} — "
            + (short(content) or "pas de texte : " + short(text)))
    if len(choices) != 1 or not content.strip():
        return failed(f"réponse sans contenu : {seen}")
    if not choices[0].get("finish_reason"):
        return failed(f"réponse sans `finish_reason` : {seen}")
    if msg.get("annotations"):
        return failed(f"annotations sur une requête qui n'a rien déclaré : {seen}")
    return passed(seen)


def tools_list():
    """4. GET /v1/tools : web_search, avec de quoi le déclarer à un modèle."""
    status, _, text = http_call("GET", ROUTE_TOOLS, timeout=30)
    if status != 200:
        return failed(refused(status, text))
    data = [t for t in (as_json(text).get("data") or []) if isinstance(t, dict)]
    names = ", ".join(str(t.get("name")) for t in data) or "aucun"
    tool = next((t for t in data if t.get("name") == "web_search"), None)
    if tool is None:
        return failed(f"web_search absent de la liste ({names})")
    params = tool.get("parameters")
    if not (isinstance(tool.get("description"), str) and tool["description"]
            and isinstance(params, dict)
            and "query" in (params.get("properties") or {})):
        return failed(f"web_search sans description ou sans paramètre "
                      f"`query` : {short(tool, 300)}")
    return passed(f"outils : {names} — web_search("
                  f"{', '.join(params['properties'])})")


def tools_run():
    """5. POST /v1/tools/web_search : le corps est les arguments, la réponse
    {"name", "result", "is_error"} ; le résultat porte au moins une URL."""
    before = counts()
    status, _, text = http_call(
        "POST", ROUTE_TOOLS + "/web_search", {"query": QUERY}, timeout=120)
    if status != 200:
        return failed(refused(status, text))
    doc = as_json(text)
    result = doc.get("result")
    n = delta(before, ROUTE_TOOLS)
    seen = (f"{len(result) if isinstance(result, str) else 0} car., "
            f"{counter(ROUTE_TOOLS, n)} — {short(result if isinstance(result, str) else text, 160)}")
    if doc.get("name") != "web_search" or not isinstance(result, str):
        return failed(f"forme inattendue (name, result attendus) : {seen}")
    if doc.get("is_error") is not False:
        return failed(f"l'outil a rendu une erreur (is_error) : {seen}")
    if "http" not in result:
        return failed(f"résultat sans URL : {seen}")
    return passed(seen)


def responses_json(model):
    """6. /v1/responses, la même déclaration, hors de Codex et sans flux :
    un élément `web_search_call` terminé, PUIS un message. Pas de
    `function_call` pour l'appel hébergé."""
    before = counts()
    status, _, text = http_call("POST", ROUTE_RESPONSES, {
        "model": model, "input": PROMPT, "stream": False,
        "tools": [{"type": "web_search"}]})
    if status != 200:
        return failed(refused(status, text))
    doc = as_json(text)
    output = [i for i in (doc.get("output") or []) if isinstance(i, dict)]
    kinds = [str(i.get("type")) for i in output]
    searches = [n for n, i in enumerate(output)
                if i.get("type") == "web_search_call"
                and i.get("status") == "completed"]
    messages = [n for n, i in enumerate(output) if i.get("type") == "message"]
    answer = " ".join(
        part.get("text") or ""
        for n in messages for part in (output[n].get("content") or [])
        if isinstance(part, dict) and part.get("type") == "output_text")
    n = delta(before, ROUTE_RESPONSES)
    query = ""
    if searches:
        action = output[searches[0]].get("action")
        query = str((action if isinstance(action, dict) else {}).get("query") or "")
    seen = (f"output : {' → '.join(kinds) or 'vide'}"
            + (f" ({short(query, 60)})" if query else "")
            + f", {counter(ROUTE_RESPONSES, n)} — "
            + (short(answer) or "pas de texte : " + short(text)))
    if doc.get("status") != "completed":
        return failed(f"réponse au statut {doc.get('status')} "
                      f"({short(doc.get('error'), 160)}) : {seen}")
    if not searches:
        return failed(f"aucun `web_search_call` terminé : {seen}")
    if "function_call" in kinds:
        return failed(f"un `function_call` rendu au client : {seen}")
    if not messages or messages[-1] < searches[0]:
        return failed(f"pas de message après la recherche : {seen}")
    if NEEDLE.lower() not in answer.lower():
        return failed(f"réponse sans « {NEEDLE} » : {seen}")
    return passed(seen)


def _answer(output):
    return " ".join(
        part.get("text") or ""
        for i in output if i.get("type") == "message"
        for part in (i.get("content") or [])
        if isinstance(part, dict) and part.get("type") == "output_text")


def long_page(model):
    """8. L'exemple en deux tours, comme un client Responses le joue.
    Tour 1 : résumer une page longue — elle doit être lue en morceaux
    contigus depuis 0, chacun rendu au client avec sa plage (« url [a, b] »).
    Tour 2 : le client renvoie la conversation SANS le contenu de la page
    (les éléments `web_search_call` n'en portent que l'URL) et pose une
    question dont la réponse n'est que dans la page : le modèle doit y
    répondre sans rien relire — c'est la mémoire des résultats du proxy —,
    et le backend reprendre l'essentiel du prompt de son cache, signe que la
    conversation rejouée est celle qu'il a calculée."""
    question = f"Résume-moi {LONG_URL} en un paragraphe."
    first = [{"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": question}]}]
    tools = [{"type": "web_search"}]
    status, _, text = http_call("POST", ROUTE_RESPONSES, {
        "model": model, "input": first, "stream": False, "tools": tools})
    if status != 200:
        return failed("tour 1 : " + refused(status, text))
    out1 = [i for i in (as_json(text).get("output") or []) if isinstance(i, dict)]
    spans = []
    for i in out1:
        action = i.get("action") if isinstance(i.get("action"), dict) else {}
        url = str(action.get("url") or "")
        if i.get("type") == "web_search_call" and action.get("type") == "open_page" \
                and url.startswith(LONG_URL) and url.endswith("]") and " [" in url:
            a, _, b = url.rsplit(" [", 1)[1][:-1].partition(", ")
            if a.isdigit() and b.isdigit():
                spans.append((int(a), int(b)))
    summary = _answer(out1)
    seen = f"tour 1 : morceaux {spans or 'aucun'} — {short(summary, 120)}"
    if len(spans) < 2:
        return failed("la page n'a pas été lue en plusieurs morceaux "
                      f"(trop courte, ou plage absente de l'URL rendue) : {seen}")
    if spans[0][0] != 0 or any(spans[n][0] != spans[n - 1][1]
                               for n in range(1, len(spans))):
        return failed(f"morceaux non contigus depuis 0 : {seen}")
    if LONG_NEEDLE.lower() not in summary.lower():
        return failed(f"résumé sans « {LONG_NEEDLE} » : {seen}")

    before = counts()
    follow = first + out1 + [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": LONG_QUESTION}]}]
    status, _, text = http_call("POST", ROUTE_RESPONSES, {
        "model": model, "input": follow, "stream": False, "tools": tools})
    if status != 200:
        return failed("tour 2 : " + refused(status, text))
    doc = as_json(text)
    out2 = [i for i in (doc.get("output") or []) if isinstance(i, dict)]
    reread = sum(1 for i in out2 if i.get("type") == "web_search_call")
    usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
    prompt = int(usage.get("input_tokens") or 0)
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    share = 100 * cached // prompt if prompt else 0
    answer = _answer(out2)
    seen = (f"{len(spans)} morceaux {spans}, puis tour 2 : {reread} relecture(s), "
            f"{cached}/{prompt} tokens du cache ({share} %) — {short(answer, 160)}")
    if reread:
        return failed(f"le modèle a rappelé un outil au tour 2 (il n'avait "
                      f"plus la page) : {seen}")
    if LONG_ANSWER.lower() not in answer.lower():
        return failed(f"réponse sans « {LONG_ANSWER} » : {seen}")
    if before is not None:
        now = counts() or {}
        key = (ROUTE_RESPONSES, "web_fetch")
        if now.get(key, 0) != before.get(key, 0):
            return failed(f"une lecture de page comptée au tour 2 : {seen}")
    # Moins de la moitié du prompt reprise : la conversation rejouée n'est
    # pas celle que le backend a calculée (ou il n'a pas de cache de préfixe).
    if prompt and share < 50:
        return failed(f"cache du backend non repris au tour 2 : {seen}")
    return passed(seen)


def usage_proof(base, expected):
    """7. Le compteur du proxy, entre le début du jeu (`base`) et
    maintenant, par route : au moins une exécution de web_search par
    scénario RÉUSSI sur cette route (`expected`). C'est la preuve côté
    proxy que l'outil a tourné, et par la bonne route. « Au moins » : le
    compteur est celui du proxy entier, un autre client peut l'avancer
    pendant le jeu — et un scénario peut chercher deux fois."""
    wanted = {route: n for route, n in expected.items() if n}
    if not wanted:
        return failed("aucun scénario avec recherche n'a réussi : rien à "
                      "recouper avec le compteur")
    got = {route: delta(base, route, n) for route, n in wanted.items()}
    seen = ", ".join(
        f"{route} " + ("illisible" if got[route] is None else f"+{got[route]}")
        + f" (≥ {n} attendu)" for route, n in wanted.items())
    short_of = [route for route, n in wanted.items()
                if got[route] is None or got[route] < n]
    if short_of:
        return failed(f"exécutions non comptées sur {', '.join(short_of)} : {seen}")
    return passed(f"web_search — {seen}")


def main():
    if not PROXY_URL or not MODELS:
        print("PROXY_URL et MODELS sont requis (envTest/.env).")
        return 2
    status, _, text = http_call("GET", "/healthz", timeout=10)
    health = as_json(text) if status == 200 else {}
    hosted = [str(t) for t in (health.get("tools") or {}).get("enabled") or []]
    search = "web_search" in hosted
    chat = bool((health.get("chat") or {}).get("hosted_tools"))
    responses = bool((health.get("responses") or {}).get("enabled"))
    no_search = ("web_search n'est pas hébergé par ce proxy (/healthz : "
                 f"tools.enabled = [{' '.join(hosted)}])")
    no_chat = no_search if not search else (
        "ce proxy ne boucle pas les outils déclarés sur /v1/chat/completions "
        "(/healthz : chat.hosted_tools absent ou false)")
    no_responses = no_search if not search else (
        "surface Responses inactive sur ce proxy (/healthz : "
        "responses.enabled absent ou false)")
    summary = ""

    for model in MODELS:
        print(f"\n════ api (Python {platform.python_version()}, urllib) → "
              f"{PROXY_URL} | modèle {model} ════", flush=True)
        fails_before, skips_before = fails, skips
        base = counts() if search else None
        # Par route, le nombre de scénarios avec recherche qui ont RÉUSSI
        # (chacun rend True ou False) : ce que le compteur doit au moins
        # avoir vu, au scénario 7.
        expected = {ROUTE_CHAT: 0, ROUTE_TOOLS: 0, ROUTE_RESPONSES: 0}

        print("1. chat/completions, web_search déclaré, en flux (une "
              "réponse : un id, pas de tool_calls, annotations url_citation)")
        if search and chat:
            expected[ROUTE_CHAT] += chat_stream(model)
        else:
            skipped(no_chat)

        print("2. chat/completions, web_search déclaré, en JSON")
        if search and chat:
            expected[ROUTE_CHAT] += chat_json(model)
        else:
            skipped(no_chat)

        print("3. chat/completions sans déclaration (relais ordinaire)")
        chat_plain(model)

        print("4. GET /v1/tools (les outils actifs et leur schéma)")
        if search:
            tools_list()
        else:
            skipped(no_search)

        print("5. POST /v1/tools/web_search (appel direct)")
        if search:
            expected[ROUTE_TOOLS] += tools_run()
        else:
            skipped(no_search)

        print("6. /v1/responses, web_search déclaré, en JSON "
              "(web_search_call terminé, puis message)")
        if search and responses:
            expected[ROUTE_RESPONSES] += responses_json(model)
        else:
            skipped(no_responses)

        print("7. Usage API des outils : exécutions comptées par le proxy, "
              "avant / après, par route")
        if not search:
            skipped(no_search)
        elif base is None:
            skipped("ce proxy ne sert pas GET /v1/organization/usage/tools "
                    "(antérieur au 05/10/2026, ou clé refusée)")
        else:
            usage_proof(base, expected)

        print("8. Page longue lue par morceaux, puis question de suivi sans "
              "relecture (mémoire des résultats, cache du backend)")
        if "web_fetch" in hosted and responses:
            long_page(model)
        else:
            skipped("web_fetch n'est pas hébergé par ce proxy, ou la surface "
                    f"Responses est inactive (/healthz : tools.enabled = "
                    f"[{' '.join(hosted)}])")

        failed_here = fails - fails_before
        skipped_here = skips - skips_before
        played = 8 - skipped_here
        line = f"{played - failed_here}/{played}"
        if skipped_here:
            line += f", {skipped_here} sauté(s)"
        summary += f"\n  {model} : {line}"

    print(f"\nRésumé :{summary}")
    if skips:
        print(f"{skips} scénario(s) sauté(s) : ce que le proxy visé "
              "n'héberge pas (voir les lignes SKIP).")
    if fails:
        print(f"{fails} scénario(s) en échec.")
        return 1
    print("Tout passe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
