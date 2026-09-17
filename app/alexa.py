#!/usr/bin/env python3
"""Vektor - Adaptateur Alexa : verification des requetes Amazon + rendu SSML.

Securite :
- Verification de la signature RSA-SHA1 du corps avec le certificat fourni par Amazon.
- URL de certificat restreinte a https://s3.amazonaws.com/alexa-static/...
- Validation SAN (echo-api.amazonaws.com), dates de validite du certificat.
- Anti-rejeu : timestamp de la requete <= 150 s.
- Verification de l'applicationId si ALEXA_SKILL_ID est configure (rejet sinon).
"""
from __future__ import annotations

import asyncio
import base64
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from .config import get_settings

MAX_BODY_BYTES = 512 * 1024
TIMESTAMP_TOLERANCE_S = 150
CERT_CACHE: dict[str, tuple[float, x509.Certificate]] = {}
CERT_TTL = 3600 * 6


class AlexaVerificationError(Exception):
    """Requete Alexa invalide ou non verifiable."""


def _validate_cert_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        raise AlexaVerificationError(f"URL de certificat invalide: {url}")
    host = parsed.hostname or ""
    # Regle officielle Amazon (verificateur ask-sdk) : host = s3.amazonaws.com
    # ou *.amazonaws.com. Pas de contrainte de chemin (les buckets regionaux
    # EU/NA/FE utilisent des prefixes differents) ; l'URL est loggee si rejet.
    if host != "s3.amazonaws.com" and not host.endswith(".amazonaws.com"):
        raise AlexaVerificationError(f"URL de certificat hors domaine Amazon autorise: {url}")


async def _load_cert(url: str) -> x509.Certificate:
    cached = CERT_CACHE.get(url)
    now = time.monotonic()
    if cached and now - cached[0] < CERT_TTL:
        return cached[1]
    _validate_cert_url(url)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(url)
    if response.status_code != 200 or len(response.content) > 100 * 1024:
        raise AlexaVerificationError("Certificat Amazon illisible")
    cert = x509.load_pem_x509_certificate(response.content)
    not_after = cert.not_valid_after_utc
    not_before = cert.not_valid_before_utc
    now_utc = datetime.now(timezone.utc)
    if now_utc < not_before or now_utc > not_after:
        raise AlexaVerificationError("Certificat Amazon expire")
    CERT_CACHE[url] = (now, cert)
    return cert


async def verify_request(
    body: bytes,
    signature: str | None,
    cert_url: str | None,
) -> None:
    if not signature or not cert_url:
        raise AlexaVerificationError("En-tetes de signature manquants")
    cert = await _load_cert(cert_url)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    names = san.value.get_values_for_type(x509.DNSName)
    # Amazon a emis historiquement echo-api.amazonaws.com ; les certificats
    # recents portent echo-api.amazon.com. Accepter les deux domaines.
    if "echo-api.amazonaws.com" not in names and "echo-api.amazon.com" not in names:
        raise AlexaVerificationError(f"SAN du certificat invalide: {names}")
    try:
        raw_sig = base64.b64decode(signature)
        cert.public_key().verify(
            raw_sig,
            body,
            padding.PKCS1v15(),
            hashes.SHA1(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise AlexaVerificationError("Signature invalide") from exc


def check_timestamp_and_app(request: dict) -> None:
    settings = get_settings()
    skill_id = getattr(settings, "alexa_skill_id", "") or ""
    if not skill_id:
        raise AlexaVerificationError("ALEXA_SKILL_ID non configure : requetes Alexa rejetees")
    timestamp = request.get("request", {}).get("timestamp")
    if timestamp:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age = abs((datetime.now(timezone.utc) - ts).total_seconds())
        if age > TIMESTAMP_TOLERANCE_S:
            raise AlexaVerificationError("Requete perimee (anti-rejeu)")
    app_id = request.get("session", {}).get("application", {}).get("applicationId") \
        or request.get("context", {}).get("System", {}).get("application", {}).get("applicationId")
    allowed = {s.strip() for s in skill_id.split(",") if s.strip()}
    if app_id not in allowed:
        raise AlexaVerificationError(
            f"applicationId inconnu: recu={app_id} attendu={sorted(allowed)}"
        )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clean_for_voice(text: str, limit: int = 600) -> str:
    cleaned = text
    for token in ["**", "__", "`", "#", "> "]:
        cleaned = cleaned.replace(token, "")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3].rstrip() + "..."
    return cleaned


PROGRESSIVE_PATH = "/v1/directives"
PROGRESS_PHRASES = [
    "Un instant, je vérifie.",
    "Je réfléchis.",
    "Encore un instant.",
    "J'assemble la réponse.",
    "Presque terminé.",
    "Presque terminé.",
    "Presque terminé.",
]


async def _send_progressive(url: str, api_access_token: str, request_id: str, phrase: str) -> None:
    if not api_access_token or not request_id:
        return
    payload = {
        "header": {"requestId": request_id},
        "directive": {
            "type": "VoicePlayer.Speak",
            "speech": f"<speak>{phrase}</speak>",
        },
    }
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_access_token}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError:
        pass


