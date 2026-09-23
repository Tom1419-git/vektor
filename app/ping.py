"""Diagnostic /ping : teste le chemin LLM complet maillon par maillon.

Chaîne : bot Telegram -> API FastAPI -> bridge socat -> Ollama -> modèle.
Chaque maillon est testé indépendamment pour nommer précisément celui
qui est cassé en cas d'échec. Lecture seule, sans coût d'inférence pour
les 4 premiers checks ; le 5e fait une micro-inférence (prompt vide de
outils, 1 token) pour prouver la chaîne d'inférence de bout en bout.
"""

import os
import time

import httpx

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://172.20.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")


async def ping_report() -> str:
    """Teste chaque maillon et nomme le premier cassé."""
    lines: list[str] = ["🏓 **Vektor /ping** — chemin LLM complet"]
    failures: list[str] = []

    # 1. Ollama joignable via le bridge (le maillon qui a déjà cassé deux fois)
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        dt = (time.perf_counter() - t0) * 1000
        if response.status_code == 200:
            names = [m.get("name", "") for m in response.json().get("models", [])]
            lines.append(f"🟢 Bridge Ollama ({OLLAMA_BASE_URL}) — {dt:.0f} ms, {len(names)} modèles")
        else:
            failures.append("bridge Ollama")
            lines.append(f"🔴 Bridge Ollama : HTTP {response.status_code}")
    except httpx.HTTPError as exc:
        failures.append("bridge Ollama")
        lines.append(
            f"🔴 Bridge Ollama ({OLLAMA_BASE_URL}) injoignable "
            f"({exc.__class__.__name__}) — vérifier le conteneur "
            "vektor-ollama-bridge et OLLAMA_NETBIRD_HOST"
        )
        return "\n".join(lines + [f"\n❌ Premier maillon cassé : **{failures[0]}**"])

    # 2. Le modèle configuré est-il présent ?
    model_present = any(OLLAMA_MODEL in name for name in names)
    if model_present:
        lines.append(f"🟢 Modèle `{OLLAMA_MODEL}` présent côté Ollama")
    else:
        failures.append("modèle Ollama")
        lines.append(
            f"🔴 Modèle `{OLLAMA_MODEL}` ABSENT — modèles vus : "
            + ", ".join(names[:5])
            + " — vérifier OLLAMA_MODEL et le tag image (ollama pull)"
        )
        lines.insert(1, f"⚠️ Premier maillon cassé : **{failures[0]}**")
        return "\n".join(lines)

    # 3. Le modèle est-il chargé en RAM (keep_alive) ?
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            ps = await client.get(f"{OLLAMA_BASE_URL}/api/ps")
        loaded = [m.get("name", "") for m in ps.json().get("models", [])]
        if any(OLLAMA_MODEL in name for name in loaded):
            lines.append(f"🟢 Modèle chargé en RAM (keep_alive) : réponse immédiate attendue")
        else:
            lines.append(
                "🟡 Modèle NON chargé en RAM — prochaine question LLM : "
                "+40-60 s de chargement (pas une panne)"
            )
    except httpx.HTTPError:
        lines.append("🟡 /api/ps indisponible (état RAM inconnu)")

    # 4. Inférence réelle de bout en bout (micro-prompt, 1 token attendu)
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": "Réponds uniquement: OK",
                    "stream": False,
                    "options": {"num_predict": 5},
                },
            )
        dt = time.perf_counter() - t0
        if response.status_code == 200 and response.json().get("response"):
            sample = response.json()["response"].strip()[:40]
            lines.append(f"🟢 Inférence de bout en bout — {dt:.0f} s (réponse: «{sample}»)")
            lines.append("\n✅ **Chemin LLM complet opérationnel**")
        else:
            failures.append("inférence Ollama")
            lines.append(f"🔴 Inférence : HTTP {response.status_code} ou réponse vide")
    except httpx.HTTPError as exc:
        failures.append("inférence Ollama (timeout 90 s — CPU chargé ?)")
        lines.append(f"🔴 Inférence : {exc.__class__.__name__} après 90 s")

    if failures:
        lines.insert(1, f"⚠️ Maillon dégradé : **{failures[0]}**")
    return "\n".join(lines)
