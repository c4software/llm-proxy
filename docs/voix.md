# Voix : synthèse, conception et clonage

Le proxy route les requêtes audio vers le backend indiqué par le préfixe du
modèle : `POST /v1/audio/speech` (synthèse),
`POST /v1/audio/transcriptions` (transcription, multipart compris) et
`GET /v1/audio/voices?model=<backend>/<modèle>` (voix disponibles pour un
modèle). Les modèles **Qwen3-TTS** d'un backend gufo permettent de lire avec
une voix prédéfinie, de **concevoir** une voix à partir d'une description ou
de la **cloner** à partir d'un court enregistrement. Les commandes `curl`
ci-dessous montrent l'utilisation directe de ces API. Le chat prend aussi
en charge ces opérations et leur normalisation ([Chat](chat.md), *Voix* et
*Voix clonées*).

## Mise en route

`/v1/audio/speech` doit figurer dans `[proxy].forward_post_paths` (il y est
dans `data/config.example.toml`, mais un `data/config.toml` plus ancien ne
l'a pas toujours : sans lui, la synthèse répond `404 unknown_route`).
`/v1/audio/transcriptions` aussi pour la transcription.

```bash
export URL=http://localhost:8000 KEY=…   # clé du proxy, si [proxy].api_keys en demande une
export B=silentchuck                     # préfixe du backend gufo
TEXT="Il était une fois, au bord d'une forêt immense, un vieux meunier qui vivait seul avec son chat. Chaque matin, il ouvrait les volets de son moulin et regardait le soleil se lever sur les champs de blé."
```

Les corps JSON sont générés avec `jq` : l'enregistrement encodé en base64
approche le mégaoctet et est trop volumineux pour une ligne de commande.

## Les modèles

| Modèle | Rôle | Exige |
|---|---|---|
| `qwen3-tts-12hz-1.7b-customvoice` | 9 voix toutes faites, ton réglable (`instructions`) | `voice` |
| `qwen3-tts-12hz-1.7b-voice-design` | invente une voix d'après une description | `instructions` non vide (la description) |
| `qwen3-tts-12hz-1.7b-base` | lit avec la voix d'un enregistrement | `reference_audio` (WAV en base64) et `reference_text` (sa transcription) |
| `qwen3-asr-1.7b` | transcription (audio → texte) : **pas** de synthèse | |

Les trois modèles de synthèse prennent en charge 10 langues, dont le
français. Définir `"language": "French"` pour éviter la détection de langue
à chaque requête. Le format sûr est `"response_format": "wav"`.

Le proxy relaie sans modification les erreurs du backend :

| Message | Cause |
|---|---|
| « Qwen3-TTS audio service is not configured » | le modèle n'est pas un modèle de synthèse (l'ASR, par exemple) |
| « CustomVoice requests require a 'voice' speaker » | `voice` manquant |
| « VoiceDesign requests require non-empty 'instructions' » | description manquante |
| « Base voice cloning requires base64 WAV 'reference_audio' » | enregistrement manquant |
| « Base ICL voice cloning requires non-empty 'reference_text' » | transcription manquante |
| `backend_offline` « backend … hors ligne » | le backend charge un modèle (voir [Un modèle à la fois](#un-modèle-à-la-fois)) |
| `unknown_backend_prefix` | modèle sans préfixe de backend |

## Voix toutes faites

```bash
curl -sS "$URL/v1/audio/voices?model=$B/qwen3-tts-12hz-1.7b-customvoice" \
  -H "Authorization: Bearer $KEY" | jq -c '[.data[].id]'
```

| Voix | Description (fiche du modèle) | Langue d'origine |
|---|---|---|
| `uncle_fu` | homme mûr, grave et doux | chinois |
| `ryan` | homme, dynamique, très rythmé | anglais |
| `aiden` | homme, solaire, américain, médium clair | anglais |
| `dylan` | jeune homme, clair et naturel | chinois (Pékin) |
| `eric` | homme, vif, un peu voilé | chinois (Sichuan) |
| `vivian` | jeune femme, brillante | chinois |
| `serena` | jeune femme, chaude et douce | chinois |
| `ono_anna` | femme, enjouée et légère | japonais |
| `sohee` | femme, chaude et expressive | coréen |

Les voix prédéfinies ne sont pas francophones d'origine, mais elles lisent
toutes le français. Lors des essais, `qwen3-asr` a retranscrit mot pour mot
les cinq voix masculines. Elles se distinguent par leur timbre et leur débit
(`aiden` est le plus rapide, `eric` le plus lent). Le ton défini par
`instructions`, en français ou en anglais, modifie l'interprétation sans
affecter l'intelligibilité.

```bash
curl -sS -o essai.wav "$URL/v1/audio/speech" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d "{\"model\": \"$B/qwen3-tts-12hz-1.7b-customvoice\", \"voice\": \"ryan\", \"language\": \"French\",
       \"instructions\": \"Voix posée de conteur\", \"input\": \"Bonjour, ceci est un essai.\", \"response_format\": \"wav\"}"
aplay essai.wav
```

## Concevoir une voix

VoiceDesign génère une voix à partir de la description (`instructions`) et
lit le texte (`input`) avec cette voix. Chaque appel produit une voix
**nouvelle** ; ce mode ne convient pas à une lecture longue qui exige une
voix constante, mais permet de générer une référence pour le clonage.

```bash
jq -n --arg text "$TEXT" --arg description "Voix masculine française d'une cinquantaine d'années, grave, chaude et veloutée, légèrement feutrée. Narrateur professionnel de livre audio : diction parfaite, accent français standard, débit posé, intonation naturelle et expressive sans excès." \
  --arg model "$B/qwen3-tts-12hz-1.7b-voice-design" \
  '{model: $model, input: $text, instructions: $description, language: "French", response_format: "wav"}' > design.json
curl -sS -o design.wav "$URL/v1/audio/speech" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @design.json
aplay design.wav
```

Décrire le genre, l'âge, le timbre, l'accent et le style de lecture.
Répéter l'appel jusqu'à obtenir la voix souhaitée. Variante féminine :
« Voix féminine française d'une quarantaine d'années, chaude, douce et
légèrement grave, timbre velouté et rassurant. Narratrice professionnelle
de livre audio : … ».

## Cloner une voix

Base synthétise un texte avec la voix et l'accent d'un enregistrement ; la
**même voix** est réutilisée d'une requête à l'autre. Une référence française
produit une voix française, plus adaptée à la synthèse en français que les
voix prédéfinies. Le flux recommandé consiste à **concevoir** une voix une
fois, puis à la **cloner** pour les autres textes.

**1. La référence** : utiliser `design.wav` ci-dessus ou un enregistrement
de sa propre voix lisant `$TEXT` (environ 15 s, dans une pièce calme), au format WAV. La
transcription doit être exacte, mot pour mot ; la vérifier avec l'ASR :

```bash
curl -sS "$URL/v1/audio/transcriptions" -H "Authorization: Bearer $KEY" \
  -F model=$B/qwen3-asr-1.7b -F language=fr -F file=@ma-voix.wav | jq -r .text
```

**2. La normaliser** : Base reproduit le niveau, l'acoustique de la pièce et
les pauses de la référence. Préparer un fichier mono à 24 kHz (fréquence des
modèles), couper les silences de début et de fin, puis normaliser à -20 LUFS
avec des crêtes à -1 dB. La paire `areverse` coupe le silence final ;
`stop_periods` couperait à la première pause de la phrase. Une référence de
3 à 20 secondes donne les meilleurs résultats ; une référence plus longue
augmente la taille de chaque requête sans améliorer la voix.

```bash
ffmpeg -i design.wav -ac 1 -ar 24000 -c:a pcm_s16le -af "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,areverse,loudnorm=I=-20:TP=-1:LRA=11" voix.wav
```

**3. Lire avec la voix clonée** : ne pas inclure `voice` ni `instructions` ;
la synthèse reprend la voix de l'enregistrement.

```bash
base64 -w0 voix.wav > voix.b64
jq -n --rawfile audio voix.b64 --arg reference "$TEXT" --arg text "Le meunier soupira longuement. Il savait que le chat avait raison." \
  --arg model "$B/qwen3-tts-12hz-1.7b-base" \
  '{model: $model, input: $text, reference_audio: $audio, reference_text: $reference, language: "French", response_format: "wav"}' > clone.json
curl -sS -o exemple.wav "$URL/v1/audio/speech" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @clone.json
aplay exemple.wav
```

L'enregistrement est joint à **chaque** requête. Une application doit
conserver la référence et sa transcription, puis les transmettre pour
chaque passage synthétisé.

**Cloner uniquement des voix dont l'utilisation est autorisée** : voix
conçue à partir d'une description, voix propre ou voix d'une personne ayant
donné son accord. La voix d'un comédien, d'un narrateur ou d'une personnalité
est protégée comme son image ; les enregistrements publiés (extraits de
livres audio, par exemple) relèvent des droits de leur éditeur.

## Débits mesurés

Qwen3-TTS 1.7B sur un GPU local, par le proxy :

| Requête | Audio | Génération |
|---|---|---|
| CustomVoice, une phrase | ~6 s | ~3 s |
| CustomVoice, ~1 000 caractères | ~1 min | 20 à 25 s |
| VoiceDesign, la phrase de référence | ~15 s | 6 à 15 s |
| Base (clonage), deux phrases | ~5 s | 3 à 8 s |

La génération est 2 à 3 fois plus rapide que la lecture. Une application
peut donc générer le passage suivant pendant la lecture du précédent.

## Un modèle à la fois

Un backend qui ne conserve qu'un seul modèle en mémoire (llama-swap) change
de modèle lorsque les requêtes ciblent CustomVoice, Base, VoiceDesign ou
ASR. La première requête suivant le changement attend le chargement ; durant
celui-ci, le proxy peut renvoyer `backend_offline`, y compris pour
`GET /v1/audio/voices`. Réessayer après quelques secondes. Pour une lecture
longue, conserver le même modèle d'une requête à l'autre (voix clonées ou
voix prédéfinies).
