import asyncio
import logging
import os

import httpx
from telegram import Update, BotCommand
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {
    int(value.strip())
    for value in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if value.strip()
}
# Défauts = service Docker `vektor-api`. Dans le conteneur telegram,
# 127.0.0.1 est SON propre loopback (aucune API n'y écoute) : un défaut
# local rend la commande muette en prod (bug « impossible de sonder les
# résolveurs » v1.3.5). Toute nouvelle commande doit suivre ce contrat —
# voir tests/test_telegram_urls.py.
API_URL = os.environ.get("VEKTOR_API_URL", "http://vektor-api:8000/api/chat")
STATUS_URL = os.environ.get("VEKTOR_STATUS_URL", "http://vektor-api:8000/api/status")
MODEL_URL = os.environ.get("VEKTOR_MODEL_URL", "http://vektor-api:8000/api/model")
SEEDS_URL = os.environ.get("VEKTOR_SEEDS_URL", "http://vektor-api:8000/api/seeds")
QB_URL = os.environ.get("VEKTOR_QB_URL", "http://vektor-api:8000/api/qb/status")
BACKUPS_URL = os.environ.get("VEKTOR_BACKUPS_URL", "http://vektor-api:8000/api/backups")
DNS_URL = os.environ.get("VEKTOR_DNS_URL", "http://vektor-api:8000/api/dns")
MONITORING_URL = os.environ.get("VEKTOR_MONITORING_URL", "http://vektor-api:8000/api/monitoring")
OPS_URL = os.environ.get("VEKTOR_OPS_URL", "http://vektor-api:8000/api/ops")
PING_URL = os.environ.get("VEKTOR_PING_URL", "http://vektor-api:8000/api/ping")
RELOAD_URL = os.environ.get("VEKTOR_RELOAD_URL", "http://vektor-api:8000/api/reload-doc")
FORGET_URL = os.environ.get("VEKTOR_FORGET_URL", "http://vektor-api:8000/api/forget")
# Heartbeat du bot (auto-supervision v1.5.0) : URL de ping Healthchecks du
# check dédié au bot. Vide = désactivé. Si le bot crash, le check passe
# down et alerte — le crash du bot est invisible partout ailleurs.
HC_PING_URL = os.environ.get("VEKTOR_HC_PING_URL", "").strip()
API_TOKEN = os.environ.get("VEKTOR_API_TOKEN", "")
HEADERS = {"X-Vektor-Token": API_TOKEN}

logger = logging.getLogger("vektor.telegram")

# Commandes sondées au démarrage : (constante URL, endpoint, libellé Telegram)
_COMMAND_ENDPOINTS: list[tuple[str, str, str]] = [
    ("API_URL", "/api/chat", "chat"),
    ("STATUS_URL", "/api/status", "/status"),
    ("MODEL_URL", "/api/model", "/model"),
    ("SEEDS_URL", "/api/seeds", "/seeds"),
    ("QB_URL", "/api/qb/status", "/pause /resume"),
    ("BACKUPS_URL", "/api/backups", "/backups"),
    ("DNS_URL", "/api/dns", "/dns"),
    ("MONITORING_URL", "/api/monitoring", "/monitoring"),
    ("OPS_URL", "/api/ops", "/ops"),
    ("PING_URL", "/api/ping", "/ping"),
    ("RELOAD_URL", "/api/reload-doc", "/reload"),
    ("FORGET_URL", "/api/forget", "/forget"),
]


