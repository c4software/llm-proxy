"""
Les outils du NAVIGATEUR — questions, fichiers, artifacts, dossier ouvert.
Leur DÉCLARATION (Runs.BROWSER) : le prompt, le schéma, la famille du
menu Outils. Leur exécution est dans la page, chat/tools/page/
(docs/navigateur.md, « Écrire un outil de la page »).
"""

from ..contract import BrowserTool, Family, FamilyKey, ToolName
from .common import CONTEXT, GREP, MAX_CONTEXT, MAX_SLICE, SLICE, SLICE_LINES

# Les types d'un artifact (show_artifact) : le schéma les énumère, la page
# les vérifie.
TYPES = ('html', 'markdown', 'svg', 'mermaid', 'code')


# ── Questions ─────────────────────────────────────────────────────────

ASK_USER = BrowserTool(
    ToolName.ASK_USER,
    description=("Pose à l'utilisateur de 1 à 4 questions à choix, dans le chat, et attend sa "
                 'réponse avant de continuer. À utiliser quand une décision lui revient ou '
                 "qu'une précision manque pour bien répondre — pas pour des confirmations "
                 'inutiles. Chaque question a 2 à 6 options courtes, chacune avec une brève '
                 "description de ce qu'elle implique ; l'utilisateur peut aussi répondre "
                 'librement.'),
    params={
        'questions': {
            'type': 'array',
            'minItems': 1,
            'maxItems': 4,
            'items': {
                'type': 'object',
                'required': ['question', 'header', 'options'],
                'properties': {
                    'question': {'type': 'string', 'description': 'La question, complète.'},
                    'header': {
                        'type': 'string',
                        'description': ("Un titre très court (1 à 3 mots), l'onglet de la question : « Budget "
                         '», « Hébergement ».'),
                    },
                    'options': {
                        'type': 'array',
                        'minItems': 2,
                        'maxItems': 6,
                        'items': {
                            'type': 'object',
                            'required': ['label'],
                            'properties': {
                                'label': {'type': 'string', 'description': 'La réponse, courte.'},
                                'description': {
                                    'type': 'string',
                                    'description': "Ce qu'elle implique, en une phrase.",
                                },
                            },
                        },
                    },
                    'multiple': {
                        'type': 'boolean',
                        'description': 'Plusieurs options peuvent être choisies.',
                    },
                },
            },
        },
    },
    required=('questions',),
)

ASK_FAMILY = Family(
    FamilyKey.ASK,
    label='Questions',
    tools=(ToolName.ASK_USER,),
    hint='des questions à choix, dans la feuille du bas',
    fixed=True,
)


# ── Fichiers ──────────────────────────────────────────────────────────

SAVE_FILE = BrowserTool(
    ToolName.SAVE_FILE,
    description=("Propose à l'utilisateur un fichier à télécharger (CSV, JSON, script, "
                 'Markdown, configuration…), affiché sous ta réponse. Le contenu est du texte.'),
    params={
        'name': {'type': 'string', 'description': 'Nom du fichier, avec son extension.'},
        'content': {'type': 'string', 'description': 'Le contenu complet du fichier.'},
        'mime': {'type': 'string', 'description': "Type MIME (facultatif, déduit de l'extension)."},
    },
    required=('name', 'content'),
)

SAVE_FAMILY = Family(
    FamilyKey.SAVE,
    label='Fichiers',
    tools=(ToolName.SAVE_FILE,),
    hint='un fichier à télécharger sous la réponse',
    fixed=True,
)


# ── Artifacts ─────────────────────────────────────────────────────────

SHOW_ARTIFACT = BrowserTool(
    ToolName.SHOW_ARTIFACT,
    description=('Affiche dans le panneau latéral du chat une page HTML (scripts permis, '
                 'bibliothèques depuis cdnjs, jsdelivr ou unpkg ; le reste du réseau — fetch, '
                 "images et tuiles distantes — est bloqué tant que l'utilisateur ne l'a pas "
                 'autorisé pour cet artifact par le bouton Réseau du panneau : prévoir alors '
                 'un message clair si une requête échoue), un document Markdown, un SVG, un '
                 'schéma Mermaid ou du code. Réutiliser le même `id` pour en publier une '
                 'nouvelle version. Pour les visualisations, maquettes, petits outils '
                 'interactifs, documents longs — pas pour une réponse courte.'),
    params={
        'id': {
            'type': 'string',
            'description': 'Identifiant stable (ex. « tableau-de-bord ») ; même id = nouvelle version.',
        },
        'title': {'type': 'string', 'description': 'Titre court.'},
        'type': {'type': 'string', 'enum': list(TYPES)},
        'content': {
            'type': 'string',
            'description': 'Le contenu complet (une page HTML entière pour `html`).',
        },
        'language': {'type': 'string', 'description': 'Pour `code` : le langage.'},
    },
    required=('title', 'type', 'content'),
    settings={'types': list(TYPES)},
)

