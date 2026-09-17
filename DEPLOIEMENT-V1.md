# Vektor V1 — Journal de déploiement complet (step by step)

Assistant personnel auto-hébergé « Jarvis » : omnicanal (Telegram + Alexa),
conscient de l'infrastructure (RAG + outils live en lecture seule), cerveau
LLM local (Ollama sur VPS).

- **Bot Telegram** : [t.me/<ton-bot-telegram>](https://t.me/<ton-bot-telegram>)
- **Endpoint Alexa** : `https://vektor.example.ch/api/alexa`
- **Hébergement** : VPS (`/opt/vektor`), 4 conteneurs Docker
- **Sécurité** : lecture seule, whitelist stricte, aucun port public inutile

---

## 1. Architecture finale

```
Telegram (polling) ─┐
                    ├─→ FastAPI (/api/chat, /api/alexa, /api/status)
Alexa (EU, HTTPS) ──┘         │
                              ├─→ LangGraph → Ollama (qwen2.5:14b, keep_alive=-1)
                              ├─→ RAG pgvector (doc infra, 7 blocs)
                              ├─→ Outils live LECTURE SEULE :
                              │     • API PVE (token vektor-ro@pve!vektor, PVEAuditor)
                              │     • SSH à commande forcée (script vektor-status unique)
                              │     • Checks HTTP (jellyfin, sonarr, radarr, prowlarr, qbit…)
                              └─→ PostgreSQL (mémoire conversationnelle multi-canal)

Réseau : API sur le réseau Docker de Caddy (accès interne par nom,
aucun port publié) ; Telegram passe par 127.0.0.1:8000 publié en loopback ;
Ollama ponté par socat host-network lié uniquement à la passerelle Docker.
```

### Conteneurs

| Conteneur | Rôle | Exposition |
|---|---|---|
| `vektor-postgres` | PostgreSQL 16 + pgvector, mémo + RAG | 127.0.0.1:55432 |
| `vektor-api` | FastAPI + LangGraph + outils | réseau Docker interne + 127.0.0.1:8000 |
| `vektor-telegram` | Bot polling (python-telegram-bot) | aucun (sortant uniquement) |
| `vektor-ollama-bridge` | socat host → passerelle Docker pour Ollama | passerelle Docker uniquement |

### Fichiers

```
vektor/
├── app/
│   ├── main.py       # FastAPI : /health, /api/chat, /api/alexa, /api/status, /api/forget
│   ├── graph.py      # run_agent : routage → LLM (Ollama, keep_alive) + prompts FR
│   ├── tools.py      # outils live : PVE, Docker, checks HTTP, /status complet
│   ├── alexa.py      # vérif signature Amazon + SSML + réponses progressives
│   ├── telegram.py   # bot : /start /help /status /forget, whitelist, messages longs
│   ├── memory.py     # PostgreSQL : conversations, messages, RAG
│   ├── rag.py        # indexation/recherche pgvector
│   └── config.py     # Settings pydantic (env)
├── knowledge/homelab.md   # doc infra NON SECRÈTE (topologie, rôles, IPs LAN)
├── scripts/               # benchmark modèles, tests keep_alive, netdiag
├── compose.yml
├── Dockerfile
├── .env.example           # modèle SANS valeurs réelles
└── DEPLOIEMENT-V1.md      # ce fichier
```

---

## 2. Étapes du déploiement (ordre réel)

### Étape 1 — Inventaire préalable
Vérifié sur le VPS : 12 vCPU, 23 Go RAM, ~40 Go libres, Ollama déjà présent
(modèles `qwen2.5:14b`, `llama3.1:8b`), Caddy existant en Docker.

### Étape 2 — Stack de base
`compose.yml` : PostgreSQL pgvector + API FastAPI. Premier échec : le build
Docker ne résolvait pas PyPI (DNS du réseau de build) → corrigé avec un
réseau de build utilisant le DNS de l'hôte.

### Étape 3 — Accès à Ollama depuis les conteneurs
L'IP NetBird d'Ollama est injoignable depuis un réseau Docker bridgé
(drop silencieux, diagnostiqué par `netdiag.py`). Solution : pont socat en
`network_mode: host`, lié **uniquement** à l'IP passerelle Docker
(172.20.0.1) + règle UFW scopée au subnet Docker. Ollama reste inatteignable
du WAN et du LAN.

### Étape 4 — API et sécurité
- `/api/chat` protégé par `X-Vektor-Token` (401 sans token).
- Chaîne testée de bout en bout : RAG, réponse LLM réelle, routage live.

### Étape 5 — Telegram
1. Constat : le token fourni au départ était celui du bot d'admin existant
   (`homelab_bot`) → conflit `getUpdates` (un token = un polling).
2. Création d'un **bot dédié** via @BotFather : `<ton-bot-telegram>`.
3. Piège de saisie : token recopié depuis un screenshot avec un caractère
   ambigu (`V` lu `v`) → `401 Unauthorized`. Toujours coller le token en
   texte, jamais depuis une image.
4. Code : whitelist d'IDs stricte (silence total pour les inconnus),
   `/start`, `/help`, `/status`, `/forget`, indicateur « écrit… »,
   découpage des messages > 4096 caractères.

### Étape 6 — Outils infrastructure (lecture seule)
1. **API PVE** : token `vektor-ro@pve!vektor`, rôle `PVEAuditor`.
   Piège : avec privilege separation (`privsep 1` par défaut), `/lxc`
   renvoie vide → `pveum acl modify ... --privsep 0` (le token hérite du
   rôle de son utilisateur, même pattern que le pve-exporter existant).
2. **SSH à commande forcée** : clé ed25519 dédiée sur le PVE avec
   `command="/usr/local/bin/vektor-status"`, no-pty, no-forwarding.
   Le script statique renvoie CTs + inventaire Docker + `memory.peak`.
   Preuve : toute commande arbitraire est remplacée par le script.
3. **Checks HTTP** : jellyfin, sonarr, radarr, prowlarr, qbittorrent, garmin.
4. Droits : `chown 10001` sur les secrets montés (conteneur non-root).

### Étape 7 — Choix du modèle (benchmark réel)
| Modèle | Verdict |
|---|---|
| `llama3.1:8b` | rapides mais réponses faibles en FR, refus aberrants |
| `qwen2.5:7b` | rapide MAIS hallucination grave (swap financier ≠ mémoire) |
| `qwen2.5:14b` | ✅ retenu : raisonnement sysadmin, bon français |

`keep_alive=-1` (résident en RAM) : finis les 74 s de chargement à froid.
⚠️ Un seul modèle résident à la fois (~9,2 Go) — deux simultanés = VPS à
880 Mo dispo. Déchargement d'urgence :
`curl http://127.0.0.1:11434/api/generate -d '{"model":"...","keep_alive":0}'`

### Étape 8 — Routage intelligent
- Intention d'**état** → outils live (0,1 s), réponse **non déformée** par le LLM.
- Intention d'**explication** (« pourquoi », « explique ») → LLM.
- Alexa n'envoie que le **slot nu** (« proxmox » sans « état ») → règle
  `bare_subject` : sujet infra court (≤ 5 mots) = demande d'état.

### Étape 9 — Alexa, partie serveur
1. `alexa.py` : vérification complète des requêtes Amazon
   (signature RSA-SHA1 du corps, certificat validé : HTTPS, port 443,
   host `s3.amazonaws.com` ou `*.amazonaws.com`, SAN `echo-api.amazon.com`
   **ou** `echo-api.amazonaws.com`, dates de validité, cache 6 h),
   anti-rejeu 150 s, `applicationId` dans allowlist.
2. `main.py` : LaunchRequest, Help, Stop/Cancel, Fallback, VektorQueryIntent,
   SessionEnded ; mémoire partagée Telegram ↔ Alexa via `sessionAttributes`.
3. Réponses **progressives** (`VoicePlayer.Speak` vers l'apiEndpoint
   régional EU de la requête) : le Echo parle pendant que le LLM calcule
   (Alexa coupe à ~8 s sinon). Boucle de phrases toutes les 6 s.
4. Exposition : DNS `vektor.example.ch` **grey cloud** (obligatoire :
   Amazon refuse le proxy orange de Cloudflare) → Caddy → réseau Docker
   interne. Certificat Let's Encrypt automatique.

### Étape 10 — Alexa, côté console Amazon (manuel)
1. Créer la skill : **Custom** · **Français (FR)** · **Provision your own**
   (⚠️ PAS « Alexa-Hosted » : le template Lambda d'Amazon répondrait à sa
   place et n'appellerait jamais notre serveur — première grosse erreur
   corrigée en recréant `vektor2`).
2. JSON Editor : coller l'interaction model (invocation `vektor`,
   `VektorQueryIntent` avec `AMAZON.SearchQuery` **toujours avec carrier
   phrase** — un slot seul est refusé au build) + `AMAZON.FallbackIntent`.