async def autotest_endpoints(transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """Auto-test au démarrage : chaque URL de commande doit toucher l'API.

    Deux phases, sans effet de bord :
    1. GET sans token sur chaque endpoint - 401 (auth requise) et 405
       (méthode) prouvent que le chemin existe ; 404 ou erreur de connexion
       = commande muette en prod (bug v1.3.6 : défauts 127.0.0.1).
       require_token est appelé AVANT tout traitement : aucune sonde,
       aucun LLM, aucune écriture ne s'exécute.
    2. GET /api/dns AVEC token (endpoint le plus léger, lecture seule) -
       vérifie le token réel : un token faux rendrait TOUTES les
       commandes muettes sans erreur visible.

    L'API et le bot démarrent en parallèle : en cas d'échec, la sonde est
    reprise (jusqu'à 4 essais, dernière attente de 8 s). Une panne n'est
    signalée que si elle persiste - pas la petite course de démarrage.
    """
    problems: list[str] = []
    retries = 4 if transport is None else 1
    for attempt in range(1, retries + 1):
        problems = []
        async with httpx.AsyncClient(timeout=5, transport=transport) as client:
            for attr, _endpoint, what in _COMMAND_ENDPOINTS:
                url = globals()[attr]
                try:
                    response = await client.get(url)
                except httpx.HTTPError as exc:
                    problems.append(
                        f"{what} : API injoignable sur {url} ({exc.__class__.__name__})"
                    )
                    continue
                if response.status_code == 404:
                    problems.append(
                        f"{what} : {url} renvoie 404 - endpoint inexistant (commande muette)"
                    )
                elif response.status_code not in (200, 401, 405):
                    problems.append(
                        f"{what} : {url} renvoie HTTP {response.status_code} (attendu 200/401/405)"
                    )
        async with httpx.AsyncClient(timeout=10, headers=HEADERS, transport=transport) as client:
            try:
                response = await client.get(DNS_URL)
            except httpx.HTTPError as exc:
                problems.append(f"sonde token : /api/dns injoignable ({exc.__class__.__name__})")
            else:
                if response.status_code == 401:
                    problems.append(
                        "sonde token : VEKTOR_API_TOKEN refusé par l'API (401 sur /api/dns) - "
                        "toutes les commandes seraient muettes"
                    )
                elif response.status_code != 200:
                    problems.append(f"sonde token : /api/dns renvoie HTTP {response.status_code}")
        if not problems:
            break
        if attempt < retries and transport is None:
            await asyncio.sleep(2.0 * attempt)
    return problems


def is_allowed(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ALLOWED)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_allowed(update) and update.message:
        await update.message.reply_text(
            "Vektor est prêt. Version lecture seule : je réponds aux questions "
            "générales, je retrouve la documentation du homelab et je vérifie "
            "l'état des services en direct. Envoie /help pour la liste."
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_allowed(update) and update.message:
        await update.message.reply_text(
            "Commandes :\n"
            "/start : vérifier que je réponds\n"
            "/status : rapport homelab complet en direct\n"
            "/model : ma fiche technique (modèle, hardware, latence)\n"
            "/seeds : top seeding qBittorrent + espace staging récupérable\n"
            "/pause : mettre les téléchargements en pause (confirmation OUI)\n"
            "/resume : reprendre les téléchargements (confirmation OUI)\n"
            "/backups : derniers backups vzdump (âge, taille)\n"
            "/dns : sonde des résolveurs DNS\n"
            "/monitoring : état des checks de monitoring\n"
            "/ops : exploitation de Vektor (version, conteneur, supervision)\n"
            "/ping : diagnostic du chemin LLM (bridge, modèle, inférence)\n"
            "/reload : réindexer la documentation (après modification)\n"
            "/help : cette aide\n"
            "/forget : effacer toute ma mémoire de conversation\n\n"
            "Exemples :\n"
            "- Quel est le rôle du CT 103 ?\n"
            "- Vérifie si Jellyfin répond\n"
            "- rescan la bibliothèque sonarr\n"
            "- mets les téléchargements en pause\n"
            "- redémarre tdarr\n"
            "- Explique-moi le DNS du réseau"
        )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(STATUS_URL, headers=HEADERS)
    except httpx.HTTPError:
        await update.message.reply_text("Impossible de générer le rapport pour le moment.")
        return
    if response.status_code != 200:
        await update.message.reply_text("Le rapport est indisponible (API).")
        return
    await send_long(update.message, response.json()["report"])


async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fiche technique réelle : modèle, hardware, latence — sans passer par le LLM."""
    if not is_allowed(update) or not update.message:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(MODEL_URL, headers=HEADERS)
    except httpx.HTTPError:
        await update.message.reply_text("Impossible de récupérer la fiche technique (API).")
        return
    if response.status_code != 200:
        await update.message.reply_text("La fiche technique est indisponible (API).")
        return
    await send_long(update.message, response.json()["card"])


async def seeds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rapport seeds : stats qBittorrent + staging récupérable — sans LLM."""
    if not is_allowed(update) or not update.message:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(SEEDS_URL, headers=HEADERS)
    except httpx.HTTPError:
        await update.message.reply_text("Impossible de générer le rapport seeds (API).")
        return
    if response.status_code != 200:
        await update.message.reply_text("Le rapport seeds est indisponible (API).")
        return
    await send_long(update.message, response.json()["report"])


async def backups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Derniers backups vzdump — sans LLM, lecture seule via API Proxmox."""
    if not is_allowed(update) or not update.message:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(BACKUPS_URL, headers=HEADERS)
    except httpx.HTTPError:
        await update.message.reply_text("Impossible de générer le rapport backups (API).")
        return
    if response.status_code != 200:
        await update.message.reply_text("Le rapport backups est indisponible (API).")
        return
    await send_long(update.message, response.json()["report"])


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnostic du chemin LLM : bridge, modèle, RAM, inférence réelle."""
    if not is_allowed(update) or not update.message:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.get(PING_URL, headers=HEADERS)
    except httpx.HTTPError:
        await update.message.reply_text("Diagnostic impossible (API injoignable — c'est déjà un indice !)")
        return
    if response.status_code != 200:
        await update.message.reply_text("Diagnostic indisponible (API).")
        return
    await send_long(update.message, response.json()["report"])


async def dns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sonde DNS des résolveurs configurés — sans LLM."""
    if not is_allowed(update) or not update.message:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(DNS_URL, headers=HEADERS)
    except httpx.HTTPError:
        await update.message.reply_text("Impossible de sonder les résolveurs (API).")
        return
    if response.status_code != 200:
        await update.message.reply_text("Le rapport DNS est indisponible (API).")
        return
    await send_long(update.message, response.json()["report"])


async def monitoring_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """État des checks Healthchecks.io — sans LLM, token lecture dédié."""
    if not is_allowed(update) or not update.message:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(MONITORING_URL, headers=HEADERS)
    except httpx.HTTPError:
        await update.message.reply_text("Impossible de lire le monitoring (API).")
        return
    if response.status_code != 200:
        await update.message.reply_text("Le rapport monitoring est indisponible (API).")
        return
    await send_long(update.message, response.json()["report"])


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause globale qBittorrent — proposition via l'API, exécution sur OUI."""
    await _qb_cmd(update, context, "pause")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reprise globale qBittorrent — proposition via l'API, exécution sur OUI."""
    await _qb_cmd(update, context, "resume")


async def _qb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, verb: str):
    """Relais pur : l'API (qui seule a la clé SSH et le registre OUI)
    renvoie le texte de proposition ; la confirmation « OUI » suit ensuite
    le chemin normal du chat (handle_message -> /api/chat -> graph)."""
    if not is_allowed(update) or not update.message:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{QB_URL.rsplit('/', 1)[0]}/{verb}/proposal",
                params={"user_id": str(update.effective_user.id), "channel": "telegram"},
                headers=HEADERS,
            )
    except httpx.HTTPError:
        await update.message.reply_text("API indisponible pour préparer l'action.")
        return
    if response.status_code != 200:
        await update.message.reply_text("Impossible de préparer l'action (API).")
        return
    await update.message.reply_text(response.json()["proposal"])