EDIT_ARTIFACT = BrowserTool(
    ToolName.EDIT_ARTIFACT,
    description=('Retouche un artifact déjà affiché SANS le réécrire : chaque `old_string`, un '
                 'passage exact et unique de sa dernière version (espaces et retours à la '
                 'ligne compris, assez long pour être unique), est remplacé par `new_string` — '
                 'ou, avec `regex`, une expression régulière (un changement répétitif : '
                 '`replace_all`). À préférer à show_artifact pour toute modification qui ne '
                 "change pas l'essentiel. Toutes les retouches s'appliquent, ou aucune. Pour "
                 'trouver le passage : grep_artifact, puis read_artifact sur ses lignes.'),
    params={
        'id': {'type': 'string', 'description': "L'identifiant de l'artifact."},
        'edits': {
            'type': 'array',
            'minItems': 1,
            'items': {
                'type': 'object',
                'required': ['old_string', 'new_string'],
                'properties': {
                    'old_string': {'type': 'string', 'description': 'Le passage exact à remplacer.'},
                    'new_string': {
                        'type': 'string',
                        'description': 'Ce qui le remplace (vide : le supprimer).',
                    },
                    'replace_all': {
                        'type': 'boolean',
                        'description': 'Remplacer toutes ses occurrences (sinon il doit être unique).',
                    },
                    'regex': {
                        'type': 'boolean',
                        'description': ('`old_string` est une expression régulière (JavaScript, multiligne) ; '
                         '`new_string` peut citer ses groupes ($1). Pour un changement '
                         'répétitif ; sinon, le texte exact.'),
                    },
                    'ignore_case': {
                        'type': 'boolean',
                        'description': 'Avec `regex` : sans tenir compte de la casse.',
                    },
                },
            },
        },
        'title': {'type': 'string', 'description': 'Un nouveau titre (facultatif).'},
    },
    required=('id', 'edits'),
)

READ_ARTIFACT = BrowserTool(
    ToolName.READ_ARTIFACT,
    description=("Rend la dernière version d'un artifact : entière, telle quelle, ou une "
                 'tranche de lignes numérotées (`start_line`, `end_line`) — à préférer pour un '
                 'gros artifact, après grep_artifact qui dit où chercher. Avant de le '
                 "retoucher quand son texte exact n'est plus sûr."),
    params={
        'id': {'type': 'string', 'description': "L'identifiant de l'artifact."},
        'start_line': {
            'type': 'integer',
            'description': "Première ligne (à partir de 1) ; absente avec end_line : l'artifact entier.",
        },
        'end_line': {
            'type': 'integer',
            'description': f'Dernière ligne, incluse (défaut : {SLICE_LINES} lignes ; {MAX_SLICE} au plus).',
        },
    },
    required=('id',),
    settings=SLICE,
)

GREP_ARTIFACT = BrowserTool(
    ToolName.GREP_ARTIFACT,
    description=("Cherche dans la dernière version d'un artifact, sans le relire en entier : "
                 'rend les lignes qui contiennent `pattern` (texte, ou expression régulière '
                 'avec `regex`), numérotées, avec `context` lignes autour. Pour retrouver un '
                 'passage avant de le lire (read_artifact sur ses lignes) puis de le retoucher '
                 '(edit_artifact).'),
    params={
        'id': {'type': 'string', 'description': "L'identifiant de l'artifact."},
        'pattern': {
            'type': 'string',
            'description': 'Le texte cherché (ou une expression régulière si `regex`).',
        },
        'regex': {
            'type': 'boolean',
            'description': '`pattern` est une expression régulière (JavaScript).',
        },
        'ignore_case': {'type': 'boolean', 'description': 'Sans tenir compte de la casse (défaut : oui).'},
        'context': {
            'type': 'integer',
            'description': f'Lignes de contexte avant et après (défaut {CONTEXT}, {MAX_CONTEXT} au plus).',
        },
    },
    required=('id', 'pattern'),
    settings=GREP,
)

ARTIFACTS_FAMILY = Family(
    FamilyKey.ARTIFACTS,
    label='Artifacts',
    tools=(ToolName.SHOW_ARTIFACT, ToolName.EDIT_ARTIFACT, ToolName.READ_ARTIFACT, ToolName.GREP_ARTIFACT),
    hint='le panneau latéral',
    fixed=True,
    authored=True,
    guidance=('Pour modifier un artifact déjà affiché : grep_artifact pour trouver le passage, '
              'read_artifact sur ses lignes (start_line, end_line) pour en avoir le texte '
              'exact, puis edit_artifact ; ne pas le relire en entier sans besoin. Un contenu '
              "d'artifact plus ancien dans la conversation peut être périmé : seule compte la "
              'version actuelle (read_artifact). show_artifact seulement pour un nouvel '
              "artifact, ou une refonte que l'utilisateur demande."),
)


