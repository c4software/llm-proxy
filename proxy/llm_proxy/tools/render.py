"""
Le RENDU d'une page par un vrai navigateur : un Chromium headless servi
par browserless (https://docs.browserless.io), pour les pages qui ne
montrent leur contenu qu'une fois leur JavaScript exécuté. Sert
`web_fetch` et `web_grep` (`render: true`) ; le texte en est tiré comme
d'une page téléchargée (web_fetch.page_text).

Une seule requête par rendu : POST <render_url>/function, une fonction
Puppeteer (FUNCTION) qui ouvre la page dans un contexte neuf (ni cookie ni
stockage gardés), en rend le HTML, l'adresse finale, son IP, et la capture
si elle est demandée.

LE RÉSEAU : le Chromium tourne hors du proxy et ne passe pas par net.py.
Ce qui est fait ici pour qu'une page ne lui fasse pas lire le réseau
interne, sachant que ce n'est qu'une ATTÉNUATION (docs/outils.md) :
  * l'URL demandée passe d'abord par net.check et net.public_target, comme
    pour un téléchargement ;
  * dans le navigateur, chaque requête de la page (sous-ressources, fetch,
    redirections, cadres) est REFUSÉE si son hôte est une IP privée en
    clair, `localhost`, un nom sans point (un service Docker), un suffixe
    local (.local, .lan, .internal, .home.arpa…), un domaine bloqué de
    [tools.net], ou un domaine de RENDER_BLOCKED_HOSTS — variable
    d'ENVIRONNEMENT du serveur (son .env), pour ne rien écrire des noms
    internes dans le dépôt ni dans la configuration ;
  * l'adresse finale doit passer net.check, et l'IP qui a servi la page
    (si browserless la donne) doit être publique : sinon le rendu est jeté.
Ce qui reste possible : un nom PUBLIC qui résout vers une adresse interne,
chargé en sous-ressource. Le service browserless doit donc tourner sur un
réseau à lui (le compose d'exemple), sans les autres conteneurs.
"""

import base64
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx2

from .. import config
from . import net
from .contract import ToolError

URL = config.text("tools.web_fetch.render_url", "").rstrip("/")
TOKEN = config.text("tools.web_fetch.render_token", "")
# Délai (s) du chargement de la page dans le navigateur ; la requête à
# browserless en a 15 de plus (capture, contenu).
TIMEOUT = config.num("tools.web_fetch.render_timeout", 25)
# Attente (ms) après le chargement, pour les pages qui remplissent leur
# contenu après coup.
SETTLE_MS = config.integer("tools.web_fetch.render_settle_ms", 1000)
MAX_HTML = config.integer("tools.web_fetch.max_bytes", 2_000_000)

# Les hôtes que la page ne joint jamais (expressions sur le NOM d'hôte).
BLOCKED_HOSTS = (
    r"^localhost$", r"\.localhost$",
    r"^[^.]+$",                                  # sans point : un service Docker, une IPv6
    r"^(0|10|127)\.", r"^192\.168\.", r"^172\.(1[6-9]|2\d|3[01])\.",
    r"^169\.254\.", r"^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.",   # CGNAT, Tailscale
    r"\.(local|lan|internal|intranet|corp|home|home\.arpa|ts\.net)$",
)


def available() -> bool:
    return bool(URL)


def _domains() -> list[str]:
    """Les domaines à refuser en plus : [tools.net].blocked_domains et
    RENDER_BLOCKED_HOSTS (environnement, séparés par des virgules)."""
    extra = os.environ.get("RENDER_BLOCKED_HOSTS", "")
    names = [*net.BLOCKED_DOMAINS, *extra.split(",")]
    out = []
    for d in names:
        d = str(d).strip().lower().split("://", 1)[-1].split("/", 1)[0]
        if d:
            out.append(d)
    return out


def blocked_patterns() -> list[str]:
    return [*BLOCKED_HOSTS, *(r"(^|\.)" + re.escape(d) + "$" for d in _domains())]


