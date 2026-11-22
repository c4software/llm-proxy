"""
Les outils du NAVIGATEUR — les bacs de code : JavaScript et TypeScript, PHP,
SQL, PostgreSQL, Linux, programmes d'outils.
Leur DÉCLARATION (Runs.BROWSER) : le prompt, le schéma, la famille du
menu Outils. Leur exécution est dans la page, chat/tools/sandbox/
(docs/navigateur.md, « Écrire un outil de la page »).
"""

from ..contract import BrowserTool, Family, FamilyKey, ToolName
from .common import CONTEXT, GREP, MAX_CONTEXT, MAX_SLICE, SLICE, SLICE_LINES

# Délais (s) d'un appel, que le prompt dit et que la page applique.
JAVASCRIPT_TIMEOUT = 15
TYPESCRIPT_TIMEOUT = 60      # le premier appel charge le compilateur
PHP_TIMEOUT = 30             # le premier appel compile le runtime
SHELL_TIMEOUT = 60           # run_shell, par défaut
SHELL_MAX_TIMEOUT = 600
PROGRAM_TIMEOUT = 120
MAX_ROWS = 200               # lignes montrées par résultat (SQL, PostgreSQL)
MAX_CALLS = 20               # appels d'outils par programme
CONCURRENCY = 4              # … à la fois


# ── JavaScript, TypeScript ────────────────────────────────────────────

RUN_JAVASCRIPT = BrowserTool(
    ToolName.RUN_JAVASCRIPT,
    description=('Exécute du JavaScript dans un bac isolé du navigateur (un Worker : aucun '
                 'réseau, aucun accès à la page ni au DOM), pour calculer exactement, '
                 'transformer des données, produire un fichier ou un graphique. Le code est le '
                 "corps d'une fonction async : `return` la valeur voulue ; console.log est "
                 'rendu aussi. Fonctions fournies : readText(chemin) et readBytes(chemin) '
                 'lisent les fichiers passés dans `files` (dossier ouvert ou pièces jointes, '
                 "qui restent sur l'appareil) ; output(nom, données) produit un fichier sous "
                 'la réponse (texte, Uint8Array, Blob, OffscreenCanvas en PNG) ; chart(config, '
                 '{width, height, name}) dessine un graphique Chart.js 4 (config : {type, '
                 'data, options}, sans fonctions), montré INTERACTIF dans la réponse (survol, '
                 'légende) et joint en PNG au résultat si le modèle lit les images ; le '
                 'résultat dit comment le placer dans le texte. Une image produite par output '
                 'est montrée sous la réponse. `language: "typescript"` accepte du TypeScript '
                 f'(types retirés, non vérifiés : check_typescript les vérifie). {JAVASCRIPT_TIMEOUT} s au plus.'),
    params={
        'code': {'type': 'string', 'description': 'Le corps de la fonction (avec `return`).'},
        'language': {
            'type': 'string',
            'enum': ['javascript', 'typescript'],
            'description': 'Le langage du code (défaut : javascript).',
        },
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': ('Fichiers à fournir au code : chemins dans le dossier ouvert, ou noms de pièces '
             'jointes.'),
        },
    },
    required=('code',),
    settings={'timeout': JAVASCRIPT_TIMEOUT},
)

CHECK_TYPESCRIPT = BrowserTool(
    ToolName.CHECK_TYPESCRIPT,
    description=('Vérifie les types de fichiers TypeScript du dossier ouvert (ou de pièces '
                 'jointes) avec le compilateur TypeScript 6, dans un bac isolé du navigateur, '
                 'sans rien exécuter. Le tsconfig.json à la racine du dossier est appliqué '
                 '(sinon : strict, ES2022, modules ESNext) ; les imports relatifs sont résolus '
                 'dans le dossier ; les paquets (pas de node_modules) valent `any`. Rend les '
                 'erreurs « fichier(ligne,colonne): error TSxxxx: message » (100 au plus). '
                 '`code` seul : vérifier un extrait (celui de la conversation, sans dossier) ; '
                 "`code` + `path` : une version modifiée d'un fichier, avant de l'écrire. "
                 f'{TYPESCRIPT_TIMEOUT} s au plus (le premier appel charge le compilateur).'),
    params={
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': 'Fichiers ou sous-dossiers à vérifier (« . » : tout le dossier).',
        },
        'code': {
            'type': 'string',
            'description': ('Un texte TypeScript à vérifier : seul (sans dossier ouvert, un extrait de la '
             'conversation), ou à la place du fichier `path`.'),
        },
        'path': {
            'type': 'string',
            'description': 'Le chemin de ce texte dans le projet (défaut : extrait.ts).',
        },
    },
    settings={'timeout': TYPESCRIPT_TIMEOUT},
)