async def progressive_loop(api_access_token: str, request_id: str, api_endpoint: str = "") -> None:
    """Envoie des messages intermédiaires pour tenir la session Alexa ouverte
    pendant que le LLM calcule (la réponse finale doit suivre la dernière
    directive progressive de moins de 8 s). Le domaine régional (EU/NA)
    est repris du champ context.System.apiEndpoint de la requête."""
    base = (api_endpoint or "https://api.amazonalexa.com").rstrip("/")
    try:
        await asyncio.sleep(2.5)
        for phrase in PROGRESS_PHRASES:
            await _send_progressive(base + PROGRESSIVE_PATH, api_access_token, request_id, phrase)
            await asyncio.sleep(6)
    except asyncio.CancelledError:
        return


def _voice_friendly(text: str) -> str:
    """Transforme un rapport technique en texte prononçable naturellement.
    Appliqué UNIQUEMENT au canal vocal : Telegram garde le format brut."""
    import re as _re
    t = text
    # listes Python : charge ['11.20', '11.35', '11.26'] -> supprimee
    t = _re.sub(r"\[[^\]]*\]", "", t)
    # titres de sections type "== CTs (host view) =="
    t = _re.sub(r"={2,}[^=]*={2,}", "Inventaire :", t)
    # vrais mots francais plutot que sigles epeles
    t = t.replace("CPU", "processeur")
    t = t.replace("RAM", "mémoire")
    t = t.replace("Noeud ", "Nœud ")
    t = t.replace("Uptime", "en marche depuis")
    t = _re.sub(r"\bCT\s?(\d)", r"conteneur \1", t)
    # unites et abreviations en francais parlé
    t = _re.sub(r"(\d)\s*giga octets\s*/\s*(\d)\s*giga octets", r"\1 giga octets sur \2 giga octets", t)
    t = _re.sub(r"(\d(?:[.,]\d+)?)\s*/\s*(\d(?:[.,]\d+)?)\s*giga octets", r"\1 giga octets sur \2 giga octets", t)
    t = _re.sub(r"(\d)G/(\d)G", r"\1 giga octets sur \2 giga octets", t)
    t = t.replace("Go", "giga octets")
    t = _re.sub(r"(\d)G\b", r"\1 giga octets", t)
    t = t.replace("giga octets/", "giga octets sur ")
    t = t.replace("%", " pour cent")
    # decimales a la francaise pour la TTS : 10.5 -> 10,5
    t = _re.sub(r"(\d)\.(\d)", r"\1,\2", t)
    # chaque ligne du rapport devient une phrase (pauses TTS naturelles)
    t = t.replace("\n\n", ". ").replace("\n", ". ")
    t = _re.sub(r"\s*:\s*", " : ", t)
    t = _re.sub(r"\.\s*\.", ".", t)
    return t
    t = _re.sub(r"(\d)\s*j\b", r"\1 jours", t)
    t = _re.sub(r"(\d)\s*h\b", r"\1 heures", t)
    t = t.replace("CPU", "C P U").replace("RAM", "R A M")
    t = t.replace("PVE", "P V E").replace("CT ", "C T ")
    # pourcentages : 42% -> 42 pour cent
    t = t.replace("%", " pour cent")
    # phrases : aérer pour la synthèse vocale
    t = t.replace(". ", ". ").replace("\n", " ")
    return t


def build_response(
    speech: str,
    should_end: bool,
    reprompt: str | None = None,
    session_attributes: dict | None = None,
) -> dict:
    response: dict = {
        "outputSpeech": {"type": "SSML", "ssml": f"<speak>{_escape(_voice_friendly(speech))}</speak>"},
        "shouldEndSession": should_end,
    }
    if not should_end and reprompt:
        response["reprompt"] = {
            "outputSpeech": {"type": "SSML", "ssml": f"<speak>{_escape(reprompt)}</speak>"}
        }
    return {
        "version": "1.0",
        "sessionAttributes": session_attributes or {},
        "response": response,
    }
