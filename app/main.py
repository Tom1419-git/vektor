from contextlib import asynccontextmanager
import asyncio
import hashlib
import json
import logging

logger = logging.getLogger("uvicorn.error")
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from .config import get_settings
from .memory import Memory
from .rag import index_knowledge
from .graph import run_agent
from .tools import full_status_report, infra_live_report, model_card, seeds_report
from .ping import ping_report
from . import watch as watch_mod
from . import alexa as alexa_mod

memory = Memory()

# Empreinte du répertoire knowledge : l'auto-reload (v1.5.0) réindexe
# automatiquement quand la doc change — plus besoin de penser à /reload.
_knowledge_sig: str = ""


def _knowledge_fingerprint() -> str:
    """Empreinte stable des fichiers markdown de knowledge/ (noms + contenu)."""
    digest = hashlib.sha256()
    for path in sorted(Path("knowledge").rglob("*.md")):
        if path.name.startswith("._"):
            continue
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


async def _knowledge_watcher(interval: int = 30) -> None:
    """Compare l'empreinte toutes les 30 s ; réindexe au changement (v1.5.0).
    Une erreur ne tue jamais la boucle : le reload reste possible à la main."""
    global _knowledge_sig
    while True:
        await asyncio.sleep(interval)
        try:
            signature = await asyncio.to_thread(_knowledge_fingerprint)
            if _knowledge_sig and signature != _knowledge_sig:
                count = await index_knowledge(memory)
                logger.info("knowledge/ modifié : réindexation auto de %d extraits", count)
            _knowledge_sig = signature
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("auto-reload knowledge : échec de ce tour")


_watcher_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _knowledge_sig
    await memory.connect()
    _knowledge_sig = _knowledge_fingerprint()
    await index_knowledge(memory)
    _watcher_task = asyncio.create_task(_knowledge_watcher())
    yield
    if _watcher_task:
        _watcher_task.cancel()
    await memory.close()


app = FastAPI(title="Vektor API", version="0.1.0", lifespan=lifespan)


class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    channel: str = Field(pattern="^(telegram|alexa|web)$")
    text: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None


class ForgetRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)


class ChatResponse(BaseModel):
    response: str
    conversation_id: str


def require_token(token: str | None):
    if token != get_settings().vektor_api_token:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "vektor"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, x_vektor_token: str | None = Header(default=None)):
    require_token(x_vektor_token)
    conversation_id = await memory.ensure_conversation(
        request.conversation_id, request.user_id, request.channel
    )
    await memory.add_message(conversation_id, "user", request.text)
    history = await memory.history(conversation_id)
    # user_key = périmètre des confirmations d'actions (canal:user)
    response = await run_agent(
        memory, request.text, history, user_key=f"{request.channel}:{request.user_id}"
    )
    await memory.add_message(conversation_id, "assistant", response)
    return ChatResponse(response=response, conversation_id=str(conversation_id))


