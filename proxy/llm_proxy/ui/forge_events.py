"""
Le corps NATIF d'un webhook de forge (GitHub, GitLab, Gitea/Forgejo), reçu
par un webhook entrant (webhook_routes.receive_hook) : il n'a pas de
`text` à la Slack. Reconnu par son en-tête d'événement, il devient un
message court pour le modèle.

  X-Gitea-Event    Gitea, Forgejo (lu d'abord : Gitea envoie aussi
                   X-GitHub-Event)
  X-GitHub-Event   GitHub
  X-Gitlab-Event   GitLab

Résumés : issue, commentaire, pull request (merge request), push ; un
autre événement donne son nom et son JSON, tronqué. Ignorés (rien n'est
lancé) : `ping` (l'essai d'un webhook créé), et tout événement qui porte
une branche du proxy (pull_request.PREFIX : PR, push, création de
branche, CI, commentaire GitLab sur sa MR) — sinon la PR ouverte par le
modèle relancerait le webhook. Un corps au format Slack (`text`,
`blocks`, `attachments`) n'est pas lu ici : Gitea pose ses en-têtes
d'événement aussi sur un webhook de type Slack.
"""

import json
from urllib.parse import urlsplit

from ..tools import pull_request

# Caractères au plus du corps d'une issue, d'un commentaire, d'une PR.
MAX_BODY = 8000
# Caractères au plus du JSON d'un événement non résumé.
MAX_JSON = 20_000
NAMES = {"gitea": "Gitea", "github": "GitHub", "gitlab": "GitLab"}


def _get(doc, *keys) -> str:
    for k in keys:
        doc = doc.get(k) if isinstance(doc, dict) else None
    return doc if isinstance(doc, str) else str(doc) if isinstance(doc, int) else ""


def _ours(branch: str) -> bool:
    branch = branch.removeprefix("refs/heads/")
    return bool(branch) and branch.startswith(pull_request.PREFIX)


# Où un événement nomme sa branche : push et création (`ref`), PR et MR,
# CI de GitHub (check_suite, check_run, workflow_run, workflow_job) et de
# GitLab (pipeline, job : `object_attributes.ref`, `ref`), commentaire
# GitLab sur une MR.
BRANCH_FIELDS = (("ref",), ("pull_request", "head", "ref"), ("check_suite", "head_branch"),
                 ("check_run", "check_suite", "head_branch"), ("workflow_run", "head_branch"),
                 ("workflow_job", "head_branch"), ("object_attributes", "ref"),
                 ("object_attributes", "source_branch"), ("merge_request", "source_branch"))


def _on_our_branch(p: dict) -> bool:
    branches = p.get("branches")                  # l'événement status de GitHub
    return any(_ours(_get(p, *keys)) for keys in BRANCH_FIELDS) or any(
        _ours(_get(b, "name")) for b in (branches if isinstance(branches, list) else []))


def _message(forge: str, what: str, project: str, author: str, title: str = "",
             url: str = "", branch: str = "", body: str = "") -> str:
    lines = [f"[{NAMES[forge]}] {what}" + (f" — {project}" if project else "")]
    for label, value in (("Par", author), ("Titre", title), ("Branche", branch),
                         ("Lien", url)):
        if value:
            lines.append(f"{label} : {value}")
    body = body.strip()
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + " […]"
    return "\n".join(lines) + (f"\n\n{body}" if body else "")


def _github(forge: str, name: str, p: dict) -> tuple[str, str] | None:
    """GitHub, et Gitea qui en reprend les formes."""
    project = _get(p, "repository", "full_name")
    author = _get(p, "sender", "login") or _get(p, "sender", "username")
    action = _get(p, "action")
    if name == "issues":
        return _message(forge, f"issue {action} #{_get(p, 'issue', 'number')}", project, author,
                        _get(p, "issue", "title"), _get(p, "issue", "html_url"),
                        body=_get(p, "issue", "body")), author
    if name in ("issue_comment", "pull_request_review_comment"):
        on = "pull_request" if name == "pull_request_review_comment" else "issue"
        return _message(forge, f"commentaire ({action}) sur #{_get(p, on, 'number')}", project,
                        author, _get(p, on, "title"), _get(p, "comment", "html_url"),
                        body=_get(p, "comment", "body")), author
    if name == "pull_request":
        head = _get(p, "pull_request", "head", "ref")
        return _message(forge, f"pull request {action} #{_get(p, 'pull_request', 'number')}",
                        project, author, _get(p, "pull_request", "title"),
                        _get(p, "pull_request", "html_url"),
                        f"{head} → {_get(p, 'pull_request', 'base', 'ref')}",
                        _get(p, "pull_request", "body")), author
    if name == "push":
        return _push(forge, project, author or _get(p, "pusher", "name"), _get(p, "ref"),
                     _get(p, "compare") or _get(p, "compare_url"), p.get("commits")), author
    return None


