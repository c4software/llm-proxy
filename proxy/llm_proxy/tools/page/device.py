"""
Les outils du NAVIGATEUR — l'appareil : date, position, notifications,
presse-papiers, agenda, liens, partage, écran, caméra, codes, couleur,
écran allumé, voix.
Leur DÉCLARATION (Runs.BROWSER) : le prompt, le schéma, la famille du
menu Outils. Leur exécution est dans la page, chat/tools/device/
(docs/navigateur.md, « Écrire un outil de la page »).
"""

from ..contract import BrowserTool, Family, FamilyKey, ToolName
from .common import REASON

MAX_DELAY = 24 * 60          # minutes d'un rappel (notify), au plus
MAX_TEXT = 4000              # caractères lus à voix haute (speak), au plus


# ── Date et heure ─────────────────────────────────────────────────────

DEVICE_CONTEXT = BrowserTool(
    ToolName.DEVICE_CONTEXT,
    description=("Donne la date et l'heure locales de l'utilisateur, son fuseau horaire et ses "
                 "langues, et ce que son navigateur dit de l'appareil (en ligne, connexion, "
                 "batterie, thème, taille d'écran). À utiliser avant de parler d'« aujourd'hui "
                 "», de « demain » ou d'une heure, ou pour adapter une réponse à l'appareil."),
)

CONTEXT_FAMILY = Family(
    FamilyKey.CONTEXT,
    label='Date et heure',
    tools=(ToolName.DEVICE_CONTEXT,),
    hint='date, heure, fuseau, langues, appareil',
    on=True,
)


# ── Position ──────────────────────────────────────────────────────────

GET_LOCATION = BrowserTool(
    ToolName.GET_LOCATION,
    description=("Demande à l'utilisateur sa position (latitude, longitude), qu'il accepte ou "
                 'refuse sur une carte dans le chat. Seulement quand la réponse en dépend '
                 'vraiment (météo, itinéraire, lieux proches) ; dire pourquoi dans `reason`.'),
    params={
        'reason': {
            'type': 'string',
            'description': "Pourquoi la position est utile, en une phrase, montrée à l'utilisateur.",
        },
    },
    required=('reason',),
)

LOCATION_FAMILY = Family(
    FamilyKey.LOCATION,
    label='Position',
    tools=(ToolName.GET_LOCATION,),
    hint='demandée sur une carte, approximative ou précise',
)


# ── Notifications ─────────────────────────────────────────────────────

NOTIFY = BrowserTool(
    ToolName.NOTIFY,
    description=("Envoie une notification du navigateur à l'utilisateur, tout de suite ou "
                 'après un délai (un rappel : « rappelle-moi dans 20 minutes »). Le rappel '
                 "n'arrive que si la page reste ouverte ; pour un rendez-vous, préférer "
                 'add_to_calendar.'),
    params={
        'title': {'type': 'string', 'description': 'Le titre, court.'},
        'body': {'type': 'string', 'description': 'Le texte (facultatif).'},
        'delay_minutes': {
            'type': 'number',
            'description': f'Dans combien de minutes (0 à {MAX_DELAY}) ; 0 ou absent : tout de suite.',
        },
    },
    required=('title',),
    settings={'max_delay': MAX_DELAY},
)

NOTIFY_FAMILY = Family(
    FamilyKey.NOTIFY,
    label='Notifications',
    tools=(ToolName.NOTIFY,),
    hint='tout de suite, ou un rappel dans N minutes',
    on=True,
)


# ── Presse-papiers ────────────────────────────────────────────────────

CLIPBOARD_WRITE = BrowserTool(
    ToolName.CLIPBOARD_WRITE,
    description=("Copie un texte dans le presse-papiers de l'utilisateur (une commande, une "
                 'adresse, un message à coller ailleurs).'),
    params={'text': {'type': 'string', 'description': 'Le texte à copier.'}},
    required=('text',),
)

CLIPBOARD_READ = BrowserTool(
    ToolName.CLIPBOARD_READ,
    description=("Demande à l'utilisateur le contenu de son presse-papiers, qu'il accepte ou "
                 "refuse sur une carte : quand il parle de « ce que j'ai copié »."),
    params={
        'reason': REASON,
    },
)

