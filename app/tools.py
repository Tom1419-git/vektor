import asyncio
import os
import re
import httpx


def _service_urls() -> dict[str, str]:
    """Services surveillés : config via VEKTOR_SERVICES (format
    `nom=url,nom=url`). Aucune IP privée n'est codée en dur."""
    raw = os.environ.get("VEKTOR_SERVICES", "")
    services: dict[str, str] = {}
    for item in raw.split(","):
        if "=" in item:
            name, url = item.split("=", 1)
            services[name.strip()] = url.strip()
    return services

SERVICE_URLS = _service_urls()

PVE_API_URL = os.environ.get("PVE_API_URL", "https://PVE_HOST:8006")
PVE_API_TOKEN = os.environ.get("PVE_API_TOKEN", "")
PVE_VERIFY_SSL = os.environ.get("PVE_VERIFY_SSL", "0") == "1"
PVE_SSH_HOST = os.environ.get("PVE_SSH_HOST", "PVE_HOST")
PVE_SSH_USER = os.environ.get("PVE_SSH_USER", "root")
PVE_SSH_KEY = os.environ.get("PVE_SSH_KEY", "/app/secrets/vektor_pve_ed25519")


def _fmt_gib(num_bytes: float | None) -> str:
    if num_bytes is None:
        return "?"
    return f"{num_bytes / 2**30:.1f}G"


async def _pve_get(path: str):
    if not PVE_API_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=40,
            verify=PVE_VERIFY_SSL,
            headers={"Authorization": f"PVEAPIToken={PVE_API_TOKEN}"},
        ) as client:
            response = await client.get(f"{PVE_API_URL}/api2/json{path}")
            if response.status_code != 200:
                return None
            return response.json().get("data")
    except httpx.HTTPError:
        return None


async def _pve_node() -> str | None:
    nodes = await _pve_get("/nodes")
    if not nodes:
        return None
    for node in nodes:
        if node.get("status") == "online":
            return node["node"]
    return nodes[0]["node"] if nodes else None


async def pve_summary() -> str:
    node = await _pve_node()
    if not node:
        return "PVE : API injoignable ou non configurée."
    status = await _pve_get(f"/nodes/{node}/status")
    if not status:
        return "PVE : status du noeud indisponible."
    mem = status.get("memory", {}) or {}
    swap = status.get("swap", {}) or {}
    mem_pct = mem.get("used", 0) / max(mem.get("total", 1), 1) * 100
    swap_pct = swap.get("used", 0) / max(swap.get("total", 1), 1) * 100
    # loadavg PVE = liste de strings ; on garde la charge 1 min en nombre propre
    try:
        load1 = float((status.get("loadavg") or ["0"])[0])
    except (ValueError, TypeError, IndexError):
        load1 = 0.0
    lines = [
        f"Noeud {node} : CPU {(status.get('cpu') or 0) * 100:.0f}%, charge {load1:.1f}",
        f"RAM {_fmt_gib(mem.get('used'))}/{_fmt_gib(mem.get('total'))} ({mem_pct:.0f}%)",
        f"Swap {_fmt_gib(swap.get('used'))}/{_fmt_gib(swap.get('total'))} ({swap_pct:.0f}%)",
        f"Uptime {(status.get('uptime') or 0) / 86400:.1f} jours",
    ]
    return "\n".join(lines)


async def pve_lxc_status() -> str:
    node = await _pve_node()
    if not node:
        return "PVE : API injoignable ou non configurée."
    cts = await _pve_get(f"/nodes/{node}/lxc")
    if not cts:
        return "PVE : liste des CTs indisponible."
    lines = ["CTs Proxmox :"]
    for ct in sorted(cts, key=lambda item: item.get("vmid", 0)):
        mem = ct.get("mem", 0)
        maxmem = ct.get("maxmem", 0)
        mem_pct = f"{mem / maxmem * 100:.0f}%" if maxmem else "?"
        lines.append(
            f"CT {ct.get('vmid')} {ct.get('name', '')} : {ct.get('status')}, "
            f"RAM {_fmt_gib(mem)}/{_fmt_gib(maxmem)} ({mem_pct}), "
            f"uptime {(ct.get('uptime') or 0) / 3600:.0f} h"
        )
    return "\n".join(lines)