JAVASCRIPT_FAMILY = Family(
    FamilyKey.JAVASCRIPT,
    label='JavaScript, TypeScript',
    tools=(ToolName.RUN_JAVASCRIPT, ToolName.CHECK_TYPESCRIPT),
    hint='du code dans un bac isolé, sans réseau : fichiers, graphiques ; vérifier les types',
    authored=True,
    guidance=("Pour un calcul exact, un traitement de données de l'utilisateur ou un "
              'graphique, exécuter du code avec run_javascript (chart() pour un graphique) '
              'plutôt que de calculer de tête ; pas pour mettre en forme une réponse. Avant de '
              "proposer une modification d'un fichier TypeScript du dossier, la vérifier avec "
              'check_typescript (`code` et `path`).'),
)


# ── PHP ───────────────────────────────────────────────────────────────

RUN_PHP = BrowserTool(
    ToolName.RUN_PHP,
    description=('Exécute un script PHP 8.3 (CLI, sans serveur web) dans un bac isolé du '
                 "navigateur, sans réseau : pour tester une fonction, montrer ce qu'affiche du "
                 'code PHP, calculer, transformer un fichier. Les fichiers de `files` (dossier '
                 "ouvert ou pièces jointes, qui restent sur l'appareil) sont copiés sous "
                 '/data, le dossier courant, avec leurs chemins (include et file_get_contents '
                 'les trouvent) ; ce que le script écrit sous /out (texte, ou image png/jpeg, '
                 'GD est là) est rendu sous la réponse. Rend la sortie (echo, print_r, '
                 'var_dump), les erreurs et le code de sortie. Chaque appel part de zéro. '
                 f'{PHP_TIMEOUT} s au plus.'),
    params={
        'code': {'type': 'string', 'description': "Le script, `<?php` au début (ajouté s'il manque)."},
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': ('Fichiers à fournir au code : chemins dans le dossier ouvert, ou noms de pièces '
             'jointes.'),
        },
    },
    required=('code',),
    settings={'timeout': PHP_TIMEOUT},
)

PHP_FAMILY = Family(
    FamilyKey.PHP,
    label='PHP',
    tools=(ToolName.RUN_PHP,),
    hint='PHP 8.3 en WASM, dans un bac isolé (≈ 18 Mo au premier appel)',
    authored=True,
)


# ── SQL ───────────────────────────────────────────────────────────────

RUN_SQL = BrowserTool(
    ToolName.RUN_SQL,
    description=('Exécute un script SQL (dialecte SQLite 3) dans un bac isolé du navigateur, '
                 "sur la base de la conversation : ses tables restent d'un appel à l'autre "
                 "(`reset` repart d'une base vide). `files` importe d'abord des fichiers en "
                 'tables, depuis le dossier ouvert ou les pièces jointes : CSV/TSV (en-tête, '
                 "séparateur et types devinés), JSON (tableau d'objets), JSONL, ou une base "
                 ".sqlite/.db — leurs données restent sur l'appareil, seuls les résultats des "
                 'requêtes reviennent. Une table importée porte le nom du fichier, ses '
                 "colonnes ceux de l'en-tête (minuscules, sans accents, « _ ») ; l'import rend "
                 'son schéma. Revoir les tables : SELECT name, sql FROM sqlite_schema. Rend '
                 f'chaque résultat en tableau ({MAX_ROWS} lignes au plus) : agréger en SQL plutôt que '
                 'tout lire.'),
    params={
        'sql': {
            'type': 'string',
            'description': ('Le script : une ou plusieurs instructions séparées par `;` (vide : seulement '
             'importer).'),
        },
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': ('Fichiers à importer avant le script : chemins dans le dossier ouvert, ou noms de '
             'pièces jointes.'),
        },
        'reset': {
            'type': 'boolean',
            'description': 'Vider la base de la conversation avant (défaut : non).',
        },
        'download': {
            'type': 'boolean',
            'description': "Proposer à l'utilisateur de télécharger la base (.sqlite) sous la réponse.",
        },
    },
    settings={'max_rows': MAX_ROWS},
)

