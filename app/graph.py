from typing import TypedDict
from langchain_core.messages import HumanMessage, SystemMessage
import time

from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END
from .config import get_settings
from .memory import Memory
from .rag import retrieve_context
from .tools import infra_live_report, record_llm_latency
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
"""


class State(TypedDict):
    text: str
    history: list[dict[str, str]]
    context: str
    live_result: str
    response: str


async def run_agent(memory: Memory, text: str, history: list[dict[str, str]], user_key: str = "default") -> str:
    settings = get_settings()
    llm = ChatOllama(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        temperature=0.1,
        # keep_alive limité : le modèle se décharge 5 min après la dernière
        # requête pour ne pas affamer la RAM du VPS (Minecraft cohabite ici).
        keep_alive="5m",
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

    context = await retrieve_context(memory, text)
    live_result = await infra_live_report(text)
    if live_result:
        return live_result
    messages = [SystemMessage(content=SYSTEM)]
    messages.extend(HumanMessage(content=item["content"]) for item in history[-8:])
    messages.append(HumanMessage(content=(
        f"Documentation pertinente :\n{context}\n\n"
        f"Résultat des contrôles live :\n{live_result or 'aucun contrôle déclenché'}\n\n"
        f"Demande actuelle : {text}"
    )))
    inf_start = time.perf_counter()
    try:
        result = await llm.ainvoke(messages)
    finally:
        # Latence d'inférence réelle, affichée par la fiche /model
        record_llm_latency(time.perf_counter() - inf_start)
    return result.content


def build_graph():
    graph = StateGraph(State)
    graph.add_node("answer", lambda state: state)
    graph.set_entry_point("answer")
    graph.add_edge("answer", END)
    return graph.compile()