CLIPBOARD_FAMILY = Family(
    FamilyKey.CLIPBOARD,
    label='Presse-papiers',
    tools=(ToolName.CLIPBOARD_WRITE, ToolName.CLIPBOARD_READ),
    hint='copier ; lire sur une carte',
    on=True,
)


# ── Agenda ────────────────────────────────────────────────────────────

ADD_TO_CALENDAR = BrowserTool(
    ToolName.ADD_TO_CALENDAR,
    description=("Propose à l'utilisateur un événement à ajouter à son agenda (un fichier .ics "
                 "sous la réponse, qu'il ouvre dans son agenda). Les heures sont locales à "
                 "l'utilisateur (appeler device_context si la date du jour est utile)."),
    params={
        'title': {'type': 'string', 'description': "Le titre de l'événement."},
        'start': {
            'type': 'string',
            'description': ('Début, heure locale : « 2026-10-18T09:30 », ou « 2026-10-18 » pour une journée '
             'entière.'),
        },
        'end': {
            'type': 'string',
            'description': 'Fin, même format (facultatif : 1 h après le début, ou la journée).',
        },
        'location': {'type': 'string', 'description': 'Le lieu (facultatif).'},
        'description': {'type': 'string', 'description': 'Les détails (facultatif).'},
        'alarm_minutes': {'type': 'number', 'description': 'Une alerte N minutes avant (facultatif).'},
    },
    required=('title', 'start'),
)

CALENDAR_FAMILY = Family(
    FamilyKey.CALENDAR,
    label='Agenda',
    tools=(ToolName.ADD_TO_CALENDAR,),
    hint='un événement à ajouter (.ics)',
    on=True,
)


# ── Liens ─────────────────────────────────────────────────────────────

OPEN_LINK = BrowserTool(
    ToolName.OPEN_LINK,
    description=("Propose à l'utilisateur d'ouvrir une adresse web (un itinéraire, une "
                 'recherche, une page de documentation) : une carte sous la réponse, où il lit '
                 "l'adresse et l'ouvre dans un nouvel onglet quand il veut. Le résultat ne dit "
                 "pas s'il l'a ouverte."),
    params={
        'url': {'type': 'string', 'description': "L'adresse complète, http ou https."},
        'reason': {'type': 'string', 'description': "Pourquoi l'ouvrir, en une phrase (facultatif)."},
    },
    required=('url',),
)

LINKS_FAMILY = Family(
    FamilyKey.LINKS,
    label='Liens',
    tools=(ToolName.OPEN_LINK,),
    hint='une adresse à ouvrir, en carte sous la réponse',
    on=True,
)


# ── Partage ───────────────────────────────────────────────────────────

SHARE = BrowserTool(
    ToolName.SHARE,
    description=("Propose à l'utilisateur de partager un texte (et une adresse) vers une autre "
                 'application — messagerie, mail, notes — par le partage du système : une '
                 "carte sous la réponse, avec un bouton Partager… qu'il utilise quand il veut "
                 "(et autant de fois qu'il veut). Le résultat ne dit pas s'il a partagé."),
    params={
        'title': {'type': 'string', 'description': 'Un titre (facultatif).'},
        'text': {'type': 'string', 'description': 'Le texte à partager.'},
        'url': {'type': 'string', 'description': 'Une adresse (facultatif).'},
    },
    required=('text',),
)

SHARE_FAMILY = Family(
    FamilyKey.SHARE,
    label='Partage',
    tools=(ToolName.SHARE,),
    hint='une carte sous la réponse, à partager quand on veut',
)


# ── Capture d'écran ───────────────────────────────────────────────────

CAPTURE_SCREEN = BrowserTool(
    ToolName.CAPTURE_SCREEN,
    description=("Demande à l'utilisateur une capture de son écran (un onglet, une fenêtre ou "
                 "l'écran entier, qu'il choisit et vérifie avant de l'envoyer) : pour voir une "
                 'erreur, une interface, un document. Il faut un modèle qui lit les images.'),
    params={
        'reason': REASON,
    },
    required=('reason',),
)

SCREEN_FAMILY = Family(
    FamilyKey.SCREEN,
    label="Capture d'écran",
    tools=(ToolName.CAPTURE_SCREEN,),
    hint="un onglet, une fenêtre ou l'écran, avec aperçu",
)


# ── Caméra ────────────────────────────────────────────────────────────