3. Build → vert.
4. Endpoint : HTTPS `https://vektor.example.ch/api/alexa`,
   « certificate from a trusted certificate authority ».
5. Onglet **Test** → On (sinon invisible sur les appareils).
6. Distribution : corriger le nom d'invocation affiché (le template
   pré-remplit « le génie des salutations »…), description, puis
   désactiver/réactiver la skill dans l'app pour resynchroniser.
7. Skill ID → `.env` VPS (`ALEXA_SKILL_ID=...`).

### Étape 11 — La chaîne de déverrouillage (le vrai feuilleton)
Chaque erreur générique « Un problème est survenu » cachait UNE couche :

| # | Symptôme | Cause réelle | Correctif |
|---|---|---|---|
| 1 | Le template répond « Intent: … Slots: … » | skill Alexa-Hosted (Lambda démo) | recréer en « Provision your own » |
| 2 | 400 signature | certificats EU : host régional `*.amazonaws.com`, chemin non standard, SAN `echo-api.amazon.com` | allowlist alignée sur le vérificateur ask-sdk |
| 3 | 400 « requête périmée » | **horloge VPS en retard de 3 min 22 s, NTP cassé** | `chrony` + `ch.pool.ntp.org` (offset final 0,00004 s) |
| 4 | « pas fourni de réponse valide » | slot nu → routage LLM lent → timeout 8 s | `bare_subject` → live instantané + progressives |
| 5 | 400 « applicationId inconnu » | l'ANCIENNE skill pointait aussi sur l'endpoint | allowlist multi-IDs, puis retour à l'ID unique |