async def ops_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exploitation de Vektor : version déployée, conteneur, supervision — sans LLM."""
    if not is_allowed(update) or not update.message:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(OPS_URL, headers=HEADERS)
    except httpx.HTTPError:
        await update.message.reply_text("Impossible de lire le rapport d'exploitation (API).")
        return
    if response.status_code != 200:
        await update.message.reply_text("Le rapport d'exploitation est indisponible (API).")
        return
    await send_long(update.message, response.json()["report"])


async def reload_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réindexe knowledge-live sans redémarrage (après édition de la doc)."""
    if not is_allowed(update) or not update.message:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(RELOAD_URL, headers=HEADERS)
    except httpx.HTTPError:
        await update.message.reply_text("Réindexation impossible (API injoignable).")
        return
    if response.status_code == 200:
        await update.message.reply_text(response.json()["report"])
    else:
        await update.message.reply_text(
            "Réindexation échouée — l'index précédent reste actif (aucune interruption)."
        )


async def forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message:
        return
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            FORGET_URL,
            json={"user_id": str(update.effective_user.id)},
            headers=HEADERS,
        )
    if response.status_code == 200:
        await update.message.reply_text("Mémoire effacée : toutes les conversations liées à ton compte sont supprimées.")
    else:
        await update.message.reply_text("Impossible d'effacer la mémoire pour le moment.")


