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

from . import actions as actions_mod

HC_API_URL = os.environ.get("VEKTOR_HC_API_URL", "")
HC_READ_TOKEN = os.environ.get("VEKTOR_HC_READ_TOKEN", "")
PVE_API_URL = os.environ.get("PVE_API_URL", "https://PVE_HOST:8006")
PVE_API_TOKEN = os.environ.get("PVE_API_TOKEN", "")
PVE_VERIFY_SSL = os.environ.get("PVE_VERIFY_SSL", "false").lower() != "true"
PROM_URL = os.environ.get("VEKTOR_PROM_URL", "http://100.70.222.73:9090")


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


async def _prom_query(query: str) -> float | None:
    """Une seule valeur depuis l'API Prometheus du VPS (None si KO)."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                f"{PROM_URL}/api/v1/query", params={"query": query}
            )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        result = response.json().get("data", {}).get("result", [])
        if not result:
            return None
        return float(result[0]["value"][1])
    except (ValueError, KeyError, IndexError):
        return None


async def _vps_ram_lines() -> list[str]:
    """RAM VPS (dispo + swap) via node_exporter du VPS, avec tendance 24h.

    Seuils alignés sur la règle RAM-GUARD : alerte push < 5G dispo (le modèle
    LLM réserve ~5G lors de ses inférences, MC a Xmx10G)."""
    avail = await _prom_query(
        'node_memory_MemAvailable_bytes{job="vps-node-exporter"}'
    )
    if avail is None:
        return []
    swap_used = await _prom_query(
        'node_memory_SwapTotal_bytes{job="vps-node-exporter"}'
        ' - node_memory_SwapFree_bytes{job="vps-node-exporter"}'
    )
    avail_min = await _prom_query(
        'min_over_time(node_memory_MemAvailable_bytes'
        '{job="vps-node-exporter"}[24h])'
    )

    avail_g = avail / 2**30
    if avail_g < 5:
        line = f"🟡 RAM VPS : {avail_g:.1f}G dispo — sous le seuil d'alerte (5G), surveille les gros process"
    elif avail_g < 8:
        line = f"🟡 RAM VPS : {avail_g:.1f}G dispo — marge modérée"
    else:
        line = f"🟢 RAM VPS : {avail_g:.1f}G dispo"
    if swap_used is not None:
        line += f", swap {swap_used / 2**30:.1f}G utilisés"
    if avail_min is not None:
        line += f" (min 24h : {avail_min / 2**30:.1f}G)"
    return [line]


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


async def _cis_line() -> list[str]:
    """Score CIS-L1 PegaProx via le canal d'actions (lecture seule, état et
    détection de régression côté PVE). Silencieux si indisponible — mais
    une RÉGRESSION est remontée en tête du rapport par morning_report."""
    try:
        raw = await actions_mod._execute("pegaprox_cis")
    except Exception:
        return False, []
    lines: list[str] = []
    regression = False
    for raw_line in (raw or "").splitlines():
        raw_line = raw_line.strip()
        if "|" not in raw_line:
            continue
        tag, text = raw_line.split("|", 1)
        if tag == "RED":
            lines.append(text)
            regression = True
        elif tag == "OK" and text:
            lines.append(text)
    return regression, lines


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

    # 2bis. RAM VPS : dispo + swap + tendance 24h (node_exporter VPS)
    vps_ram = await _vps_ram_lines()
    if vps_ram:
        lines.append("")
        lines.extend(vps_ram)

    # 3. Compliance : score CIS-L1 PegaProx (régression = ligne + action)
    cis_regression, cis_lines = await _cis_line()
    if cis_lines:
        if not cis_regression:
            lines.append("")
        lines.extend(cis_lines)

    # 4. Conclusion adaptive
    # 3bis. Une régression CIS passe AVANT la conclusion : impossible à rater.
    if cis_regression:
        lines.append("👉 Ouvre PegaProx (https://pegaprox.mayoraz-net.ch) : la section hardening détaille les contrôles en échec.")

    if not down:
        lines.append("✅ Rien à faire ce matin.")
    else:
        lines.append("👉 Commence par les checks DOWN ci-dessus, je peux diagnostiquer si tu me demandes.")

    return "\n".join(lines)