SQL_FAMILY = Family(
    FamilyKey.SQL,
    label='SQL',
    tools=(ToolName.RUN_SQL,),
    hint='SQLite en WASM : une base par conversation, import de CSV, JSON, .sqlite',
    authored=True,
    guidance=("Pour analyser des données de l'utilisateur (CSV, JSON, base SQLite, dans le "
              'dossier ouvert ou en pièce jointe), préférer run_sql avec `files` à leur '
              "lecture : les données restent sur l'appareil, seuls les résultats des requêtes "
              "reviennent. Les tables de la conversation restent d'un message à l'autre."),
)


# ── PostgreSQL ────────────────────────────────────────────────────────

RUN_POSTGRES = BrowserTool(
    ToolName.RUN_POSTGRES,
    description=('Exécute un script SQL PostgreSQL (PGlite : PostgreSQL complet en WASM, '
                 'PL/pgSQL, JSONB, CTE, fonctions fenêtres, types…) dans un bac isolé du '
                 'navigateur, sur la base PostgreSQL de la conversation : ses tables restent '
                 "d'un appel à l'autre (`reset` repart d'une base vide). `files` importe "
                 "d'abord des fichiers du dossier ouvert ou des pièces jointes : CSV/TSV, "
                 "JSON, JSONL en tables (nom du fichier ; colonnes de l'en-tête en minuscules "
                 'sans accents ; bigint, double precision ou text déduits), ou un script .sql '
                 "exécuté tel quel — leurs données restent sur l'appareil, seuls les résultats "
                 "reviennent. Le script est une transaction implicite : une erreur l'annule "
                 f'(sauf COMMIT explicite). Rend chaque résultat en tableau ({MAX_ROWS} lignes au '
                 'plus) et les messages RAISE NOTICE. Revoir les tables : SELECT table_name '
                 "FROM information_schema.tables WHERE table_schema = 'public'."),
    params={
        'sql': {
            'type': 'string',
            'description': ('Le script : une ou plusieurs instructions séparées par `;` (vide : seulement '
             'importer).'),
        },
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': ('Fichiers à importer avant le script : chemins dans le dossier ouvert, ou noms de '
             'pièces jointes.'),
        },
        'reset': {
            'type': 'boolean',
            'description': 'Vider la base PostgreSQL de la conversation avant (défaut : non).',
        },
    },
    settings={'max_rows': MAX_ROWS},
)

POSTGRES_FAMILY = Family(
    FamilyKey.POSTGRES,
    label='PostgreSQL',
    tools=(ToolName.RUN_POSTGRES,),
    hint=('PostgreSQL en WASM (PGlite) : une base par conversation, import de CSV, JSON, .sql '
          '(≈ 17 Mo au premier appel)'),
    authored=True,
    guidance=('run_postgres a sa propre base PostgreSQL par conversation, distincte de celle '
              "de run_sql (SQLite) : l'utiliser quand l'utilisateur veut du PostgreSQL (son "
              'dialecte, PL/pgSQL, JSONB, un schéma à tester avant de le déployer).'),
)


# ── Linux ─────────────────────────────────────────────────────────────

RUN_SHELL = BrowserTool(
    ToolName.RUN_SHELL,
    description=('Exécute un script bash dans une VM Linux du navigateur (Alpine, x86 32 bits, '
                 "émulée en WASM), sans réseau sauf si l'utilisateur l'ouvre : bash, les "
                 'outils GNU (coreutils, findutils, grep, sed, diff, patch), python3 et sa '
                 'bibliothèque standard, git, jq, sqlite3, make, bc, curl, htop, less, tree, '
                 "xz, et busybox. Rien ne s'installe (ni apk, ni pip). Le dossier courant est "
                 "/work, gardé avec la conversation d'un appel à l'autre ; l'utilisateur voit "
                 'et utilise la même machine dans le panneau Terminal. Les fichiers de `files` '
                 "(dossier ouvert ou pièces jointes, qui restent sur l'appareil) sont copiés "
                 'sous /work à leur chemin ; les fichiers que le script écrit sous /out '
                 "pendant l'appel (texte, ou image png/jpeg/webp/gif) sont rendus sous la "
                 'réponse, et restent dans la VM (/out est /work/out, gardé avec /work). Rend '
                 "la sortie (stdout et stderr mêlés) et le code de sortie. Quand l'utilisateur "
                 'a ouvert le panneau Terminal, la commande y est tapée sous ses yeux (« lance '
                 '… », « montre-moi … ») — un script de plusieurs lignes en un bloc ( … ), un '
                 "sous-shell : il la voit s'exécuter ; une commande qui dure au-delà du délai "
                 '(un serveur, un programme interactif) reste alors dans son terminal. Émulée, '
                 'la machine est lente : quelques secondes pour un gros calcul Python.'),
    params={
        'script': {'type': 'string', 'description': 'Le script bash, exécuté dans /work.'},
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': ('Fichiers à fournir au code : chemins dans le dossier ouvert, ou noms de pièces '
             'jointes.'),
        },
        'timeout': {
            'type': 'number',
            'description': (f'Délai en secondes (défaut {SHELL_TIMEOUT}, {SHELL_MAX_TIMEOUT} au plus) ; '
                            'le script est tué au-delà.'),
        },
        'reset': {
            'type': 'boolean',
            'description': "Repartir d'une machine neuve et d'un /work vide (défaut : non).",
        },
        'terminal': {
            'type': 'boolean',
            'description': ("Taper la commande dans le terminal de l'utilisateur s'il est ouvert (défaut : "
             "oui) ; false : l'exécuter sans la montrer."),
        },
    },
    required=('script',),
    settings={'timeout': SHELL_TIMEOUT, 'max_timeout': SHELL_MAX_TIMEOUT},
)