TAKE_PHOTO = BrowserTool(
    ToolName.TAKE_PHOTO,
    description=("Demande à l'utilisateur une photo prise avec sa caméra (une étiquette, un "
                 "document, un objet), qu'il vérifie avant de l'envoyer. Il faut un modèle qui "
                 'lit les images.'),
    params={
        'reason': REASON,
    },
    required=('reason',),
)

CAMERA_FAMILY = Family(
    FamilyKey.CAMERA,
    label='Caméra',
    tools=(ToolName.TAKE_PHOTO,),
    hint='une photo, avec aperçu',
)


# ── QR et codes-barres ────────────────────────────────────────────────

SCAN_CODE = BrowserTool(
    ToolName.SCAN_CODE,
    description=("Demande à l'utilisateur de lire un QR code ou un code-barres, à la caméra ou "
                 "sur son écran ; rend le texte lu (pas d'image)."),
    params={
        'reason': REASON,
    },
)

CODES_FAMILY = Family(
    FamilyKey.CODES,
    label='QR et codes-barres',
    tools=(ToolName.SCAN_CODE,),
    hint="lus à la caméra ou à l'écran",
)


# ── Pipette ───────────────────────────────────────────────────────────

PICK_COLOR = BrowserTool(
    ToolName.PICK_COLOR,
    description=("Demande à l'utilisateur de prendre une couleur n'importe où à son écran "
                 '(pipette) ; rend son code hexadécimal.'),
    params={'reason': {'type': 'string', 'description': 'Pourquoi, en une phrase (facultatif).'}},
)

COLOR_FAMILY = Family(
    FamilyKey.COLOR,
    label='Pipette',
    tools=(ToolName.PICK_COLOR,),
    hint="une couleur prise à l'écran",
)


# ── Écran allumé ──────────────────────────────────────────────────────

KEEP_SCREEN_ON = BrowserTool(
    ToolName.KEEP_SCREEN_ON,
    description=("Empêche l'écran de l'utilisateur de se mettre en veille (recette, pas-à-pas, "
                 'présentation), ou le rend à la normale. Tant que la page est visible ; une '
                 "carte sous la réponse permet à l'utilisateur de basculer lui-même."),
    params={'on': {'type': 'boolean', 'description': 'true : écran allumé ; false : veille normale.'}},
    required=('on',),
)

WAKE_FAMILY = Family(
    FamilyKey.WAKE,
    label='Écran allumé',
    tools=(ToolName.KEEP_SCREEN_ON,),
    hint='empêcher la mise en veille',
)


# ── Lecture à voix haute ──────────────────────────────────────────────

SPEAK = BrowserTool(
    ToolName.SPEAK,
    description=("Lit un texte à voix haute sur l'appareil de l'utilisateur (synthèse vocale "
                 "du navigateur) : une étape d'un pas-à-pas mains libres, un résumé à écouter. "
                 'Un texte court et oral, sans Markdown ; une carte sous la réponse permet de '
                 'le relire.'),
    params={
        'text': {'type': 'string', 'description': f'Le texte à lire ({MAX_TEXT} caractères au plus).'},
        'lang': {
            'type': 'string',
            'description': 'La langue, au format BCP 47 (« fr-FR », « en-US ») ; défaut : celle du navigateur.',
        },
    },
    required=('text',),
    settings={'max_text': MAX_TEXT},
)

SPEAK_FAMILY = Family(
    FamilyKey.SPEAK,
    label='Lecture à voix haute',
    tools=(ToolName.SPEAK,),
    hint='le navigateur lit un texte (synthèse vocale)',
)


TOOLS = (DEVICE_CONTEXT, GET_LOCATION, NOTIFY, CLIPBOARD_WRITE, CLIPBOARD_READ, ADD_TO_CALENDAR, OPEN_LINK, SHARE, CAPTURE_SCREEN, TAKE_PHOTO, SCAN_CODE, PICK_COLOR, KEEP_SCREEN_ON, SPEAK,)
FAMILIES = (CONTEXT_FAMILY, LOCATION_FAMILY, NOTIFY_FAMILY, CLIPBOARD_FAMILY, CALENDAR_FAMILY, LINKS_FAMILY, SHARE_FAMILY, SCREEN_FAMILY, CAMERA_FAMILY, CODES_FAMILY, COLOR_FAMILY, WAKE_FAMILY, SPEAK_FAMILY,)