def _gitlab(p: dict) -> tuple[str, str] | None:
    kind = _get(p, "object_kind")
    project = _get(p, "project", "path_with_namespace")
    author = _get(p, "user", "username") or _get(p, "user_username")
    o = p.get("object_attributes") if isinstance(p.get("object_attributes"), dict) else {}
    if kind == "issue":
        return _message("gitlab", f"issue {_get(o, 'action')} #{_get(o, 'iid')}", project,
                        author, _get(o, "title"), _get(o, "url"),
                        body=_get(o, "description")), author
    if kind == "note":
        on = p.get("merge_request") or p.get("issue") or {}
        return _message("gitlab", f"commentaire sur {_get(o, 'noteable_type')} "
                                  f"#{_get(on, 'iid')}", project, author,
                        _get(on, "title"), _get(o, "url"), body=_get(o, "note")), author
    if kind == "merge_request":
        return _message("gitlab", f"merge request {_get(o, 'action')} !{_get(o, 'iid')}",
                        project, author, _get(o, "title"), _get(o, "url"),
                        f"{_get(o, 'source_branch')} → {_get(o, 'target_branch')}",
                        _get(o, "description")), author
    if kind == "push":
        return _push("gitlab", project, author, _get(p, "ref"), "", p.get("commits")), author
    return None


def _push(forge: str, project: str, author: str, ref: str, url: str, commits) -> str:
    commits = commits if isinstance(commits, list) else []
    lines = []
    for c in commits[:20]:
        first = (_get(c, "message").strip().splitlines() or ["?"])[0]
        lines.append(f"- {first} {_get(c, 'url')}".rstrip())
    body = "\n".join(lines)
    if len(commits) > 20:
        body += f"\n- … {len(commits) - 20} commit(s) de plus"
    return _message(forge, f"push de {len(commits)} commit(s)", project, author,
                    url=url, branch=ref.removeprefix("refs/heads/"), body=body)


def _scope(forge: str, p: dict) -> tuple[str, str]:
    """(hôte, projet) de l'événement : ce que les outils de forge pourront
    ouvrir pendant le tour (settings.FORGE_SCOPE). «» : inconnu, rien."""
    if forge == "gitlab":
        project, url = _get(p, "project", "path_with_namespace"), _get(p, "project", "web_url")
    else:
        project, url = _get(p, "repository", "full_name"), _get(p, "repository", "html_url")
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    return (host, project) if host and project else ("", "")


def event(headers, payload: dict) -> tuple[str, str, tuple[str, str]] | None:
    """(message, auteur, (hôte, projet)) d'un corps de forge ; None : pas
    un corps de forge (aucun en-tête d'événement, ou un corps au format
    Slack) ; message «» : à ignorer."""
    if any(k in payload for k in ("text", "blocks", "attachments")):
        return None
    for header, forge in (("x-gitea-event", "gitea"), ("x-github-event", "github"),
                          ("x-gitlab-event", "gitlab")):
        name = (headers.get(header) or "").strip()
        if name:
            break
    else:
        return None
    if name == "ping" or _on_our_branch(payload):
        return "", "", ("", "")
    scope = _scope(forge, payload)
    got = _gitlab(payload) if forge == "gitlab" else _github(forge, name, payload)
    if got is not None:
        return (*got, scope)
    project = _get(payload, "repository", "full_name") or \
        _get(payload, "project", "path_with_namespace")
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) > MAX_JSON:
        raw = raw[:MAX_JSON] + " […]"
    return _message(forge, f"événement {name}", project, "",
                    body=f"```json\n{raw}\n```"), "", scope