# ── Proposer le débogage ──────────────────────────────────────────────

REQUEST_ARTIFACT_DEBUG = BrowserTool(
    ToolName.REQUEST_ARTIFACT_DEBUG,
    description=("Propose à l'utilisateur, sur une carte qu'il accepte ou refuse, de rétablir "
                 "le DÉBOGAGE d'un artifact qu'il a coupé : le modèle pourra alors lire sa "
                 'console et ses erreurs, y exécuter du code et le capturer (inspect_artifact, '
                 "run_in_artifact). À proposer quand l'utilisateur dit que cet artifact ne "
                 'marche pas, plutôt que de deviner la cause.'),
    params={
        'id': {'type': 'string', 'description': "L'identifiant de l'artifact."},
        'reason': {
            'type': 'string',
            'description': "Ce qui ne marche pas, en une phrase, montré à l'utilisateur.",
        },
    },
    required=('id',),
)

ARTIFACT_DEBUG_OFFER_FAMILY = Family(
    FamilyKey.ARTIFACT_DEBUG_OFFER,
    label='Proposer le débogage',
    tools=(ToolName.REQUEST_ARTIFACT_DEBUG,),
    hint='une carte pour le rétablir',
    fixed=True,
    guidance=("Si l'utilisateur signale qu'un artifact au débogage coupé ne marche pas (un "
              'bouton sans effet, une page blanche, une erreur) : proposer de rétablir le '
              'débogage avec request_artifact_debug plutôt que de deviner la cause.'),
)


# ── Déboguer un artifact ──────────────────────────────────────────────

INSPECT_ARTIFACT = BrowserTool(
    ToolName.INSPECT_ARTIFACT,
    description=("Inspecte un artifact tel qu'il tourne dans le panneau, sous les yeux de "
                 "l'utilisateur : sa console et ses erreurs depuis le chargement (scripts, "
                 'ressources bloquées, promesses rejetées), et sa page en texte (un élément '
                 'par ligne : balise, id, classes, texte, valeurs). `screenshot` : une capture '
                 'de la page, jointe si le modèle lit les images. À utiliser quand '
                 "l'utilisateur signale qu'un artifact ne marche pas, avant de le corriger, et "
                 'après pour vérifier.'),
    params={
        'id': {'type': 'string', 'description': "L'identifiant de l'artifact."},
        'screenshot': {'type': 'boolean', 'description': 'Joindre une capture de la page (facultatif).'},
    },
    required=('id',),
)

RUN_IN_ARTIFACT = BrowserTool(
    ToolName.RUN_IN_ARTIFACT,
    description=("Exécute du JavaScript DANS un artifact tel qu'il tourne dans le panneau "
                 "(l'état que voit l'utilisateur), pour reproduire un bug ou lire un état : le "
                 "corps d'une fonction async, `return` la valeur voulue. Aides : "
                 'click(sélecteur), type(sélecteur, texte), key(sélecteur, touche), wait(ms), '
                 'text(sélecteur), all(sélecteur) ; le DOM et les variables globales de la '
                 'page sont accessibles. Rend la valeur, et la console et les erreurs '
                 "survenues pendant l'exécution. Ne corrige rien : pour corriger, "
                 'edit_artifact, puis revérifier.'),
    params={
        'id': {'type': 'string', 'description': "L'identifiant de l'artifact."},
        'code': {'type': 'string', 'description': 'Le corps de la fonction async (avec `return`).'},
    },
    required=('id', 'code'),
)

ARTIFACT_DEBUG_FAMILY = Family(
    FamilyKey.ARTIFACT_DEBUG,
    label='Déboguer un artifact',
    tools=(ToolName.INSPECT_ARTIFACT, ToolName.RUN_IN_ARTIFACT),
    hint='tester la page affichée',
    fixed=True,
    authored=True,
    guidance=("Quand un artifact ne marche pas, ne pas deviner : l'inspecter (inspect_artifact "
              ': console, erreurs, page), reproduire le problème (run_in_artifact), corriger '
              '(edit_artifact), puis revérifier de la même façon. Le débogage est actif par '
              "défaut ; l'utilisateur peut le couper pour un artifact."),
)


# ── Dossier ouvert ────────────────────────────────────────────────────