SHELL_READ = BrowserTool(
    ToolName.SHELL_READ,
    description=('Lit un fichier texte de la VM Linux : entier, ou une tranche de lignes '
                 'numérotées (`start_line`, `end_line`) — à préférer pour un gros fichier, '
                 'après shell_grep qui dit où chercher. Avant shell_edit quand le texte exact '
                 "n'est plus sûr."),
    params={
        'path': {'type': 'string', 'description': 'Le chemin du fichier : absolu, ou relatif à /work.'},
        'start_line': {
            'type': 'integer',
            'description': 'Première ligne (à partir de 1) ; absente avec end_line : le fichier entier.',
        },
        'end_line': {
            'type': 'integer',
            'description': f'Dernière ligne, incluse (défaut : {SLICE_LINES} lignes ; {MAX_SLICE} au plus).',
        },
    },
    required=('path',),
    settings=SLICE,
)

SHELL_GREP = BrowserTool(
    ToolName.SHELL_GREP,
    description=('Cherche dans les fichiers texte de la VM Linux, sans les relire en entier : '
                 'rend les lignes qui contiennent `pattern` (texte, ou expression régulière '
                 'JavaScript avec `regex`), numérotées, avec `context` lignes autour, fichier '
                 'par fichier. `path` : un fichier, ou un dossier (récursif, fichiers et '
                 'dossiers cachés exclus ; défaut /work). Pour retrouver un passage avant de '
                 'le lire (shell_read sur ses lignes) puis de le retoucher (shell_edit).'),
    params={
        'pattern': {
            'type': 'string',
            'description': 'Le texte cherché (ou une expression régulière si `regex`).',
        },
        'path': {
            'type': 'string',
            'description': 'Un fichier ou un dossier : absolu, ou relatif à /work (défaut : /work).',
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
    required=('pattern',),
    settings=GREP,
)

SHELL_EDIT = BrowserTool(
    ToolName.SHELL_EDIT,
    description=('Retouche un fichier texte de la VM Linux SANS le réécrire : chaque '
                 '`old_string`, un passage exact et unique du fichier (espaces et retours à la '
                 'ligne compris, assez long pour être unique), est remplacé par `new_string` — '
                 'ou, avec `regex`, une expression régulière (un changement répétitif : '
                 "`replace_all`). Toutes les retouches s'appliquent, ou aucune. Pour trouver "
                 'le passage : shell_grep, puis shell_read sur ses lignes. Plutôt que sed dans '
                 'run_shell.'),
    params={
        'path': {'type': 'string', 'description': 'Le chemin du fichier : absolu, ou relatif à /work.'},
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
    },
    required=('path', 'edits'),
)

SHELL_WRITE = BrowserTool(
    ToolName.SHELL_WRITE,
    description=('Écrit un fichier texte entier dans la VM Linux : le crée (dossiers compris), '
                 'ou le remplace. Pour un nouveau fichier, ou une réécriture complète ; pour '
                 "une modification, shell_edit. Sans les guillemets et l'échappement d'un "
                 'heredoc dans run_shell.'),
    params={
        'path': {'type': 'string', 'description': 'Le chemin du fichier : absolu, ou relatif à /work.'},
        'content': {'type': 'string', 'description': 'Le contenu complet du fichier.'},
    },
    required=('path', 'content'),
)

LINUX_FAMILY = Family(
    FamilyKey.LINUX,
    label='Linux',
    tools=(ToolName.RUN_SHELL, ToolName.SHELL_READ, ToolName.SHELL_GREP, ToolName.SHELL_EDIT, ToolName.SHELL_WRITE),
    hint=('Une VM Linux en WASM (Alpine : bash, python3, git, outils GNU…), sans réseau, '
          'partagée avec le panneau Terminal'),
    authored=True,
    guidance=("run_shell exécute du bash dans une VM Linux du navigateur. Ne s'en servir que "
              "si la demande exige d'exécuter quelque chose : lancer ou tester un script shell "
              'ou Python (bibliothèque standard seulement), traiter des fichiers déjà dans la '
              'VM. Pas pour rédiger une réponse, des notes, un tableau ou un document : '
              'répondre directement, ou passer par un artifact. /work est gardé avec la '
              "conversation ; l'utilisateur voit la même machine dans le panneau Terminal. "
              'Pour modifier un fichier de la VM : shell_grep (où ?), shell_read sur ses '
              'lignes, shell_edit — plutôt que sed ou un heredoc dans run_shell ; shell_write '
              'pour un fichier neuf. Un contenu de fichier plus ancien dans la conversation '
              'peut être périmé : shell_read rend le fichier actuel.'),
)


# ── Programmes d'outils ───────────────────────────────────────────────

RUN_TOOLS_PROGRAM = BrowserTool(
    ToolName.RUN_TOOLS_PROGRAM,
    description=('Exécute un court programme JavaScript qui appelle les outils hébergés de '
                 'cette conversation (recherche web, lecture de pages, mémoire…) et combine '
                 'leurs résultats, en UNE étape : seul ce que le programme rend ou écrit sur '
                 'la console te revient, pas chaque résultat intermédiaire. Pour une tâche de '
                 'plusieurs appels (chercher puis lire plusieurs pages, filtrer de longs '
                 "résultats) ; pour un seul appel, appelle l'outil directement. Le code est le "
                 "corps d'une fonction async : `await tools.<nom>(args)` appelle l'outil "
                 'hébergé <nom> avec les MÊMES arguments que sa fonction (un objet), et rend '
                 "son texte ; un échec lève une Error dont `code` est le code d'erreur "
                 '(not_allowed, limit…), à attraper pour continuer. `Promise.all` pour des '
                 f'appels indépendants ; `return` la valeur voulue. {MAX_CALLS} appels par programme, '
                 f'{CONCURRENCY} à la fois, {PROGRAM_TIMEOUT} s au plus ; attendre (await) chaque appel '
                 'avant de rendre : '
                 'ceux qui restent en cours à la fin du programme sont coupés. Aucun réseau ni '
                 'API du navigateur : seulement ces outils.'),
    params={'code': {'type': 'string', 'description': 'Le corps de la fonction (avec `return`).'}},
    required=('code',),
    settings={'max_calls': MAX_CALLS, 'concurrency': CONCURRENCY, 'timeout': PROGRAM_TIMEOUT},
)

PROGRAMS_FAMILY = Family(
    FamilyKey.PROGRAMS,
    label="Programmes d'outils",
    tools=(ToolName.RUN_TOOLS_PROGRAM,),
    hint='enchaîner les outils hébergés cochés dans un programme JavaScript, en une étape',
    authored=True,
    guidance=('Pour une tâche qui demande plusieurs appels aux outils hébergés (chercher puis '
              'lire plusieurs pages, comparer, filtrer), écrire un programme run_tools_program '
              "qui les enchaîne, plutôt qu'un appel par tour. Ne pas deviner d'URL : chercher "
              "d'abord, puis lire celles que la recherche a rendues."),
)


TOOLS = (RUN_JAVASCRIPT, CHECK_TYPESCRIPT, RUN_PHP, RUN_SQL, RUN_POSTGRES, RUN_SHELL, SHELL_READ, SHELL_GREP, SHELL_EDIT, SHELL_WRITE, RUN_TOOLS_PROGRAM,)
FAMILIES = (JAVASCRIPT_FAMILY, PHP_FAMILY, SQL_FAMILY, POSTGRES_FAMILY, LINUX_FAMILY, PROGRAMS_FAMILY,)
