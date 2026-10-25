# Voix : synthèse, conception et clonage

Le proxy relaie l'audio des backends comme le reste, routé par le préfixe
du modèle : `POST /v1/audio/speech` (synthèse), `POST /v1/audio/transcriptions`
(transcription, multipart compris) et `GET /v1/audio/voices?model=<backend>/<modèle>`
(voix d'un modèle). Avec les modèles **Qwen3-TTS** d'un backend gufo, on
obtient trois usages : lire avec une voix toute faite, **concevoir** une voix
à partir d'une description, et **cloner** une voix depuis un court
enregistrement. Ce guide les fait à la main, avec curl ; le chat de la page
les fait aussi, normalisation comprise, sans rien installer
([Chat](chat.md), *Voix* et *Voix clonées*).

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

Les corps JSON sont construits avec `jq` : un enregistrement en base64 pèse
près d'un mégaoctet, trop pour une ligne de commande.

## Les modèles

| Modèle | Rôle | Exige |
|---|---|---|
| `qwen3-tts-12hz-1.7b-customvoice` | 9 voix toutes faites, ton réglable (`instructions`) | `voice` |
| `qwen3-tts-12hz-1.7b-voice-design` | invente une voix d'après une description | `instructions` non vide (la description) |
| `qwen3-tts-12hz-1.7b-base` | lit avec la voix d'un enregistrement | `reference_audio` (WAV en base64) et `reference_text` (sa transcription) |
| `qwen3-asr-1.7b` | transcription (audio → texte) : **pas** de synthèse | |

Les trois modèles de synthèse lisent 10 langues, dont le français :
`"language": "French"` évite qu'ils devinent la langue à chaque requête.
`"response_format": "wav"` est le format sûr.

Les erreurs du backend sont relayées telles quelles :

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

Aucune n'est francophone d'origine, toutes lisent le français : relues par
`qwen3-asr`, les 5 voix d'homme ont été retranscrites mot pour mot. Elles se
distinguent par le timbre et le débit (`aiden` le plus rapide, `eric` le plus
lent) ; le ton (`instructions`, en français ou en anglais) change le jeu, pas
l'intelligibilité.

```bash
curl -sS -o essai.wav "$URL/v1/audio/speech" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d "{\"model\": \"$B/qwen3-tts-12hz-1.7b-customvoice\", \"voice\": \"ryan\", \"language\": \"French\",
       \"instructions\": \"Voix posée de conteur\", \"input\": \"Bonjour, ceci est un essai.\", \"response_format\": \"wav\"}"
aplay essai.wav
```

## Concevoir une voix

VoiceDesign invente une voix d'après sa description (`instructions`) et lit
le texte (`input`) avec elle. Il en invente une **nouvelle à chaque appel** :
inutilisable tel quel pour une lecture longue (la voix changerait d'un passage
à l'autre), mais parfait pour fabriquer la référence d'une voix clonée.

```bash
jq -n --arg text "$TEXT" --arg description "Voix masculine française d'une cinquantaine d'années, grave, chaude et veloutée, légèrement feutrée. Narrateur professionnel de livre audio : diction parfaite, accent français standard, débit posé, intonation naturelle et expressive sans excès." \
  --arg model "$B/qwen3-tts-12hz-1.7b-voice-design" \
  '{model: $model, input: $text, instructions: $description, language: "French", response_format: "wav"}' > design.json
curl -sS -o design.wav "$URL/v1/audio/speech" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @design.json
aplay design.wav
```

Décrire le genre, l'âge, le timbre, l'accent et le style de lecture ; relancer
jusqu'à ce que la voix plaise. Une variante féminine : « Voix féminine
française d'une quarantaine d'années, chaude, douce et légèrement grave,
timbre velouté et rassurant. Narratrice professionnelle de livre audio : … ».

## Cloner une voix

Base lit n'importe quel texte avec la voix d'un enregistrement : la **même
voix** d'une requête à l'autre, avec l'accent de l'enregistrement (une
référence française donne une voix française, bien meilleure en français que
les voix toutes faites). La chaîne conseillée : **concevoir** la voix une fois,
puis la **cloner** pour tout le reste.

**1. La référence** : `design.wav` ci-dessus, ou sa propre voix lisant `$TEXT`
(une quinzaine de secondes, pièce calme), en WAV. Sa transcription doit être
exacte, mot pour mot ; la vérifier avec l'ASR :

```bash
curl -sS "$URL/v1/audio/transcriptions" -H "Authorization: Bearer $KEY" \
  -F model=$B/qwen3-asr-1.7b -F language=fr -F file=@ma-voix.wav | jq -r .text
```

**2. La normaliser** : Base copie le niveau, la pièce et les pauses de sa
référence. Mono 24 kHz (la fréquence des modèles), silences de début et de fin
coupés (la paire `areverse` coupe la fin ; `stop_periods` couperait à la
première pause de la phrase), -20 LUFS, crêtes à -1 dB. Une référence de 3 à
20 secondes clone le mieux ; plus longue, elle alourdit chaque requête sans
améliorer la voix.

```bash
ffmpeg -i design.wav -ac 1 -ar 24000 -c:a pcm_s16le -af "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,areverse,loudnorm=I=-20:TP=-1:LRA=11" voix.wav
```

**3. Lire avec la voix clonée** (ni `voice` ni `instructions` : la voix lit
comme elle a été enregistrée) :

```bash
base64 -w0 voix.wav > voix.b64
jq -n --rawfile audio voix.b64 --arg reference "$TEXT" --arg text "Le meunier soupira longuement. Il savait que le chat avait raison." \
  --arg model "$B/qwen3-tts-12hz-1.7b-base" \
  '{model: $model, input: $text, reference_audio: $audio, reference_text: $reference, language: "French", response_format: "wav"}' > clone.json
curl -sS -o exemple.wav "$URL/v1/audio/speech" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @clone.json
aplay exemple.wav
```

L'enregistrement part avec **chaque** requête : une application garde la
référence et sa transcription, et les renvoie pour chaque passage lu.

**Ne cloner que des voix qu'on a le droit d'utiliser** : une voix conçue
d'après une description, la sienne, ou celle d'une personne d'accord. La voix
d'un comédien, d'un narrateur ou d'une personnalité est protégée comme son
image, et les enregistrements publiés (extraits de livres audio…) par les
droits de leur éditeur.

## Débits mesurés

Qwen3-TTS 1.7B sur un GPU local, par le proxy :

| Requête | Audio | Génération |
|---|---|---|
| CustomVoice, une phrase | ~6 s | ~3 s |
| CustomVoice, ~1 000 caractères | ~1 min | 20 à 25 s |
| VoiceDesign, la phrase de référence | ~15 s | 6 à 15 s |
| Base (clonage), deux phrases | ~5 s | 3 à 8 s |

La génération va 2 à 3 fois plus vite que la lecture : une application peut
préparer le passage suivant pendant que le précédent est joué.

## Un modèle à la fois

Un backend qui ne garde qu'un modèle chargé (llama-swap) bascule quand les
requêtes changent de modèle (CustomVoice, Base, VoiceDesign, ASR) : la première
requête après une bascule attend le chargement, et pendant celui-ci le proxy
peut répondre `backend_offline`, même à `GET /v1/audio/voices`. Réessayer
quelques secondes plus tard ; une application qui lit longtemps garde un seul
modèle (toutes ses voix clonées, ou toutes toutes faites).
