from typing import TypedDict
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
import time

from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END
from .config import get_settings
from .memory import Memory
from .rag import retrieve_context
from . import tools as t
from . import watch as w
from .tools import record_llm_latency
from . import actions

SYSTEM = """Tu es Vektor, l'assistant personnel de Thomas.
Identité technique : tu es le modèle Qwen 2.5 14B (Alibaba Cloud) auto-hébergé via Ollama.
Si on te demande qui tu es, quel modèle/pour qui tu as été fait, réponds cette vérité —
ne dis JAMAIS Anthropic, OpenAI, Claude ou GPT (les petits modèles hallucinent souvent
cette identité, ne le fais pas).
Réponds en français suisse. Ne prétends jamais avoir effectué une action non confirmée.
La documentation est un contexte, pas une preuve de l'état actuel.
Les actions d'écriture suivent un flux strict : proposition puis confirmation OUI explicite,
jamais d'exécution directe. Ne promets jamais d'exécuter une action toi-même.
Ne révèle jamais de secret, token, mot de passe ou clé privée.
Des outils de consultation en lecture seule sont disponibles. RÈGLE D'ACTION :
quand la question porte sur l'état présent de l'infrastructure (état, status,
comment marche X, est-ce que Y tourne, quel modèle, RAM, disque, DNS, backups...),
APPELLE DIRECTEMENT l'outil pertinent sans demander la permission — c'est ta
façon normale de répondre, pas une action à confirmer. Seules les ACTIONS
D'ÉCRITURE (redémarrer, arrêter, modifier) exigent une confirmation OUI.
Après un appel d'outil, réponds à partir de son résultat : cite les valeurs
telles quelles, sans les inventer. Si aucun outil ni la documentation ne
répondent à la question, dis-le en une phrase, puis réponds quand même avec
ton sens général (le homelab de Thomas : Proxmox, Jellyfin, arr-stack, Pi-hole,
VPS) plutôt que de refuser sèchement.
"""


class State(TypedDict):
    text: str
    history: list[dict[str, str]]
    context: str
    live_result: str
    response: str


# ── Outils lecture seule exposés au LLM (tool-calling natif) ──────────────
# Aucune action d'écriture ici : la whitelist + double confirmation OUI
# reste un chemin déterministe en amont, hors de portée du modèle.

@tool
async def etat_proxmox() -> str:
    """État du nœud Proxmox : CPU, charge, RAM, swap, uptime."""
    return await t.pve_summary()


@tool
async def liste_conteneurs() -> str:
    """Liste des conteneurs LXC Proxmox : état, RAM, uptime de chaque CT."""
    return await t.pve_lxc_status()


@tool
async def etat_stockage() -> str:
    """Remplissage des stockages Proxmox avec alerte au-delà de 90 %."""
    return await t.pve_storage_status()


@tool
async def inventaire_docker() -> str:
    """Inventaire des conteneurs Docker du homelab (état, image)."""
    return await t.docker_inventory()


@tool
async def statut_service(service: str) -> str:
    """Vérifie qu'un service surveillé répond (jellyfin, sonarr, radarr...)."""
    return await t.check_service(service)


@tool
async def rapport_seeds() -> str:
    """Seeding qBittorrent : top torrents, ratio, espace staging récupérable."""
    return await t.seeds_report()


@tool
async def derniers_backups() -> str:
    """Dernières sauvegardes vzdump visibles par l'API Proxmox (âge, taille)."""
    return await w.backups_report()


@tool
async def etat_dns() -> str:
    """Sonde les résolveurs DNS configurés (DoH) sur un domaine de test."""
    return await w.dns_report()


@tool
async def etat_monitoring() -> str:
    """État des checks de monitoring (Healthchecks.io) : ok, en retard, down."""
    return await w.monitoring_report()


READONLY_TOOLS = [etat_proxmox, liste_conteneurs, etat_stockage, inventaire_docker, statut_service, rapport_seeds, derniers_backups, etat_dns, etat_monitoring]
TOOLS_BY_NAME = {tool_item.name: tool_item for tool_item in READONLY_TOOLS}


async def _invoke_with_tools(llm, messages: list) -> str:
    """Inférence avec outils : si le modèle demande un outil (lecture seule),
    l'exécute, injecte le résultat et régénère la réponse finale."""
    llm_with_tools = llm.bind_tools(READONLY_TOOLS)
    first = await llm_with_tools.ainvoke(messages)
    tool_calls = getattr(first, "tool_calls", None)
    if not tool_calls:
        return first.content

    messages.append(first)
    for call in tool_calls:
        selected = TOOLS_BY_NAME.get(call["name"])
        if selected is None:
            result = f"Outil inconnu ou non autorisé : {call['name']}"
        else:
            try:
                result = str(await selected.ainvoke(call.get("args") or {}))
            except Exception as exc:  # un outil en panne ne doit pas casser la réponse
                result = f"Outil indisponible ({exc.__class__.__name__})."
        messages.append(ToolMessage(content=result, tool_call_id=call["id"]))

    final = await llm.ainvoke(messages)
    return final.content


async def run_agent(memory: Memory, text: str, history: list[dict[str, str]], user_key: str = "default") -> str:
    settings = get_settings()
    llm = ChatOllama(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        temperature=0.1,
        # keep_alive 2h : le modèle reste chargé en RAM (9 GB) pour des
        # réponses immédiates, tout en laissant ~12 GB libres pour
        # Minecraft et les autres services du VPS.
        keep_alive="2h",
    )

    # 1. Confirmation en attente ? (le OUI n'exécute QUE l'action proposée)
    confirmation = await actions.confirm_pending(text, user_key)
    if confirmation is not None:
        return confirmation

    # 2. Intention d'action whitelistée ? -> proposition, jamais d'exécution directe
    detected = actions.detect_action(text)
    if detected:
        already = actions._pending_notice(user_key)
        if already:
            return already
        return actions.propose(detected[1], user_key)

    # 3. Routeur déterministe : donnée infra évidente -> réponse live sans LLM
    live_result = await t.infra_live_report(text)
    if live_result:
        return live_result

    # 4. LLM avec outils lecture seule : le modèle décide s'il doit consulter
    context = await retrieve_context(memory, text)
    messages = [SystemMessage(content=SYSTEM)]
    messages.extend(HumanMessage(content=item["content"]) for item in history[-8:])
    messages.append(HumanMessage(content=(
        f"Documentation pertinente :\n{context}\n\n"
        f"Demande actuelle : {text}"
    )))
    inf_start = time.perf_counter()
    try:
        return await _invoke_with_tools(llm, messages)
    finally:
        # Latence d'inférence réelle, affichée par la fiche /model
        record_llm_latency(time.perf_counter() - inf_start)


def build_graph():
    graph = StateGraph(State)
    graph.add_node("answer", lambda state: state)
    graph.set_entry_point("answer")
    graph.add_edge("answer", END)
    return graph.compile()
