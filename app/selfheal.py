"""Auto-réparation Vektor : webhook Grafana -> diagnostic -> action sûre.

Principe : alertes critiques uniquement, actions explicitement allowlistées,
anti-tempête et rapports Telegram narratifs. Les réparations média sont
vérifiées côté PVE et ne modifient ni données ni configuration.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import httpx

from . import actions

TG_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_COOLDOWN_S = 1800
_LAST_RUN: dict[str, float] = {}
_ACTIVE_ALERTS: dict[str, dict[str, str]] = {}
logger = logging.getLogger("vektor.selfheal")
_MEDIA_ACTIONS = {"selfheal_media_jellyfin", "selfheal_media_immich", "selfheal_media_authelia"}


async def _tg(text: str) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logger.warning("Rapport d'incident non envoyé : configuration Telegram absente")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for offset in range(0, max(len(text), 1), 3800):
                response = await client.post(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                    data={"chat_id": TG_CHAT_ID, "text": text[offset : offset + 3800]},
                )
                response.raise_for_status()
                if not response.json().get("ok"):
                    raise httpx.HTTPError("Telegram API a refusé l'envoi")
        return True
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Envoi du rapport Telegram impossible: %s", exc.__class__.__name__)
        return False


def _alert_key(alert: dict, title: str) -> str:
    return str(alert.get("fingerprint") or title or "alerte inconnue")


def _probable_cause(title: str, annotations: dict) -> str:
    t = title.lower()
    if "thin pool lvm" in t:
        return "Le thin pool LVM a dépassé son seuil critique de capacité."
    if "qbittorrent" in t and ("unhealthy" in t or "down" in t):
        return "Le conteneur qBittorrent a échoué à son healthcheck ou ne répond plus."
    if "vektor-telegram" in t and "down" in t:
        return "Le contrôle de santé du bot Telegram Vektor est en échec."
    if "jellyfin" in t:
        return "Le contrôle de santé de Jellyfin (systemd et endpoint local) est en échec."
    if "immich" in t:
        return "Le contrôle de santé de l’API Immich est en échec."
    if "backup" in t and any(w in t for w in ("échec", "echec", "failed")):
        return "Le contrôle signale un échec de sauvegarde ; la cause exacte reste à diagnostiquer."
    if "authelia" in t or "auth.mayoraz-net.ch" in t or any(
        w.strip(".,:;!()[]-") in ("auth", "sso") for w in t.split()
    ):
        return "Le contrôle de santé du SSO Authelia est en échec ; le gardien sso-switch (VPS) peut déjà servir les sites depuis la réplique."
    detail = str(annotations.get("description") or annotations.get("summary") or "").strip()
    if detail:
        return f"Cause non déterminée automatiquement. Détail Grafana : {detail[:400]}"
    return "Cause non déterminée automatiquement à partir du nom de l’alerte."


def _action_label(action: str) -> str:
    return {
        "pve_fstrim": "lancer fstrim sur le stockage Proxmox",
        "docker_restart_107_qbittorrent": "redémarrer qBittorrent (CT 107)",
        "bot_recreate": "déclencher le watchdog du bot Vektor Telegram",
        "selfheal_media_jellyfin": "vérifier puis redémarrer Jellyfin (CT 102, systemd)",
        "selfheal_media_immich": "vérifier puis redémarrer le serveur Immich (CT 109)",
        "selfheal_media_authelia": "vérifier puis redémarrer Authelia (CT 104, SSO)",
    }.get(action, action)


def _action_failed(result: str) -> bool:
    lowered = result.strip().lower()
    return any(marker in lowered for marker in (
        "refus", "indisponible", "échec", "echec", "erreur", "error",
        "timeout", "unhealthy", "max_attempts", "unhealthy:",
    ))


def _duration(started_at: str | None, ended_at: datetime) -> str | None:
    if not started_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        seconds = max(0, int((ended_at - start).total_seconds()))
    except (TypeError, ValueError):
        return None
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes} min {remainder} s" if minutes else f"{remainder} s"


def _incident_report(
    *, title: str, severity: str, annotations: dict, action: str | None = None,
    action_result: str = "", action_error: str = "", skipped_reason: str = "",
    detected_at: str = "", started_at: str | None = None,
    prior_action: str | None = None, prior_result: str = "", prior_error: str = "",
    prior_skipped_reason: str = "", resolved: bool = False,
) -> str:
    """Récit déterministe, distinguant l’action de la preuve santé."""
    cause = _probable_cause(title, annotations)
    detail = str(annotations.get("summary") or annotations.get("description") or "").strip()
    lines = ["🧾 Rapport d’incident Vektor", f"Alerte : {title or 'alerte sans titre'}", f"Gravité : {severity or 'non précisée'}"]
    if resolved:
        duration = _duration(started_at, datetime.now(timezone.utc))
        lines.extend([
            "État : rétablissement détecté par Grafana.",
            f"Détecté le : {detected_at or 'heure non fournie'}" + (f" ; durée : {duration}." if duration else ". Durée indisponible."),
            f"Cause probable : {cause}",
        ])
        if prior_action:
            lines.append(f"Action prise : {_action_label(prior_action)}.")
            if prior_action in _MEDIA_ACTIONS:
                lines.append("Garde-fous : cooldown 30 min, deux tentatives maximum ; aucune restauration de données/configuration, DB ou photos.")
                if prior_action == "selfheal_media_authelia":
                    lines.append("Rollback de données : volontairement absent ; la DB (CT 106) et la configuration Authelia ne sont pas touchées.")
                else:
                    lines.append("Rollback de données : volontairement absent pour protéger Immich et Jellyfin.")
            if prior_error:
                lines.extend([f"Résultat de l’action : échec ({prior_error[:300]}).", "Le retour résolu a ensuite été observé par Grafana."])
            elif prior_result:
                lines.append(f"Résultat de l’action : {prior_result[:400]}")
            else:
                lines.append("Résultat de l’action : inconnu (Vektor a pu redémarrer avant le retour Grafana).")
        elif prior_skipped_reason:
            lines.append(f"Action prise : aucune réparation automatique ({prior_skipped_reason}).")
        else:
            lines.append("Action de Vektor : aucune nouvelle réparation au signal de rétablissement.")
        lines.extend([
            "Résultat : Grafana signale l’alerte résolue ; vérifier Kuma/Grafana si l’incident a eu un impact utilisateur.",
            "Conseil : surveiller une récidive et consulter les journaux autour de l’heure de l’alerte.",
        ])
    else:
        lines.extend(["État : panne/condition anormale détectée."])
        if detected_at:
            lines.append(f"Détecté le : {detected_at}")
        lines.append(f"Cause probable : {cause}")
        if action:
            lines.append(f"Action prise : {_action_label(action)}.")
            if action_error:
                lines.extend([f"Résultat : échec de l’action ou escalade opérateur ({action_error[:350]}).", "Conseil : vérifier l’accès SSH/actions et intervenir manuellement si le service reste indisponible."])
            elif action_result:
                if action_result.startswith("HEALTHY:"):
                    lines.append(f"Résultat : santé confirmée par contrôle local. {action_result[:450]}")
                elif _action_failed(action_result):
                    lines.append(f"Résultat : la commande a renvoyé une erreur ; succès fonctionnel non vérifié. Détail : {action_result[:450]}")
                elif action not in _MEDIA_ACTIONS:
                    lines.append(f"Résultat : retour reçu, succès fonctionnel non vérifié. Détail : {action_result[:450]}")
                elif action_result.startswith("ATTEMPT ") and "HEALTHY:" in action_result:
                    lines.append(f"Résultat : redémarrage et contrôle santé réussis. {action_result[:450]}")
                elif action_result.startswith("COOLDOWN:"):
                    lines.append(f"Résultat : aucune nouvelle action, cooldown actif. {action_result[:350]}")
                else:
                    lines.append(f"Résultat : {action_result[:500]}")
                if action in _MEDIA_ACTIONS:
                    if action == "selfheal_media_authelia":
                        lines.append("Garde-fous : cooldown 30 min et deux tentatives maximum ; seul le conteneur Authelia est redémarré, la DB (CT 106) et la configuration ne sont jamais touchées.")
                    else:
                        lines.append("Garde-fous : cooldown 30 min et deux tentatives maximum par service ; aucun rollback de données/configuration, DB ou photos.")
                lines.append("Vérification : le webhook ne remplace pas le suivi Grafana/Kuma.")
                lines.append("Conseil : si l’alerte persiste, lancer /diag puis consulter les journaux du service.")
            else:
                lines.append("Résultat : action déclenchée, retour non reçu ; contrôler Kuma/Grafana.")
        else:
            reason = skipped_reason or "aucune règle d’auto-réparation sûre ne correspond"
            lines.extend([
                f"Action prise : aucune réparation automatique ({reason}).",
                "Résultat : Vektor n’a pas modifié le serveur.",
                "Conseil : lancer /diag (ou /diag services), puis consulter les journaux.",
            ])
    if detail and not resolved:
        lines.append(f"Contexte Grafana : {detail[:400]}")
    return "\n".join(lines)


def _match_rule(title: str) -> str | None:
    t = title.lower()
    if "thin pool lvm" in t and "92%" in t:
        return "pve_fstrim"
    if "qbittorrent" in t and ("unhealthy" in t or "down" in t):
        return "docker_restart_107_qbittorrent"
    if "vektor-telegram" in t and "down" in t:
        return "bot_recreate"
    if "backup" in t and any(w in t for w in ("échec", "echec", "failed")):
        return None
    fault = ("down", "unhealthy", "indisponible", "offline", "panne", "échec", "echec", "failed", "not up")
    if "jellyfin" in t and any(marker in t for marker in fault):
        return "selfheal_media_jellyfin"
    if "immich" in t and any(marker in t for marker in fault):
        return "selfheal_media_immich"
    if ("authelia" in t or "auth.mayoraz-net.ch" in t or any(
        w.strip(".,:;!()[]-") in ("auth", "sso") for w in t.split()
    )) and any(marker in t for marker in fault):
        return "selfheal_media_authelia"
    return None


async def _run_media_action(action: str) -> tuple[str, str, str]:
    """Retourne (résultat, erreur rapportable, raison de skip)."""
    try:
        result = (await actions._execute(action)).strip()
    except Exception as exc:
        logger.exception("Auto-réparation média échouée")
        return f"ERROR: {exc.__class__.__name__}: {str(exc)[:250]}", f"{exc.__class__.__name__}: {str(exc)[:250]}", ""
    if result.startswith("COOLDOWN:"):
        return result, "", "cooldown média anti-tempête actif (30 min)"
    if result.startswith("MAX_ATTEMPTS:"):
        return result, result[:250], "deux tentatives déjà effectuées ; escalade opérateur nécessaire"
    if result.startswith(("ERROR:", "UNHEALTHY:")):
        return result, result[:250], "service toujours indisponible après contrôle local"
    if result.startswith("ATTEMPT ") and "HEALTHY:" not in result:
        return result, result[:250], "service toujours indisponible après redémarrage"
    if not result.startswith(("HEALTHY:", "ATTEMPT ")):
        return result, result[:250] or "réponse d’action vide/inconnue", "résultat d’action non reconnu"
    return result, "", ""


async def handle_grafana_webhook(payload: dict) -> dict:
    alerts = payload.get("alerts") or []
    acted: list[str] = []
    skipped: list[str] = []
    reports: list[str] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            skipped.append("?")
            continue
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        title = str(alert.get("title") or labels.get("alertname", ""))
        severity = str(labels.get("severity", "")).lower()
        status = str(alert.get("status", "")).lower()
        key = _alert_key(alert, title)

        if status == "resolved":
            previous = _ACTIVE_ALERTS.pop(key, None)
            media_service = (previous or {}).get("media_service", "")
            if media_service:
                try:
                    reset_action = f"selfheal_media_reset_{media_service}"
                    reset = (await actions._execute(reset_action)).strip()
                    if previous is not None:
                        previous["action_result"] = (previous.get("action_result", "") + "\n" + reset).strip()
                except Exception as exc:
                    logger.warning("Réinitialisation budget média impossible: %s", exc.__class__.__name__)
            if previous is None:
                skipped.append(f"{title or '?'} (résolution sans incident suivi)")
                continue
            reports.append(_incident_report(
                title=title, severity=severity or previous.get("severity", ""),
                annotations=annotations, detected_at=str(alert.get("startsAt") or previous.get("detected_at", "")),
                started_at=previous.get("detected_at"), prior_action=previous.get("action") or None,
                prior_result=previous.get("action_result", ""), prior_error=previous.get("action_error", ""),
                prior_skipped_reason=previous.get("skipped_reason", ""), resolved=True,
            ))
            continue
        if status != "firing":
            skipped.append(title or "?")
            continue

        action = _match_rule(title) if severity == "critical" else None
        previous = _ACTIVE_ALERTS.get(key)
        if previous is not None:
            if action in _MEDIA_ACTIONS and previous.get("health_confirmed"):
                skipped.append(f"{title or '?'} (santé locale déjà confirmée, attente de résolution Grafana)")
                continue
            if action in _MEDIA_ACTIONS and not previous.get("attempt_limit_reported"):
                last_call = float(previous.get("media_last_call", "0") or 0)
                now = time.monotonic()
                if last_call and now - last_call < _COOLDOWN_S:
                    skipped.append(f"{title or '?'} (cooldown média anti-tempête)")
                    continue
                previous["media_last_call"] = str(now)
                result, error, reason = await _run_media_action(action)
                if reason == "cooldown média anti-tempête actif (30 min)":
                    skipped.append(f"{title} (cooldown)")
                    continue
                if result.startswith(("HEALTHY:", "ATTEMPT ")) and not error:
                    previous["health_confirmed"] = "1"
                if reason == "deux tentatives déjà effectuées ; escalade opérateur nécessaire":
                    previous["attempt_limit_reported"] = "1"
                previous.update({
                    "action": action,
                    "media_service": action.removeprefix("selfheal_media_"),
                    "action_result": result,
                    "action_error": error,
                    "skipped_reason": reason,
                })
                reports.append(_incident_report(
                    title=title, severity=severity, annotations=annotations, action=action,
                    action_result=result, action_error=error, skipped_reason=reason,
                    detected_at=previous.get("detected_at", ""),
                ))
                if reason == "cooldown média anti-tempête actif (30 min)":
                    skipped.append(f"{title} (cooldown)")
                elif error:
                    skipped.append(f"{title} -> {reason}")
                else:
                    acted.append(f"{title} -> {action} : {result[:200]}")
                continue
            skipped.append(f"{title or '?'} (rapport déjà envoyé pour cette alerte)")
            continue

        skipped_reason = ""
        action_result = ""
        action_error = ""
        if severity != "critical":
            skipped_reason = "la réparation automatique est réservée aux alertes critiques"
            skipped.append(title or "?")
            action = None
        elif action is None:
            skipped_reason = "aucune règle d’auto-réparation sûre ne correspond à cette alerte"
            skipped.append(title or "?")
        elif action in _MEDIA_ACTIONS:
            media_last_call = time.monotonic()
            action_result, action_error, skipped_reason = await _run_media_action(action)
            if skipped_reason:
                skipped.append(f"{title} -> {skipped_reason}")
            else:
                acted.append(f"{title} -> {action} : {action_result[:200]}")
        else:
            now = time.monotonic()
            last = _LAST_RUN.get(action)
            if last is not None and now - last < _COOLDOWN_S:
                skipped_reason = "cooldown anti-tempête actif (30 min)"
                skipped.append(f"{title} (cooldown)")
                action = None
            else:
                _LAST_RUN[action] = now
                try:
                    if action == "bot_recreate":
                        proc = await asyncio.create_subprocess_exec(
                            "systemctl", "start", "bot-watchdog.service",
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        code = await proc.wait()
                        action_result = f"bot-watchdog.service déclenché (code de sortie {code})"
                        if code != 0:
                            action_error = f"systemctl start a renvoyé {code}"
                    else:
                        action_result = (await actions._execute(action)).strip()
                    if action_error:
                        skipped.append(f"{title} -> {action_error}")
                    elif _action_failed(action_result):
                        action_error = action_result[:250]
                        skipped.append(f"{title} -> action renvoyée en échec")
                    else:
                        acted.append(f"{title} -> {action} : {action_result[:200]}")
                except Exception as exc:
                    logger.exception("Auto-réparation échouée pour %s", title)
                    action_error = f"{exc.__class__.__name__}: {str(exc)[:250]}"
                    skipped.append(f"{title} -> erreur d’action")

        detected_at = str(alert.get("startsAt") or datetime.now(timezone.utc).isoformat(timespec="seconds"))
        _ACTIVE_ALERTS[key] = {
            "health_confirmed": "1" if action in _MEDIA_ACTIONS and (action_result.startswith("HEALTHY:") or (action_result.startswith("ATTEMPT ") and "HEALTHY:" in action_result)) else "",
            "title": title,
            "severity": severity,
            "detected_at": detected_at,
            "started_at": detected_at,
            "action": action or "",
            "media_service": action.removeprefix("selfheal_media_") if action in _MEDIA_ACTIONS else "",
            "media_last_call": str(media_last_call) if action in _MEDIA_ACTIONS else "",
            "attempt_limit_reported": "1" if action in _MEDIA_ACTIONS and action_error.startswith("MAX_ATTEMPTS:") else "",
            "action_result": action_result,
            "action_error": action_error,
            "skipped_reason": skipped_reason,
        }
        reports.append(_incident_report(
            title=title, severity=severity, annotations=annotations, action=action,
            action_result=action_result, action_error=action_error,
            skipped_reason=skipped_reason, detected_at=detected_at,
        ))

    reports_sent = 0
    for report in reports:
        if await _tg(report):
            reports_sent += 1
    return {"acted": acted, "skipped": skipped, "reports_generated": len(reports), "reports_sent": reports_sent}


def detect_cancel_qbit(text: str) -> bool:
    """« annule la pause des torrents » variation non couverte par resume."""
    import re
    return bool(re.search(
        r"\bannule?[rs]?\s+(?:la\s+)?pause\s+(?:des\s+|de\s+)?(?:torrents?t?s?|t[ée]l[ée]chargements?)\b",
        text, re.I,
    ))
