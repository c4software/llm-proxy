# Limites connues

Ce que le proxy ne fait pas, et ce qui n'a pas été validé. Chaque section
renvoie à la page qui détaille.

## Général

- **Pas de cache côté proxy.** `cache_control` est ignoré ; le cache KV
  appartient au serveur d'inférence. Un backend à cache de préfixe (vLLM,
  llama.cpp) en profite si le début de la conversation ne change pas. Le
  proxy y veille (sérialisation stable, `system` en tête, injections
  fixes) et le mesure (`input_cached_tokens`, colonne *Cache*). Un client
  Anthropic reçoit `cache_read_input_tokens`, exclu d'`input_tokens`.
  Albert facture à chaque tour le prompt système d'un client agent
  (~20 k tokens pour Claude Code).
- **PDF** (`document` base64) : remplacé par un texte.
- **Images** : seul le catalogue du backend décide. Un modèle vision
  servi sans `--mmproj` est un modèle texte.
- **Hors périmètre, volontairement** : Batches, Files, outils serveur
  Anthropic autres que recherche et lecture de page (`code_execution`…),
  backend Anthropic.

## Surface Responses ([API OpenAI Responses](api-responses.md))

- Seul `web_search` est exécuté, s'il est activé. Les autres outils
  hébergés (`file_search`, `code_interpreter`, `mcp`,
  `image_generation`…) sont ignorés, invisibles du modèle.
- `custom` joué une fois avec un vrai client, `local_shell` jamais. La
  grammaire d'un `custom` n'est pas imposée au décodage ; son entrée
  arrive en un seul delta.
- Autres outils intégrés (`shell`, `apply_patch` natif, `computer_use`…)
  ignorés.
- Pas de `ping` pendant l'attente du quota du **premier** tour. Ensuite
  (recherche, tours suivants), un commentaire SSE `: ping` tient la
  connexion.
- La mémoire des résultats ne survit pas à un redémarrage.

## Outils hébergés sur `/v1/chat/completions`

Voir [Client chat/completions](outils-heberges.md#client-chatcompletions--déclarer-loutil).

- Le client ne garde pas les appels hébergés. Le proxy les réinsère tant
  que sa mémoire (`[chat].memory`) les garde et que l'historique revient
  inchangé.
- Perdus après compaction ou troncature, redémarrage du proxy, ou si le
  client ne déclare plus l'outil. Le modèle oublie ce qu'il avait lu, et
  le cache de préfixe s'arrête avant la recherche.
- Pas de `ping` pendant l'attente du quota du premier tour. `n` > 1
  refusé. Pas joué contre un client réel.
- Avec `[chat].always`, les outils vont aussi aux requêtes de service
  (titre, tags), qu'on n'exclut que par leur modèle
  (`[chat].always_except`), et aux modèles qui ne savent pas appeler
  d'outils. Voir
  [Outils présentés d'office](outils-heberges.md#outils-présentés-doffice).

## Exécution de code ([détails](execution-de-code.md))

- Validé sur une seule machine (Docker 29.7.1, noyau 6.1, cgroup v2, sans
  AppArmor). Pas joués : liens `/v1/files` dans un navigateur, une
  interface de chat réelle, arm64, `cap_drop`.
- Bornes par bac (mémoire, CPU, processus) seulement si le cgroup est
  délégué (`writable-cgroups=true`, Docker 28+). Sinon, plafond du
  conteneur exécuteur seul.
- Exécuteur sans seccomp, AppArmor, masquage de `/proc` ni
  `no-new-privileges`.
- Un processus peut tourner jusqu'à la fin de son bac.
- Absent de `/v1/responses` et `/v1/messages`.
- Historique modifié ou proxy redémarré : bac neuf. Fichiers rendus en
  mémoire vive.

## Génération d'images ([détails](outils-heberges.md#génération-dimages))

- Jouée contre un seul backend (gufo, Qwen-Image, 512×512, `b64_json`).
  Pas joués : `url` rendue par le backend, backend à quotas, retouche
  (`edits`), une interface de chat réelle.
- Une image par appel, pas de `quality`, `n`, `background`… Le modèle ne
  voit pas l'image produite.
- Absente de `/v1/responses` et `/v1/messages`. Image en mémoire vive.

## Recherche et lecture de page

- Sur `/v1/messages`, chacune n'est branchée que si le client déclare son
  outil serveur. Pas de citations ni de `pause_turn`, `user_location`
  ignoré, listes de domaines appliquées en filtre sur les résultats de
  SearXNG. Voir
  [L'outil serveur `web_search`](outils-heberges.md#loutil-serveur-web_search).
- L'outil serveur `web_fetch` ne vérifie pas que l'URL figurait dans la
  conversation, rend un PDF en texte, et n'a été joué contre aucun client
  réel. Voir [L'outil serveur `web_fetch`](outils-heberges.md#loutil-serveur-web_fetch).
- `web_fetch` n'exécute pas le JavaScript et ne fait pas d'OCR (d'un PDF,
  seule la couche texte est lue).

État de la validation : voir [Outils hébergés](outils-heberges.md).
