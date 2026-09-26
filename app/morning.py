"""Rapport matinal narratif — « la nuit s'est bien passée, voici pourquoi ».

Contrairement à /status (exhaustif) et /ops (exploitation), /matin choisit
les 5-8 lignes qui comptent après la nuit : verdicts vzdump/backups via
Healthchecks, saturation LVM avec tendance, alertes actives. Aucun LLM :
les chiffres viennent des API live (PVE + Healthchecks), la mise en
récit est déterministe.
"""
from __future__ import annotations

import os
import time

import httpx

HC_API_URL = os.environ.get("VEKTOR_HC_API_URL", "")
HC_READ_TOKEN = os.environ.get("VEKTOR_HC_READ_TOKEN", "")
PVE_API_URL = os.environ.get("PVE_API_URL", "https://PVE_HOST:8006")
PVE_API_TOKEN = os.environ.get("PVE_API_TOKEN", "")
PVE_VERIFY_SSL = os.environ.get("PVE_VERIFY_SSL", "false").lower() != "true"


async def _hc_checks() -> list[dict] | None:
    if not (HC_API_URL and HC_READ_TOKEN):
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                f"{HC_API_URL}/api/v3/checks/",
                headers={"X-Api-Key": HC_READ_TOKEN},
            )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json().get("checks") or []
    except ValueError:
        return None


async def _pve_json(path: str):
    if not PVE_API_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=40, verify=PVE_VERIFY_SSL,
            headers={"Authorization": f"PVEAPIToken={PVE_API_TOKEN}"},
        ) as client:
            response = await client.get(f"{PVE_API_URL}/api2/json{path}")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    return response.json().get("data")


def _parse_textfile(raw: str) -> tuple[dict[str, float], dict[str, float]]:
    """Extrait (pools_thin, vg_free) d'un .prom du textfile collector."""
    pools: dict[str, float] = {}
    vg_free: dict[str, float] = {}
    for raw_line in raw.splitlines():
        if raw_line.startswith("lvm_thin_data_percent{"):
            try:
                pool = raw_line.split('pool="')[1].split('"')[0]
                pools[pool] = float(raw_line.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                continue
        elif raw_line.startswith("lvm_vg_free_bytes{"):
            try:
                vg = raw_line.split('vg="')[1].split('"')[0]
                vg_free[vg] = float(raw_line.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                continue
    return pools, vg_free


async def _lvm_lines() -> list[str]:
    """Thin pools + VG libres via node_exporter (textfile lvm_thin.prom)."""
    lines: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get("http://192.168.1.60:9100/metrics")
        if response.status_code != 200:
            return lines
        pools, vg_free = _parse_textfile(response.text)
    except (httpx.HTTPError, ValueError):
        return lines

    for pool, value in sorted(pools.items()):
        if value >= 90:
            lines.append(f"🔴 {pool} : {value:.0f}% — action requise (fstrim, purge, extension)")
        elif value >= 85:
            lines.append(f"🟡 {pool} : {value:.0f}% — au seuil d'alerte, à surveiller")
        elif value >= 70:
            lines.append(f"🟢 {pool} : {value:.0f}% — confortable, trim mensuel en place")
    for vg, free in vg_free.items():
        if free < 1024**3:
            lines.append(f"🟡 VG {vg} : {(free / 2**30):.0f}G libres (extension impossible tant que ça ne bouge pas)")
    return lines


async def morning_report() -> str:
    """Le récit du matin, chiffré et hiérarchisé."""
    lines: list[str] = ["🌅 **Bonjour — la nuit en résumé**"]

    # 1. Verdict de la nuit via Healthchecks (source de vérité)
    checks = await _hc_checks()
    down: list[str] = []
    late: list[str] = []
    backups_ok = None
    if checks is None:
        lines.append("🩺 Monitoring : injoignable — passe par /monitoring pour le détail")
    else:
        for check in checks:
            name = str(check.get("name", "?"))
            status = str(check.get("status", ""))
            if status == "down":
                down.append(name)
            elif status == "late":
                late.append(name)
            if name in ("vzdump-backup", "backup-s3"):
                backups_ok = backups_ok is not False and status in ("up", "late")
        if not down and not late:
            lines.append(f"🩺 Les {len(checks)} checks de la nuit sont verts (backup, coffre, sonde, API)")
        else:
            if down:
                lines.append(f"🔴 Checks DOWN : {', '.join(down)}")
            if late:
                lines.append(f"🟡 En retard : {', '.join(late)}")
        if backups_ok:
            lines.append("💾 La chaîne vzdump + miroir GDrive a tourné")

    # 2. Stockage PVE : thin pools au-dessus de 70%
    lvm = await _lvm_lines()
    if lvm:
        lines.append("🗄️ Stockage PVE :")
        lines.extend(f"   {line}" for line in lvm)

    # 3. Conclusion adaptive
    if not down:
        lines.append("✅ Rien à faire ce matin.")
    else:
        lines.append("👉 Commence par les checks DOWN ci-dessus, je peux diagnostiquer si tu me demandes.")

    return "\n".join(lines)