async def pve_storage_status() -> str:
    node = await _pve_node()
    if not node:
        return "PVE : API injoignable ou non configurée."
    storages = await _pve_get(f"/nodes/{node}/storage")
    if not storages:
        return "PVE : liste des stockages indisponible."
    lines = ["Stockages PVE :"]
    for storage in sorted(storages, key=lambda item: item.get("storage", "")):
        total = storage.get("total") or 0
        used = storage.get("used") or 0
        if total <= 0:
            continue
        pct = used / total * 100
        flag = " ⚠️" if pct >= 90 else ""
        lines.append(
            f"{storage.get('storage')} : {_fmt_gib(used)}/{_fmt_gib(total)} ({pct:.0f}%){flag}"
        )
    return "\n".join(lines)


async def docker_inventory() -> str:
    if not os.path.exists(PVE_SSH_KEY):
        return "Canal SSH lecture seule non configuré."
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-i", PVE_SSH_KEY,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/tmp/known_hosts",
            "-o", "ConnectTimeout=15",
            f"{PVE_SSH_USER}@{PVE_SSH_HOST}",
            "vektor-status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=100)
    except (asyncio.TimeoutError, OSError):
        return "Canal SSH lecture seule indisponible pour le moment."
    text = stdout.decode(errors="replace").strip()
    return text if text else "Sortie vide du canal lecture seule."


async def check_service(name: str) -> str:
    url = SERVICE_URLS.get(name.lower())
    if not url:
        return f"Service inconnu ou non autorisé : {name}"
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.get(url)
        return f"{name}: HTTP {response.status_code} depuis le contrôle live."
    except httpx.HTTPError as exc:
        return f"{name}: indisponible ({exc.__class__.__name__})."


_STATE_WORDS = ["état", "etat", "status", "statut", "tourne", "tournent", "liste",
                "inventaire", "up", "down", "marche", "santé", "sante"]


_EXPLAIN_WORDS = ["explique", "pourquoi", "comment", "c'est quoi", "qu'est-ce", "qu est ce", "difference", "différence", "définis", "define"]


