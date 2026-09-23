from contextlib import asynccontextmanager
import asyncio
import json
import logging

logger = logging.getLogger("uvicorn.error")
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from .config import get_settings
from .memory import Memory
from .rag import index_knowledge
from .graph import run_agent
from .tools import full_status_report, infra_live_report, model_card, seeds_report
from . import watch as watch_mod
from . import alexa as alexa_mod

memory = Memory()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await memory.connect()
    await index_knowledge(memory)
    yield
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
    """Sonde DoH des résolveurs configurés. Lecture seule."""
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
