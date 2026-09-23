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
API_URL = os.environ.get("VEKTOR_API_URL", "http://127.0.0.1:8000/api/chat")
STATUS_URL = os.environ.get("VEKTOR_STATUS_URL", "http://127.0.0.1:8000/api/status")
MODEL_URL = os.environ.get("VEKTOR_MODEL_URL", "http://127.0.0.1:8000/api/model")
SEEDS_URL = os.environ.get("VEKTOR_SEEDS_URL", "http://127.0.0.1:8000/api/seeds")
BACKUPS_URL = os.environ.get("VEKTOR_BACKUPS_URL", "http://127.0.0.1:8000/api/backups")
DNS_URL = os.environ.get("VEKTOR_DNS_URL", "http://127.0.0.1:8000/api/dns")
MONITORING_URL = os.environ.get("VEKTOR_MONITORING_URL", "http://127.0.0.1:8000/api/monitoring")
PING_URL = os.environ.get("VEKTOR_PING_URL", "http://127.0.0.1:8000/api/ping")
RELOAD_URL = os.environ.get("VEKTOR_RELOAD_URL", "http://127.0.0.1:8000/api/reload-doc")
FORGET_URL = os.environ.get("VEKTOR_FORGET_URL", "http://127.0.0.1:8000/api/forget")
API_TOKEN = os.environ.get("VEKTOR_API_TOKEN", "")
HEADERS = {"X-Vektor-Token": API_TOKEN}


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
            "/backups : derniers backups vzdump (âge, taille)\n"
            "/dns : sonde des résolveurs DNS\n"
            "/monitoring : état des checks de monitoring\n"
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
        # 240 s : une réponse avec outil fait passer deux inférences LLM
        # (décision d'appel puis rédaction) — sur CPU, ça peut dépasser 2 min.
        async with httpx.AsyncClient(timeout=240) as client:
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
            BotCommand("backups", "Derniers backups vzdump (âge, taille)"),
            BotCommand("dns", "Sonde des résolveurs DNS"),
            BotCommand("monitoring", "État des checks de monitoring"),
            BotCommand("forget", "Effacer la mémoire des conversations"),
        ]
    )


if __name__ == "__main__":
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
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("reload", reload_doc))
    application.add_handler(CommandHandler("forget", forget))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling(allowed_updates=Update.ALL_TYPES)