async def full_status_report() -> str:
    """Rapport homelab complet formaté Telegram, sans LLM, données live."""
    lines: list[str] = ["🏠 **VEKTOR — Rapport homelab**"]

    node = await _pve_node()
    if node:
        status = await _pve_get(f"/nodes/{node}/status")
        if status:
            mem = status.get("memory", {}) or {}
            swap = status.get("swap", {}) or {}
            mem_pct = mem.get("used", 0) / max(mem.get("total", 1), 1) * 100
            swap_pct = swap.get("used", 0) / max(swap.get("total", 1), 1) * 100
            load = status.get("loadavg") or ["?"]
            if isinstance(load, str):
                load = load.split()
            lines.append(
                f"\n⚙️ **PVE** : charge {load[0] if load else '?'} | "
                f"RAM {mem_pct:.0f}% | swap {swap_pct:.0f}% | "
                f"up {(status.get('uptime') or 0) / 86400:.1f} j"
            )

        cts = await _pve_get(f"/nodes/{node}/lxc")
        if cts:
            running = [c for c in cts if c.get("status") == "running"]
            lines.append(f"\n📦 **CTs** : {len(running)}/{len(cts)} running")
            for ct in sorted(cts, key=lambda item: item.get("vmid", 0)):
                icon = "🟢" if ct.get("status") == "running" else "🔴"
                maxmem = ct.get("maxmem") or 0
                mem_pct_ct = (ct.get("mem") or 0) / maxmem * 100 if maxmem else 0
                lines.append(
                    f"{icon} {ct.get('vmid')} {ct.get('name', '')} : "
                    f"{ct.get('status')}, RAM {mem_pct_ct:.0f}%"
                )

        storages = await _pve_get(f"/nodes/{node}/storage")
        if storages:
            lines.append("\n💾 **Stockages**")
            for storage in sorted(storages, key=lambda item: item.get("storage", "")):
                total = storage.get("total") or 0
                used = storage.get("used") or 0
                if total <= 0:
                    continue
                pct = used / total * 100
                icon = "⚠️" if pct >= 90 else "•"
                lines.append(f"{icon} {storage.get('storage')} : {pct:.0f}% ({_fmt_gib(total - used)} libres)")
    else:
        lines.append("\n🔴 **PVE** : API injoignable")

    inv = await docker_inventory()
    if not inv.startswith("Canal") and not inv.startswith("Sortie"):
        up_count = inv.count("Up ")
        exited = inv.lower().count("exited")
        lines.append(f"\n🐳 **Docker** : {up_count} conteneurs Up" + (f", {exited} exited ⚠️" if exited else ""))
        dead = [l for l in inv.splitlines() if "exited" in l.lower() or "restarting" in l.lower()]
        for entry in dead[:5]:
            lines.append(f"⚠️ {entry.strip()}")
    else:
        lines.append("\n🐳 **Docker** : canal indisponible")

    lines.append("\n🌐 **Services**")
    for name in ["jellyfin", "sonarr", "radarr", "qbittorrent"]:
        result = await check_service(name)
        ok = "HTTP" in result and not result.startswith(name + ": indisponible")
        icon = "🟢" if ok else "🔴"
        lines.append(f"{icon} {name} : {result.split(': ', 1)[1] if ': ' in result else result}")

    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=2)))
    lines.append(f"\n_ Rapport généré le {now.strftime('%d/%m à %H:%M')} — lecture seule_")
    return "\n".join(lines)


async def infra_live_report(text: str) -> str | None:
    low = text.lower()
    parts: list[str] = []

    services = [name for name in SERVICE_URLS if name in low]
    if services:
        for name in services[:3]:
            parts.append(await check_service(name))

    is_explanation = any(word in low for word in _EXPLAIN_WORDS)
    full = any(k in low for k in ["homelab", "état général", "etat general", "vue d'ensemble"])
    # Alexa n'envoie que le slot extrait (ex: "proxmox", sans "état") :
    # un sujet nu et court est traite comme une demande d'etat.
    bare_subject = len(low.split()) <= 5 and any(re.search(
        r"\b(proxmox|pve|docker|conteneur|container|stack|arr|conteneurs?|cts|lxc|disques?|stockage|espace|ram|swap|cpu|charge)\b", low
    ) or name in low for name in SERVICE_URLS)
    state = (any(word in low for word in _STATE_WORDS) or bare_subject) and not is_explanation

    want_node = (re.search(
        r"\b(proxmox|pve|hyperviseur|noeud|node|charge|cpu|swap|uptime)\b", low
    ) or re.search(r"\bram\b|\bm[ée]moire\b", low)) and (state or full)
    want_cts = bool(
        re.search(r"\bct\s?\d+\b|\bcts\b|\blxc\b", low)
        or ("conteneur" in low and "proxmox" in low)
    ) and (state or full)
    want_docker = ("docker" in low and state) or (
        re.search(r"\b(conteneurs?|containers?|stacks?|arr)\b", low) and state and not services
    )
    want_storage = re.search(r"\b(disque|disques|stockage|espace)\b", low) and (state or full)

    if full:
        parts.append(await pve_summary())
        parts.append(await pve_lxc_status())
        parts.append(await docker_inventory())
        parts.append(await pve_storage_status())
    else:
        if want_node:
            parts.append(await pve_summary())
        if want_cts:
            parts.append(await pve_lxc_status())
        if want_docker:
            parts.append(await docker_inventory())
        if want_storage:
            parts.append(await pve_storage_status())

    if not parts:
        return None
    return "\n\n".join(p for p in parts if p)
