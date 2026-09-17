from typing import TypedDict
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END
from .config import get_settings
from .memory import Memory
from .rag import retrieve_context
from .tools import infra_live_report

SYSTEM = """Tu es Vektor, l'assistant personnel de Thomas.
Réponds en français suisse. Ne prétends jamais avoir effectué une action non confirmée.
La documentation est un contexte, pas une preuve de l'état actuel.
Toute écriture, suppression, redémarrage, commande shell ou changement réseau est interdite dans cette version.
Pour une demande d'action, explique que seule la lecture est activée.
Ne révèle jamais de secret, token, mot de passe ou clé privée.
"""


class State(TypedDict):
    text: str
    history: list[dict[str, str]]
    context: str
    live_result: str
    response: str


async def run_agent(memory: Memory, text: str, history: list[dict[str, str]]) -> str:
    settings = get_settings()
    llm = ChatOllama(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        temperature=0.1,
        # keep_alive limité : le modèle se décharge 5 min après la dernière
        # requête pour ne pas affamer la RAM du VPS (Minecraft cohabite ici).
        keep_alive="5m",
    )
    context = await retrieve_context(memory, text)
    live_result = await infra_live_report(text)
    if live_result:
        return live_result
    messages = [SystemMessage(content=SYSTEM)]
    messages.extend(HumanMessage(content=item["content"]) for item in history[-8:])
    messages.append(HumanMessage(content=(
        f"Documentation pertinente :\n{context}\n\n"
        f"Résultat des contrôles live :\n{live_result}\n\n"
        f"Demande actuelle : {text}"
    )))
    result = await llm.ainvoke(messages)
    return result.content


def build_graph():
    graph = StateGraph(State)
    graph.add_node("answer", lambda state: state)
    graph.set_entry_point("answer")
    graph.add_edge("answer", END)
    return graph.compile()