async def send_long(message, text: str) -> None:
    for i in range(0, max(len(text), 1), 3800):
        await message.reply_text(text[i : i + 3800])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update) or not update.message or not update.message.text:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    payload = {
        "user_id": str(update.effective_user.id),
        "channel": "telegram",
        "text": update.message.text,
    }
    try:
        # 360 s : une réponse multi-tâches peut enchaîner plusieurs tours
        # d'outils (v1.4.0), soit 3+ inférences LLM sur CPU.
        async with httpx.AsyncClient(timeout=360) as client:
            response = await client.post(API_URL, json=payload, headers=HEADERS)
    except httpx.HTTPError:
        await update.message.reply_text("Je n'arrive pas à joindre mon cerveau (API). Réessaie dans un instant.")
        return
    if response.status_code != 200:
        await update.message.reply_text("Vektor est temporairement indisponible.")
        return
    await send_long(update.message, response.json()["response"])


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Vérifier que Vektor répond"),
            BotCommand("help", "Liste des commandes et exemples"),
            BotCommand("status", "Rapport homelab complet en direct"),
            BotCommand("model", "Fiche technique de Vektor (modèle, hardware, latence)"),
            BotCommand("seeds", "Top seeding qBittorrent + espace staging récupérable"),
            BotCommand("pause", "Mettre les téléchargements en pause"),
            BotCommand("resume", "Reprendre les téléchargements"),
            BotCommand("backups", "Derniers backups vzdump (âge, taille)"),
            BotCommand("dns", "Sonde des résolveurs DNS"),
            BotCommand("monitoring", "État des checks de monitoring"),
            BotCommand("ops", "Exploitation de Vektor (version, supervision)"),
            BotCommand("forget", "Effacer la mémoire des conversations"),
        ]
    )

    # Auto-test : un défaut d'URL ne doit plus rester invisible (v1.3.6).
    problems = await autotest_endpoints()
    if problems:
        detail = "\n".join(f"  - {p}" for p in problems)
        logger.error(
            "AUTO-TEST démarrage : %d problème(s) — commande(s) muette(s) en prod !\n%s",
            len(problems),
            detail,
        )
        alert = "🚨 Vektor — auto-test démarrage : commandes muettes !\n" + "\n".join(
            f"• {p}" for p in problems
        )
        chat_id = next(iter(ALLOWED), None)
        if chat_id is not None:
            try:
                await application.bot.send_message(chat_id=chat_id, text=alert)
            except Exception:
                logger.exception("Notification auto-test impossible")
    else:
        total = len(_COMMAND_ENDPOINTS)
        logger.info(
            "AUTO-TEST démarrage : %d/%d commandes -> API OK (token vérifié via sonde DNS)",
            total,
            total,
        )

    application.create_task(_heartbeat_loop())
    application.create_task(_periodic_autotest_loop(application))


async def _heartbeat_loop(interval: int = 300) -> None:
    """Ping le check Healthchecks du bot toutes les 5 min : si le bot meurt,
    le check passe down et alerte tout seul (auto-supervision)."""
    if not HC_PING_URL:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(HC_PING_URL)
            logger.info("heartbeat Healthchecks : HTTP %s", response.status_code)
        except Exception as exc:
            logger.warning("heartbeat Healthchecks impossible : %s", exc.__class__.__name__)


async def _periodic_autotest_loop(
    application: Application,
    interval: int = 3600,
    reminder_every: int = 6,
) -> None:
    """Rejoue l'auto-test toutes les heures (l'API peut mourir en cours de vie).

    Alerte Telegram sur transition OK -> KO, rappel toutes les
    `reminder_every` heures tant que ça dure, message de rétablissement."""
    was_ok = True
    failures_in_a_row = 0
    while True:
        await asyncio.sleep(interval)
        problems = await autotest_endpoints()
        if problems:
            failures_in_a_row += 1
            logger.error(
                "AUTO-TEST périodique : %d problème(s)\n%s",
                len(problems),
                "\n".join(f"  - {p}" for p in problems),
            )
            if was_ok or failures_in_a_row % reminder_every == 0:
                was_ok = False
                await _alert_owner(application, "🚨 Vektor — auto-test périodique : commandes muettes !\n" + "\n".join(f"• {p}" for p in problems))
        else:
            if not was_ok:
                logger.info("AUTO-TEST périodique : rétabli")
                await _alert_owner(application, "🟢 Vektor — auto-test rétabli : toutes les commandes répondent à nouveau.")
            was_ok = True
            failures_in_a_row = 0


async def _alert_owner(application: Application, text: str) -> None:
    chat_id = next(iter(ALLOWED), None)
    if chat_id is None:
        return
    try:
        await application.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.exception("Notification propriétaire impossible")


if __name__ == "__main__":
    # Logging visible dans docker logs : l'auto-test de démarrage doit laisser
    # une trace lisible (INFO), sans le bruit des librairies (polling HTTP).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    if not TOKEN or not ALLOWED:
        raise SystemExit("Telegram désactivé : token ou whitelist manquant.")
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("model", model_cmd))
    application.add_handler(CommandHandler("seeds", seeds_cmd))
    application.add_handler(CommandHandler("backups", backups_cmd))
    application.add_handler(CommandHandler("dns", dns_cmd))
    application.add_handler(CommandHandler("monitoring", monitoring_cmd))
    application.add_handler(CommandHandler("ops", ops_cmd))
    application.add_handler(CommandHandler("pause", pause_cmd))
    application.add_handler(CommandHandler("resume", resume_cmd))
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("reload", reload_doc))
    application.add_handler(CommandHandler("forget", forget))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling(allowed_updates=Update.ALL_TYPES)
