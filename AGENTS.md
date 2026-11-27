# Règles pour les agents

Ce fichier s'adresse à tout agent (ou personne) qui modifie ce dépôt. Il fige
les règles du code, de la documentation et des tests ; la vue d'ensemble du
code est dans `docs/architecture.md`, la vérification complète dans
`docs/verification.md`.

## Code

### Pas de code rapiécé

- **Réutiliser avant d'ajouter.** Avant d'écrire un mécanisme (téléchargement,
  cache, verrou, appel à l'exécuteur, règle de magasin, extrait autour d'une
  correspondance…), chercher celui qui existe : `tools/net.py`
  (`download`, `post`), `tools/webcache.py` (`Cache`), `tools/pattern.py`,
  `tools/code_execution.py` (`execute`, `destroy`, `sessions`…),
  `storage/owned.py` (magasins par compte), `ui/conversation_routes.run`. Lui
  ajouter un paramètre plutôt qu'en écrire une copie.
- **Une approche abandonnée part entièrement** : son code, ses réglages, ses
  tests, sa doc, ses commentaires. Après une réécriture, chercher ses noms
  (`grep`) dans tout le dépôt.
- Une fonction qui a grossi par ajouts successifs (filets d'exception
  emboîtés, cas particuliers en série) se réécrit en un seul chemin, plutôt
  que de recevoir une rustine de plus.
- Suivre les conventions du module voisin : forme des erreurs, unités (les
  dates de la page en secondes ou en millisecondes selon le cas, comme le
  voisin), nommage (`tools/<nom de l'outil>.py`, le pluriel pour le
  magasin), textes des outils en anglais, le reste en français.
- Pas d'accès aux attributs privés d'un autre module : lui ajouter la méthode
  qui manque.
- Pas de constante en dur côté page quand le proxy la connaît (une durée, une
  borne) : la route la rend.

### Avant de livrer

- Les trois suites passent (voir *Tests*).
- Une relecture indépendante du diff (un autre agent, à qui l'on donne le
  code, pas ses conclusions) : correction, sécurité, restes, duplications.
  Ses constats sont corrigés ou tranchés avant le commit.
- Les données du déploiement ne se touchent qu'avec une copie de sauvegarde
  (`config.toml.avant-<sujet>-AAAAMMJJ`), et un nettoyage de données de
  production se fait par une migration du code (une fois, notée), pas à la
  main.

## Documentation

La doc (`README.md`, `docs/*.md`, les commentaires de
`data/config.example.toml`) décrit **l'état actuel**, pour qui installe,
configure ou utilise le proxy.

- **Pas d'historique dans la doc de référence** : ni « depuis v… », ni
  « avant / maintenant », ni « une base d'avant… ». L'historique va dans le
  message de commit et les notes de release. Seule exception : une consigne
  de mise à jour dont un admin a besoin pour agir, en une phrase.
- **Une règle, une page.** Chaque sujet a sa page de référence ; ailleurs, un
  renvoi (lien vers l'ancre), pas une copie. Avant d'écrire, chercher où le
  sujet est déjà décrit (`grep` dans `docs/`) et compléter là.
- **Vérifiée contre le code** : les durées, bornes, défauts, noms de réglages
  et comportements se relisent dans le code avant publication. Toute
  modification qui change un comportement ou un réglage met à jour sa page de
  référence dans le même lot. Une valeur réglable se cite par son réglage
  (`[repos].cache_ttl`, 30 min par défaut), pas seulement par sa valeur.
- **Courte et factuelle** : français technique, neutre et direct ; éviter les
  tournures conversationnelles ou artificielles et l'emphase. Conserver les
  contrats, bornes, valeurs par défaut, commandes, exemples et détails utiles ;
  ne pas sacrifier l'exactitude à la concision. Les noms de fonctions internes
  vont dans `docs/architecture.md` seulement. Une liste énumère des éléments,
  elle ne découpe pas une explication.
- **Mise en forme Markdown** : prose d'environ 80 colonnes, hors tableaux,
  code et texte alternatif d'images. Respecter les lignes blanches entre
  paragraphes, titres, listes et tableaux ; vérifier le rendu, notamment des
  tableaux.
- **Aucun nom tiré d'un déploiement réel** (hôtes internes, groupes ou projets
  d'une entreprise, adresses e-mail) : des exemples génériques
  (`gitlab.exemple.fr`, `equipe/*`).
- Tout nouveau réglage est commenté dans `data/config.example.toml` (un test
  l'exige) et décrit dans `docs/configuration.md` ; toute nouvelle page est
  citée dans `README.md` (tableau des pages) et reliée depuis les pages
  voisines.
- Conserver les titres et ancres existants. Si un titre doit changer, mettre à
  jour tous les liens qui y pointent ; vérifier les liens et ancres internes
  avant de rendre la main.

## Tests

### Avant de rendre la main

Les trois suites passent, sans réseau :

```sh
(cd proxy && ../.venv/bin/python -m pytest -q)
(cd services/executor && ../../.venv/bin/python -m pytest -q tests)
(cd proxy/wasm && npm run check)
```

Un test qui casse à cause d'un changement de production fait par quelqu'un
d'autre se signale ; il ne se contourne pas et ne s'affaiblit pas.

### Ce qu'un test vérifie

- Un **comportement qui compte** : contrat d'API (statut, code d'erreur, forme
  du corps), sécurité, cloisonnement entre comptes, bornes, cas d'erreur réels.
- Pas un détail d'implémentation. Ni attribut ou fonction privés (`_sessions`,
  `_loading`, `_generation`, compteurs internes), ni texte exact d'un message
  quand ce texte n'est pas un contrat. Passer par l'entrée publique : la route,
  l'outil (`TOOL.run`), la fonction du magasin.