def host_blocked(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return not host or any(re.search(p, host) for p in blocked_patterns())


# La fonction exécutée par browserless (Puppeteer). `context` : url,
# timeout (ms), settle (ms), blocked (expressions), screenshot, maxHtml.
FUNCTION = r"""
export default async ({ page, context }) => {
  const blocked = context.blocked.map((p) => new RegExp(p, "i"));
  const refused = [];
  const bad = (raw) => {
    let u;
    try { u = new URL(raw); } catch { return true; }
    if (["data:", "blob:", "about:"].includes(u.protocol)) return false;
    if (!["http:", "https:"].includes(u.protocol)) return true;
    return blocked.some((re) => re.test(u.hostname));
  };
  await page.setRequestInterception(true);
  page.on("request", (r) => {
    if (bad(r.url())) { if (refused.length < 20) refused.push(r.url()); r.abort("blockedbyclient"); }
    else r.continue();
  });
  await page.setViewport({ width: 1280, height: 900 });
  let resp = null, error = "";
  try {
    resp = await page.goto(context.url, { waitUntil: "networkidle2", timeout: context.timeout });
  } catch (e) { error = String((e && e.message) || e); }
  if (context.settle) await new Promise((r) => setTimeout(r, context.settle));
  let html = "";
  try { html = await page.content(); } catch (e) { error = error || String(e); }
  let shot = "";
  if (context.screenshot) {
    try { shot = await page.screenshot({ type: "jpeg", quality: 70, encoding: "base64" }); } catch (e) { error = error || String(e); }
  }
  const ra = resp && resp.remoteAddress ? resp.remoteAddress() : {};
  return { data: {
    url: page.url(), status: resp ? resp.status() : 0, ip: (ra && ra.ip) || "",
    title: await page.title().catch(() => ""), html: html.slice(0, context.maxHtml),
    truncated: html.length > context.maxHtml, screenshot: shot, refused, error,
  }, type: "application/json" };
};
"""


@dataclass(frozen=True)
class Rendered:
    url: str            # l'adresse finale
    status: int
    title: str
    html: str
    truncated: bool
    image: bytes | None
    refused: tuple[str, ...]


async def render(url: str, settings=None, *, screenshot: bool = False,
                 transport=None) -> Rendered:
    """La page à `url`, rendue. Lève ToolError."""
    if not URL:
        raise ToolError("unavailable", "page rendering is not configured on this proxy.")
    net.check(url, settings)
    try:
        await net.public_target(url, net.ALLOW_PRIVATE)
    except net.Blocked as exc:
        raise ToolError(exc.code, f"{exc}.")
    if host_blocked(url) and not net.ALLOW_PRIVATE:
        raise ToolError("not_allowed", f"{urlsplit(url).hostname} is an internal "
                                       f"address, which this proxy does not render.")
    body = {"code": FUNCTION, "context": {
        "url": url, "timeout": int(TIMEOUT * 1000), "settle": SETTLE_MS,
        "blocked": blocked_patterns(), "screenshot": bool(screenshot), "maxHtml": MAX_HTML}}
    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT + 15, transport=transport,
                                     trust_env=False) as c:
            r = await c.post(f"{URL}/function", params={"token": TOKEN} if TOKEN else None,
                             json=body)
    except httpx2.HTTPError as exc:
        raise ToolError("unavailable", f"the page renderer is unreachable ({type(exc).__name__}).")
    if r.status_code == 429:
        raise ToolError("too_many_requests", "the page renderer is busy; try again shortly.")
    if r.status_code >= 400:
        raise ToolError("unavailable", f"the page renderer failed (HTTP {r.status_code}).")
    try:
        d = r.json()
    except ValueError:
        raise ToolError("unavailable", "the page renderer returned an unreadable answer.")
    # browserless rend l'objet de la fonction tel quel : {data, type}.
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        d = d["data"]
    if not isinstance(d, dict):
        raise ToolError("unavailable", "the page renderer returned an unreadable answer.")
    final = str(d.get("url") or url)
    # Là où la page a fini : un hôte interne, ou une IP privée, et le rendu
    # est jeté (une redirection vers le réseau local).
    ip = str(d.get("ip") or "").strip("[]")
    if not net.ALLOW_PRIVATE and (host_blocked(final) or (ip and not net.is_public(ip))):
        raise ToolError("not_allowed", f"{url} led to an internal address, which "
                                       f"this proxy does not render.")
    net.check(final, settings)
    html = str(d.get("html") or "")
    status = int(d.get("status") or 0)
    if not html.strip():
        why = d.get("error") or "the page is empty"
        raise ToolError("not_accessible", f"could not render {url} ({str(why)[:200]}).")
    if status >= 400:
        raise ToolError("too_many_requests" if status == 429 else "not_accessible",
                        f"{final} returned HTTP {status}.")
    image = None
    if d.get("screenshot"):
        try:
            image = base64.b64decode(d["screenshot"])
        except (ValueError, TypeError):
            image = None
    return Rendered(final, status, str(d.get("title") or ""), html, bool(d.get("truncated")),
                    image, tuple(str(u) for u in (d.get("refused") or ())[:20]))