**Leçon majeure** : `docker compose restart` ne relit NI le `.env` NI le
code → toujours `docker compose up -d --build` après modification.
Le log de diagnostic (motif exact + valeurs reçues) a remplacé toutes les
devinettes : `logger.warning("Requete Alexa rejetee: %s", exc)`.

### Étape 12 — Voix naturelle
`_voice_friendly()` (canal vocal uniquement) : listes Python supprimées,
CPU→« processeur », RAM→« mémoire », Uptime→« en marche depuis »,
`G`→« giga octets sur », `%`→« pour cent », décimales à la française
(11,2), lignes transformées en phrases. Telegram garde le format brut.

---

## 3. État final de la V1 (testé et prouvé)

| Capacité | État |
|---|---|
| Telegram `/start` `/help` `/status` `/forget` + chat libre | ✅ live |
| « Etat de Proxmox » → CPU/RAM/swap/uptime live | ✅ 0,1 s |
| Inventaire Docker des 6 CTs (SSH forcé) | ✅ |
| Checks services (jellyfin, *arr, qbit…) | ✅ |
| Questions générales → qwen2.5:14b | ✅ ~15 s (CPU) |
| Alexa simulateur : launch + question → réponse vocale | ✅ |
| Mémoire partagée Telegram ↔ Alexa | ✅ |
| Refus sans signature / mauvais ID / requête périmée | ✅ testés |
| Secrets | `.env` chmod 600 hors Git, aucun secret dans le RAG |

### Limites connues (V1)
- Echo **physique** : skill activée et fiche correcte, mais l'appareil ne
  déclenche pas encore la skill (« je ne sais pas comment vous aider »).
  Pistes restantes : resynchro complète (disable/enable + reboot), compte
  Echo = compte dev, marketplace du compte dev = FR. Le simulateur, lui,
  fonctionne de bout en bout — le serveur n'est pas en cause.
- Latence LLM ~15 s (CPU) : leviers futurs = petit GPU local avec Ollama
  (aucun changement d'architecture) ou streaming Telegram.
- Réponses Alexa tronquées à 600 caractères (limite vocale assumée).

## 4. Exploitation

```bash
# état
ssh vps 'docker ps --filter name=vektor'
ssh vps 'docker logs vektor-api --tail 20'

# redémarrer proprement après modif code/env (JAMAIS simple restart)
ssh vps 'cd /opt/vektor && docker compose up -d --build'

# décharger le modèle si RAM VPS critique
ssh vps 'curl -s http://127.0.0.1:11434/api/generate -d "{\"model\":\"qwen2.5:14b\",\"keep_alive\":0}"'
```

Sauvegardes : `/opt/vektor` (compose + .env) et le volume `postgres-data`
sont couverts par les backups VPS existants ; la mémoire conversationnelle
vit dans PostgreSQL (restaurable).

## 5. Feuille de route V2

1. Echo physique (résolution compte/marketplace).
2. Outils d'écriture **avec confirmation obligatoire** (restart d'un service…).
3. Streaming des réponses Telegram (perçu 3× plus rapide).
4. Adaptateur web (canal déjà prévu : `channel: "web"`).
5. GPU local pour le LLM via NetBird.
