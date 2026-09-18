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
            "/help : cette aide\n"
            "/forget : effacer toute ma mémoire de conversation\n\n"
            "Exemples :\n"
            "- Quel est le rôle du CT 103 ?\n"
            "- Vérifie si Jellyfin répond\n"
            "- rescan la bibliothèque sonarr\n"
            "- mets les téléchargements en pause\n"
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
        async with httpx.AsyncClient(timeout=120) as client:
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
    application.add_handler(CommandHandler("forget", forget))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling(allowed_updates=Update.ALL_TYPES)