FOLDER_LIST = BrowserTool(
    ToolName.FOLDER_LIST,
    description=("Liste le contenu du dossier ouvert par l'utilisateur dans le chat. Sans "
                 '`path` : sa racine. Les sous-dossiers finissent par « / », avec leur nombre '
                 'de fichiers ; les fichiers ont leur taille. `recursive` : tous les fichiers '
                 'sous `path`.'),
    params={
        'path': {
            'type': 'string',
            'description': 'Sous-dossier relatif à la racine ; vide = la racine.',
        },
        'recursive': {'type': 'boolean', 'description': 'Lister tous les fichiers sous `path`.'},
    },
)

FOLDER_READ = BrowserTool(
    ToolName.FOLDER_READ,
    description=('Lit un fichier texte du dossier ouvert, lignes numérotées. Un long fichier '
                 'se lit par tranches : `start_line` et `end_line` (incluses, à partir de 1).'),
    params={
        'path': {'type': 'string', 'description': 'Chemin du fichier, relatif à la racine du dossier.'},
        'start_line': {'type': 'integer', 'description': 'Première ligne (défaut : 1).'},
        'end_line': {
            'type': 'integer',
            'description': "Dernière ligne (défaut : jusqu'à la limite d'une lecture).",
        },
    },
    required=('path',),
)

FOLDER_SEARCH = BrowserTool(
    ToolName.FOLDER_SEARCH,
    description=('Cherche un texte dans les fichiers texte du dossier ouvert ; rend « '
                 'chemin:ligne: texte » pour chaque ligne trouvée.'),
    params={
        'pattern': {
            'type': 'string',
            'description': 'Le texte cherché (ou une expression régulière si `regex`).',
        },
        'path': {'type': 'string', 'description': 'Ne chercher que sous ce sous-dossier.'},
        'regex': {
            'type': 'boolean',
            'description': '`pattern` est une expression régulière (JavaScript).',
        },
        'ignore_case': {'type': 'boolean', 'description': 'Sans tenir compte de la casse (défaut : oui).'},
    },
    required=('pattern',),
)

FOLDER_WRITE = BrowserTool(
    ToolName.FOLDER_WRITE,
    description=('Crée ou remplace entièrement un fichier texte du dossier ouvert. '
                 "L'utilisateur voit le diff et accepte ou refuse ; le résultat le dit. Pour "
                 'une modification localisée, préférer folder_edit.'),
    params={
        'path': {'type': 'string', 'description': 'Chemin relatif à la racine du dossier.'},
        'content': {'type': 'string', 'description': 'Le contenu complet du fichier.'},
    },
    required=('path', 'content'),
)

FOLDER_EDIT = BrowserTool(
    ToolName.FOLDER_EDIT,
    description=("Remplace un passage EXACT d'un fichier du dossier ouvert (lire le fichier "
                 "d'abord). `old_text` doit y être une seule fois, sauf `replace_all`. "
                 "L'utilisateur voit le diff et accepte ou refuse."),
    params={
        'path': {'type': 'string'},
        'old_text': {
            'type': 'string',
            'description': "Le passage actuel, à l'identique (espaces compris).",
        },
        'new_text': {'type': 'string', 'description': 'Ce qui le remplace.'},
        'replace_all': {'type': 'boolean', 'description': 'Remplacer toutes les occurrences.'},
    },
    required=('path', 'old_text', 'new_text'),
)

FOLDER_FAMILY = Family(
    FamilyKey.FOLDER,
    label='Dossier ouvert',
    tools=(ToolName.FOLDER_LIST, ToolName.FOLDER_READ, ToolName.FOLDER_SEARCH, ToolName.FOLDER_WRITE, ToolName.FOLDER_EDIT),
    hint='lire, chercher, écrire avec accord',
    fixed=True,
    guidance=("Un dossier de l'utilisateur est ouvert dans le chat. Le parcourir avec "
              'folder_list, folder_read et folder_search plutôt que de supposer son contenu ; '
              "toute écriture (folder_write, folder_edit) passe par l'accord de l'utilisateur."),
)


TOOLS = (ASK_USER, SAVE_FILE, SHOW_ARTIFACT, EDIT_ARTIFACT, READ_ARTIFACT, GREP_ARTIFACT, REQUEST_ARTIFACT_DEBUG, INSPECT_ARTIFACT, RUN_IN_ARTIFACT, FOLDER_LIST, FOLDER_READ, FOLDER_SEARCH, FOLDER_WRITE, FOLDER_EDIT,)
FAMILIES = (ASK_FAMILY, SAVE_FAMILY, ARTIFACTS_FAMILY, ARTIFACT_DEBUG_OFFER_FAMILY, ARTIFACT_DEBUG_FAMILY, FOLDER_FAMILY,)