- Un test doit **pouvoir échouer**. Pas de test sans assertion, pas
  d'assertion lâche (`status in (200, 400)`), pas de cas qui passerait aussi
  sans le code qu'il est censé protéger. Pour un mécanisme subtil (verrou,
  cache, borne), vérifier une fois que le test échoue sans lui.
- Toute ressource par compte a son cas « un autre compte » : il ne la voit
  pas, ne la modifie pas, ne la supprime pas.
- Un secret (jeton, clé, URL de webhook) n'apparaît jamais dans ce qui est
  rendu : le vérifier sur la sortie, pas sur le stockage.

### Combien, et où

- **Avant d'ajouter un test, chercher celui qui couvre déjà le comportement**
  et l'étendre. Un nouveau test n'existe que pour un comportement nouveau.
- Un comportement = un test. Fusionner les petits fragments d'un même
  comportement ; découper un test qui vérifie plusieurs choses sans lien.
- Plusieurs entrées, même attente : une boucle avec le cas dans le message
  d'assertion (`assert ..., cas`), plutôt qu'un `parametrize` qui gonfle le
  compte. `parametrize` reste utile quand chaque cas demande son propre
  montage.
- Un test va dans le fichier de son module et dans sa section (`# ── … ──`),
  jamais en vrac en fin de fichier.
- Le nom et la docstring disent le comportement vérifié, pas la fonction
  appelée (`test_un_compte_ne_touche_pas_aux_webhooks_d_un_autre`, pas
  `test_update_in`). Chaque fichier garde sa langue.

### Les tests de régression se gardent

Un test qui protège contre un bug déjà corrigé ne se supprime pas ; il peut
se condenser. Entre autres : jeton non suivi par une redirection vers un
autre hôte, archive piégée (liens, `..`, trop d'entrées), regex à retour
arrière catastrophique, boucle sortant → entrant, corps Slack mal typés,
409 du sortant d'un passe plat, réponse « en cours » remplacée, branche par
défaut invalide, borne du pack git, migrations des bases d'avant.

Corriger un bug, c'est d'abord écrire le test qui échoue.

### Montage

- Ce qui est commun vit dans `proxy/tests/fakes.py` (fonctions) et
  `proxy/tests/conftest.py` (fixtures, dont `proxy`). Un montage recopié une
  troisième fois devient un helper ou une fixture du fichier ; utilisé par
  plusieurs fichiers, il monte dans `fakes.py` ou `conftest.py`. Ne pas
  importer une fixture d'un autre `test_*.py`.
- **Aucun réseau** : DNS, web, forge, backend et exécuteur sont des doublures
  (`fake_dns`, `FakeWeb`, `FakeUpstream`, `httpx2.MockTransport`). Une doublure
  qui exécute le vrai code (le script du bac, le faux podman) vaut mieux
  qu'une réponse en dur.
- Remplacer le moins possible : la frontière extérieure (`net.post`,
  `relaying.send_upstream`, le transport), pas une fonction interne du module
  testé.
- **Pas d'attente fragile** : ni `sleep` pour laisser « le temps de », ni seuil
  sur une durée réelle. Décaler l'horloge (`monotonic` remplacé) ou attendre
  un état observable.
- Chaque test repart d'un état neuf : bases sous `tmp_path`, réglages par
  `monkeypatch` (`fakes.everywhere` pour un réglage recopié dans les parties
  d'un paquet), jamais `data/`.
- Les imports sont en tête de fichier.