@app.post("/api/alexa")
async def alexa_endpoint(request: Request):
    body = await request.body()
    if len(body) > 512 * 1024:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        await alexa_mod.verify_request(
            body,
            request.headers.get("Signature"),
            request.headers.get("SignatureCertChainUrl"),
        )
        payload = json.loads(body)
        alexa_mod.check_timestamp_and_app(payload)
    except alexa_mod.AlexaVerificationError as exc:
        logger.warning("Requete Alexa rejetee: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="JSON invalide") from exc

    request_type = payload.get("request", {}).get("type", "")
    session_attrs = payload.get("session", {}).get("attributes", {}) or {}
    session_id = payload.get("session", {}).get("sessionId") or "alexa-anon"
    user_id = (
        payload.get("session", {}).get("user", {}).get("userId")
        or payload.get("context", {}).get("System", {}).get("user", {}).get("userId")
        or "alexa-anon"
    )
    conv_id = session_attrs.get("conversation_id")

    if request_type in ("LaunchRequest", "SessionEndedRequest"):
        if request_type == "SessionEndedRequest":
            return alexa_mod.build_response("Au revoir !", True, session_attributes={})
        return alexa_mod.build_response(
            "Vektor est là ! Demande-moi l'état du homelab, un service, ou pose ta question.",
            False,
            reprompt="Que veux-tu savoir ?",
            session_attributes=session_attrs,
        )

    if request_type != "IntentRequest":
        return alexa_mod.build_response("Je n'ai pas compris ce type de demande.", True)

    intent_name = payload.get("request", {}).get("intent", {}).get("name", "")
    if intent_name in ("AMAZON.StopIntent", "AMAZON.CancelIntent"):
        return alexa_mod.build_response("À bientôt !", True)
    if intent_name == "AMAZON.HelpIntent":
        return alexa_mod.build_response(
            "Demande-moi l'état de Proxmox, vérifie si Jellyfin répond, "
            "ou pose une question générale.",
            False,
            reprompt="Que veux-tu savoir ?",
            session_attributes=session_attrs,
        )
    if intent_name == "AMAZON.FallbackIntent":
        return alexa_mod.build_response(
            "Je n'ai pas saisi la demande. Essaie par exemple : quel est l'état de Proxmox ?",
            False,
            reprompt="Que veux-tu savoir ?",
            session_attributes=session_attrs,
        )

    slots = payload.get("request", {}).get("intent", {}).get("slots", {}) or {}
    query = (slots.get("query", {}) or {}).get("value", "").strip()
    if not query:
        return alexa_mod.build_response(
            "Quel est le sujet de ta question ?",
            False,
            reprompt="Pose ta question.",
            session_attributes=session_attrs,
        )

    conversation_id = await memory.ensure_conversation(conv_id, user_id, "alexa")
    await memory.add_message(conversation_id, "user", query)

    # Alexa : canal vocal -> AUCUNE action d'écriture (pas de confirmation
    # orale fiable). Les demandes d'action reçoivent la réponse de refus standard.
    live = await infra_live_report(query)
    if live:
        answer = live
    else:
        # Le LLM peut prendre 15-60 s : Alexa coupe à ~8 s. On diffuse des
        # messages intermédiaires (VoicePlayer.Speak) pour tenir la session.
        api_token = (
            payload.get("context", {})
            .get("System", {})
            .get("apiAccessToken", "")
        )
        request_id = payload.get("request", {}).get("requestId", "")
        api_endpoint = payload.get("context", {}).get("System", {}).get("apiEndpoint", "")
        progress = asyncio.create_task(
            alexa_mod.progressive_loop(api_token, request_id, api_endpoint)
        )
        try:
            history = await memory.history(conversation_id)
            answer = await run_agent(memory, query, history)
        finally:
            progress.cancel()
    await memory.add_message(conversation_id, "assistant", answer)

    return alexa_mod.build_response(
        alexa_mod._clean_for_voice(answer),
        False,
        reprompt="Autre chose ?",
        session_attributes={**session_attrs, "conversation_id": str(conversation_id)},
    )


@app.get("/api/model")
async def model_endpoint(x_vektor_token: str | None = Header(default=None)):
    """Fiche technique réelle : modèle, hardware, latence. Aucun appel LLM."""
    require_token(x_vektor_token)
    return {"card": await model_card()}


@app.get("/api/seeds")
async def seeds_endpoint(x_vektor_token: str | None = Header(default=None)):
    """Rapport seeds : qBittorrent live + staging récupérable. Lecture seule."""
    require_token(x_vektor_token)
    return {"report": await seeds_report()}


@app.get("/api/ping")
async def ping_endpoint(x_vektor_token: str | None = Header(default=None)):
    """Diagnostic du chemin LLM complet, maillon par maillon. Lecture seule."""
    require_token(x_vektor_token)
    return {"report": await ping_report()}


@app.post("/api/reload-doc")
async def reload_doc_endpoint(x_vektor_token: str | None = Header(default=None)):
    """Ré-indexe knowledge/ (volume knowledge-live) sans redémarrer.

    replace_knowledge fait TRUNCATE + réinsertion en une transaction :
    un chat en cours pendant le reload ne voit jamais un index vide."""
    require_token(x_vektor_token)
    try:
        count = await index_knowledge(memory)
    except Exception:
        logger.exception("reload-doc : réindexation échouée")
        raise HTTPException(status_code=500, detail="Réindexation échouée — index précédent conservé")
    return {
        "report": (
            f"🔄 Documentation réindexée : {count} extraits chargés depuis knowledge/.\n"
            "Le prochain chat utilisera directement la nouvelle version — aucun redémarrage nécessaire."
        )
    }


@app.get("/api/status")
async def status(x_vektor_token: str | None = Header(default=None)):
    require_token(x_vektor_token)
    report = await full_status_report()
    return {"report": report}


@app.get("/api/backups")
async def backups_endpoint(x_vektor_token: str | None = Header(default=None)):
    """Derniers backups vzdump vus par l'API Proxmox. Lecture seule."""
    require_token(x_vektor_token)
    return {"report": await watch_mod.backups_report()}


@app.get("/api/dns")
async def dns_endpoint(x_vektor_token: str | None = Header(default=None)):
    """Sonde multi-transport (UDP wire/DoH) des résolveurs. Lecture seule."""
    require_token(x_vektor_token)
    return {"report": await watch_mod.dns_report()}


@app.get("/api/monitoring")
async def monitoring_endpoint(x_vektor_token: str | None = Header(default=None)):
    """État des checks Healthchecks.io (token lecture dédié). Lecture seule."""
    require_token(x_vektor_token)
    return {"report": await watch_mod.monitoring_report()}


@app.post("/api/forget")
async def forget(request: ForgetRequest, x_vektor_token: str | None = Header(default=None)):
    require_token(x_vektor_token)
    deleted = await memory.forget_user(request.user_id)
    return {"deleted_conversations": deleted}


# ── Canal web (roadmap V2 #4) ──────────────────────────────────────────
# Le token sert ici de mot de passe client : la page le garde en
# sessionStorage et l'envoie à chaque message. Aucun secret dans le dépôt.

_WEB_PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vektor</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, 'Segoe UI', sans-serif;
         background: #0f1420; color: #e8ecf4; display: flex;
         flex-direction: column; height: 100dvh; }
  header { padding: 14px 18px; border-bottom: 1px solid #232b3d;
           font-weight: 600; letter-spacing: .5px; }
  header span { color: #6ea8ff; }
  #log { flex: 1; overflow-y: auto; padding: 18px; display: flex;
         flex-direction: column; gap: 10px; }
  .msg { max-width: 78%; padding: 10px 14px; border-radius: 14px;
         white-space: pre-wrap; line-height: 1.45; }
  .user { align-self: flex-end; background: #2b5fd9; }
  .bot  { align-self: flex-start; background: #1b2334; }
  .err  { align-self: flex-start; background: #4a1f24; color: #ffb4b4; }
  form { display: flex; gap: 8px; padding: 12px; border-top: 1px solid #232b3d; }
  textarea { flex: 1; resize: none; background: #161d2c; color: #e8ecf4;
             border: 1px solid #2a3550; border-radius: 10px; padding: 10px;
             font: inherit; min-height: 44px; max-height: 140px; }
  button { background: #2b5fd9; color: white; border: 0; border-radius: 10px;
           padding: 0 18px; font: inherit; cursor: pointer; }
  button:disabled { opacity: .5; }
  #gate { margin: auto; display: flex; flex-direction: column; gap: 10px;
          width: min(360px, 90%); }
  #gate input { background: #161d2c; color: #e8ecf4; border: 1px solid #2a3550;
                border-radius: 10px; padding: 12px; font: inherit; }
</style>
</head>
<body>
<header>Vektor <span>· canal web</span></header>
<div id="gate">
  <p>Entre le token d'accès (variable <code>VEKTOR_API_TOKEN</code> du serveur).</p>
  <input id="tok" type="password" placeholder="token" autofocus>
  <button onclick="enter()">Entrer</button>
</div>
<div id="log" hidden></div>
<form id="f" hidden>
  <textarea id="t" placeholder="Demande-moi quelque chose…"></textarea>
  <button id="send">Envoyer</button>
</form>
<script>
const $ = id => document.getElementById(id);
let token = sessionStorage.vektorToken || '';

function show() {
  const gate = $('gate');
  if (gate) gate.remove();
  $('log').hidden = false; $('f').hidden = false; $('t').focus();
}

function enter() {
  const field = $('tok');
  if (!field) return;
  token = field.value.trim();
  if (!token) return;
  sessionStorage.vektorToken = token;
  show();
}

// Les listeners sont attachés AVANT tout retrait du formulaire de
// connexion : avec un token déjà en session, #gate n'existe plus au
// chargement — un listener sur un élément absent tuerait tout le script.
const tokField = $('tok');
if (tokField) tokField.addEventListener('keydown', e => { if (e.key === 'Enter') enter(); });

function bubble(text, cls) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.textContent = text;
  $('log').appendChild(d);
  $('log').scrollTop = $('log').scrollHeight;
  return d;
}

async function ask(text) {
  bubble(text, 'user');
  const waiting = bubble('Vektor réfléchit… (le LLM local peut prendre 1-3 min)', 'bot');
  $('send').disabled = true;
  try {
    const r = await fetch('/api/web/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Vektor-Token': token },
      body: JSON.stringify({ text })
    });
    if (r.status === 401) { waiting.textContent = 'Token refusé — recharge la page.'; return; }
    const d = await r.json();
    waiting.textContent = d.response || JSON.stringify(d);
  } catch (e) {
    waiting.className = 'msg err';
    waiting.textContent = 'Erreur : ' + e;
  } finally {
    $('send').disabled = false; $('t').focus();
  }
}

$('f').addEventListener('submit', e => {
  e.preventDefault();
  const text = $('t').value.trim();
  if (!text) return;
  $('t').value = '';
  ask(text);
});

if (token) show();
</script>
</body>
</html>"""


@app.get("/web", response_class=HTMLResponse)
async def web_page():
    """Mini client web : token en sessionStorage, chat direct vers l'API."""
    return _WEB_PAGE


class WebChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/api/web/chat", response_model=ChatResponse)
async def web_chat(request: WebChatRequest, x_vektor_token: str | None = Header(default=None)):
    """Chat depuis la page /web : user_id fixe 'web-local', même agent que Telegram."""
    require_token(x_vektor_token)
    conversation_id = await memory.ensure_conversation(None, "web-local", "web")
    await memory.add_message(conversation_id, "user", request.text)
    history = await memory.history(conversation_id)
    response = await run_agent(memory, request.text, history, user_key="web:web-local")
    await memory.add_message(conversation_id, "assistant", response)
    return ChatResponse(response=response, conversation_id=str(conversation_id))
